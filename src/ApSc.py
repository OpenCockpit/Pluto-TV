# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0
#
#   ApSc - incremental .ap/.sc (access-point / structure) index writer.
#
#   Enigma2's native DVB record path builds these two sidecar files live, as
#   part of demuxing, so a recorded .ts always has a trick-play/seek index by
#   the time it's finished. eServiceMP3Record (used for all recordings that
#   go through RecordingProxy - see servicemp3record.cpp) has none of that:
#   it is a plain uridecodebin -> filesink GStreamer pipeline that writes raw
#   bytes straight to the file and nothing else. Enigma2 does ship an offline
#   equivalent, lib/timeshift/createapscfiles.cc (compiled as
#   reconstruct_apsc), which rebuilds .ap/.sc after the fact by scanning a
#   finished .ts for MPEG2/H.264 frame headers - this module is a port of
#   that same algorithm (framesearch()/do_one() there), restructured to
#   consume the recording incrementally via feed() calls instead of blocking
#   file reads, so the index grows alongside the .ts in real time instead of
#   requiring a separate pass once recording stops.
#
#   One deliberate departure from the reference tool: instead of its
#   heuristic "guess the video PID from the first PES header with a
#   stream_id in 0xE0-0xEF" (all it can do with no other context), this
#   parses the actual PAT then PMT to get a definite video PID and stream
#   type. ffmpeg's -pat_period 2 (see LiveProxy._ffmpeg_mux) guarantees
#   PAT/PMT show up near the start of every recording. Once found, the PID
#   is treated as fixed for the session, same as the reference tool's own
#   "lock onto the first-found pid" behavior - consistent here too, since
#   every segment is muxed by ffmpeg with the same fixed stream mapping.
#
#   Byte-level start-code matching (which MPEG2/H.264 marker bytes count as
#   a frame boundary, which count as an access point, how the "structure"
#   word is packed) is kept identical to the reference tool rather than
#   redesigned, so a recording's .ap/.sc are byte-for-byte what
#   reconstruct_apsc would have produced from the same file after the fact -
#   with one exception (see the next paragraph).
#
#   Second deliberate departure: the reference tool's H.264 access-point
#   trigger is the Access Unit Delimiter's primary_pic_type field ("this AU
#   is I-slices only" - a hint the encoder is free to set conservatively).
#   Confirmed in production against real Pluto TV recordings: this stream's
#   encoder always emits primary_pic_type=7 (the generic/permissive value),
#   even across real IDR access units - so that hint alone never fires and
#   .ap stays permanently empty regardless of how the file is otherwise
#   written. This module additionally looks for an actual IDR slice NAL
#   (nal_unit_type == 5) as the access-point trigger - a spec-guaranteed,
#   unambiguous random-access point that doesn't depend on the encoder
#   setting an optional hint correctly. The AUD-based hint is still tracked
#   (see ApScWriter._h264_kf_seen) for diagnostic visibility, but no longer
#   gates any .ap write.
#
#   Third departure, and a deliberate one: this module no longer writes .ap
#   at all (ApScWriter._open_files only opens .sc; _write_ap is a no-op).
#   enigma2's own seek resolution (eDVBTSTools::getOffset, lib/dvb/tstools.cpp)
#   branches on eMPEGStreamInformation::hasAccessPoints() (lib/dvb/pvrparse.h) -
#   when true, it resolves every seek against the in-memory access-point map
#   that eDVBChannel::playSource() loaded from .ap exactly once, when the file
#   was first opened for playback (lib/dvb/dvb.cpp; nothing anywhere in
#   dvb.cpp/pvrparse.cpp/servicedvb.cpp ever reloads it during that same
#   session). Confirmed in production: watching a PlutoTVCockpit recording
#   while it's still actively being written let seeking/skipping reach only
#   whatever position .ap covered at the moment playback opened the file -
#   any further skip, no matter how large, landed on that same frozen offset.
#   When hasAccessPoints() is false instead (no .ap loaded), getOffset() takes
#   a completely different path: calcBeginAndEnd() + takeSamples(), an
#   interpolation-based estimate that calls the source's current length()
#   fresh on every call - so it naturally tracks a still-growing file instead
#   of a one-time snapshot. TimeshiftCockpit's own recordings independently
#   confirm this works well in practice: its recording pipeline
#   (TSRecordingTaskExecution.copyTSRecording) runs the real createapscfiles
#   tool once on an already-complete, static file, and in practice those
#   recordings end up with only a .sc file, never an .ap - and skip/jump on
#   them is smooth with no such ceiling. .sc (frame/structure boundaries,
#   used for trick-play stepping) is unaffected either way and is still
#   generated normally.

import struct

from .Debug import logger

_U64_MASK = (1 << 64) - 1
_STREAM_TYPE_MPEG2 = 0x02
_STREAM_TYPE_H264 = 0x1B
_VIDEO_STREAM_TYPES = (_STREAM_TYPE_MPEG2, _STREAM_TYPE_H264)
_NAL_TYPE_IDR = 5

_RETRY = object()


def _framepts(buf, pos):
    """Read the PTS (90kHz ticks) of the PES packet starting at TS packet
    offset *pos*, or None if this packet doesn't carry one.

    Direct port of createapscfiles.cc's framepts(): requires PUSI set, a
    payload present, a PES start code (00 00 01) at the front of the
    payload (after any adaptation field), and PTS_DTS_flags indicating a
    PTS is present - same 33-bit/90kHz field layout used elsewhere in this
    plugin (RecordingProxy._rw_ts_field, LiveProxy._first_pts), just
    read-only and parameterized by an arbitrary known packet offset instead
    of scanning for the first match.
    """
    end = pos + 188
    afc = (buf[pos + 3] >> 4) & 0x03
    if not (afc & 0x01):
        return None
    if not (buf[pos + 1] & 0x40):
        return None
    tmp = pos + buf[pos + 4] + 5 if (afc & 0x02) else pos + 4
    if tmp + 8 > end:
        return None
    if buf[tmp] != 0 or buf[tmp + 1] != 0 or buf[tmp + 2] != 1:
        return None
    if not (buf[tmp + 7] & 0x80):
        return None
    if tmp + 14 > end:
        return None
    b0, b1, b2, b3, b4 = buf[tmp + 9], buf[tmp + 10], buf[tmp + 11], buf[tmp + 12], buf[tmp + 13]
    return (((b0 & 0x0E) << 29) | (b1 << 22) | ((b2 & 0xFE) << 14)
            | (b3 << 7) | ((b4 & 0xFE) >> 1))


def _parse_pat_packet(buf, off):
    """Return the PMT PID from the PAT packet at *off*, _RETRY if the
    section isn't fully captured in this one packet, or None if a complete
    PAT was parsed but held no program (shouldn't happen for a valid mux).
    """
    if not (buf[off + 1] & 0x40):
        return _RETRY
    afc = (buf[off + 3] >> 4) & 0x03
    if not (afc & 0x01):
        return _RETRY
    pos = off + 4
    if afc & 0x02:
        pos += 1 + buf[off + 4]
    if pos >= off + 188:
        return _RETRY
    pointer_field = buf[pos]
    sec_start = pos + 1 + pointer_field
    if sec_start + 8 > off + 188:
        return _RETRY
    sec = buf[sec_start:off + 188]
    if sec[0] != 0x00:
        return _RETRY
    section_length = ((sec[1] & 0x0F) << 8) | sec[2]
    end = 3 + section_length
    if len(sec) < end:
        return _RETRY
    p = 8
    loop_end = end - 4
    while p + 4 <= loop_end:
        program_number = (sec[p] << 8) | sec[p + 1]
        pid = ((sec[p + 2] & 0x1F) << 8) | sec[p + 3]
        if program_number != 0:
            return pid
        p += 4
    return None


def _parse_pmt_packet(buf, off):
    """Return (video_pid, stream_type) from the PMT packet at *off*, _RETRY
    if the section isn't fully captured yet, or None if a complete PMT was
    parsed but none of its streams is a supported video type.
    """
    if not (buf[off + 1] & 0x40):
        return _RETRY
    afc = (buf[off + 3] >> 4) & 0x03
    if not (afc & 0x01):
        return _RETRY
    pos = off + 4
    if afc & 0x02:
        pos += 1 + buf[off + 4]
    if pos >= off + 188:
        return _RETRY
    pointer_field = buf[pos]
    sec_start = pos + 1 + pointer_field
    if sec_start + 12 > off + 188:
        return _RETRY
    sec = buf[sec_start:off + 188]
    if sec[0] != 0x02:
        return _RETRY
    section_length = ((sec[1] & 0x0F) << 8) | sec[2]
    end = 3 + section_length
    if len(sec) < end:
        return _RETRY
    program_info_length = ((sec[10] & 0x0F) << 8) | sec[11]
    p = 12 + program_info_length
    loop_end = end - 4
    while p + 5 <= loop_end:
        stream_type = sec[p]
        pid = ((sec[p + 1] & 0x1F) << 8) | sec[p + 2]
        es_info_length = ((sec[p + 3] & 0x0F) << 8) | sec[p + 4]
        if stream_type in _VIDEO_STREAM_TYPES:
            return pid, stream_type
        p += 5 + es_info_length
    return None


class ApScWriter:
    """Feeds recorded .ts bytes in, writes matching .ap/.sc entries as it goes.

    feed() must be called with exactly the bytes landing in the recorded
    file, in order - offsets written to .ap/.sc are file byte offsets, so
    anything fed here that doesn't end up on disk (or vice versa) would
    desync the index from the recording it's meant to describe. Chunks are
    assumed to each be a whole number of 188-byte TS packets, same
    assumption RecordingProxy already makes throughout.
    """

    def __init__(self, ts_path: str):
        self._path = ts_path
        self._ap = None
        self._sc = None
        self._disabled = False
        self._buf = bytearray()
        self._base_pos = 0
        self._next_pkt = 0
        self._scan_from = 0
        self._pmt_pid = None
        self._video_pid = None
        self._sdflag = False
        self._ap_written_this_au = True
        self._h264_aud_seen = 0
        self._h264_kf_seen = 0
        self._h264_idr_seen = 0

    def feed(self, data: bytes) -> None:
        if not data or self._disabled:
            return
        try:
            self._feed(data)
        except Exception as exc:
            logger.debug('%s: disabling AP/SC generation after error: %r', self._path, exc)
            self._disabled = True
            self.close()

    def _feed(self, data: bytes) -> None:
        self._buf += data
        self._sdflag = False
        if self._video_pid is None:
            self._find_video_pid()
        if self._video_pid is not None:
            self._scan()
        self._trim()

    def _find_video_pid(self) -> None:
        buf = self._buf
        npkts = len(buf) // 188
        for idx in range(self._next_pkt, npkts):
            off = idx * 188
            if buf[off] != 0x47:
                continue
            pid = ((buf[off + 1] & 0x1F) << 8) | buf[off + 2]
            if pid == 0 and self._pmt_pid is None:
                result = _parse_pat_packet(buf, off)
                if result is _RETRY:
                    continue
                if result is None:
                    logger.debug('%s: PAT has no program; disabling AP/SC', self._path)
                    self._disabled = True
                    return
                self._pmt_pid = result
            elif self._pmt_pid is not None and pid == self._pmt_pid:
                result = _parse_pmt_packet(buf, off)
                if result is _RETRY:
                    continue
                if result is None:
                    logger.debug('%s: no supported video stream in PMT; disabling AP/SC', self._path)
                    self._disabled = True
                    return
                self._video_pid, stream_type = result
                logger.debug('%s: locked video pid=%#x stream_type=%#x',
                             self._path, self._video_pid, stream_type)
                self._open_files()
                break
        self._next_pkt = npkts

    def _open_files(self) -> None:
        """Create .sc only - see the module docstring's third departure for
        why .ap is deliberately never created. self._ap stays None forever;
        _write_ap() no-ops on that, so the IDR/AUD detection in _scan() that
        would otherwise feed it is harmless dead work, not a crash risk.
        """
        self._sc = open(self._path + '.sc', 'wb')  # pylint: disable=consider-using-with

    def _scan(self) -> None:
        """Port of framesearch()'s inner matching loop: scan for `00 00 01`
        start codes belonging to the locked video PID, emitting .ap/.sc
        entries for the same byte patterns the reference tool recognizes.
        """
        buf = self._buf
        n = len(buf)
        i = self._scan_from
        while i <= n - 7:
            if buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 1:
                i += 1
                continue
            ind = (i // 188) * 188
            if buf[ind] != 0x47:
                i += 1
                continue
            pid = ((buf[ind + 1] & 0x1F) << 8) | buf[ind + 2]
            if pid != self._video_pid:
                i += 1
                continue
            b3 = buf[i + 3]
            if b3 in {0x00, 0xB3, 0xB8}:
                pts = _framepts(buf, ind) if b3 == 0xB3 else None
                dat = b3 | (buf[i + 4] << 8) | (buf[i + 5] << 16) | (buf[i + 6] << 24)
                self._write_sc(self._base_pos + i, dat)
                if pts is not None:
                    self._write_ap(self._base_pos + ind, pts)
                self._sdflag = True
                i += 1
            elif not self._sdflag and b3 == 0x09 and (buf[ind + 1] & 0x40):
                prim = buf[i + 4]
                is_kf = (prim >> 5) == 0
                dat = b3 | (prim << 8)
                self._write_sc(self._base_pos + i, dat)
                self._ap_written_this_au = False
                self._h264_aud_seen += 1
                if is_kf:
                    self._h264_kf_seen += 1
                if self._h264_aud_seen <= 5 or self._h264_aud_seen % 500 == 0:
                    logger.debug('%s: AUD #%s primary_pic_type=%s'
                                 ' (AUD-hint kf so far: %s, IDR-NAL kf so far: %s)',
                                 self._path, self._h264_aud_seen, prim >> 5,
                                 self._h264_kf_seen, self._h264_idr_seen)
                i += 1
            elif (not (b3 & 0x80) and (b3 & 0x1F) == _NAL_TYPE_IDR
                  and (buf[ind + 1] & 0x40) and not self._ap_written_this_au):
                pts = _framepts(buf, ind)
                if pts is not None:
                    self._write_ap(self._base_pos + ind, pts)
                    self._ap_written_this_au = True
                    self._h264_idr_seen += 1
                    if self._h264_idr_seen <= 5 or self._h264_idr_seen % 50 == 0:
                        logger.debug('%s: IDR-NAL access point #%s at pts=%s',
                                     self._path, self._h264_idr_seen, pts)
                i += 1
            else:
                i += 1
        self._scan_from = i

    def _write_ap(self, offset: int, pts: int) -> None:
        if self._ap is None:
            return
        self._ap.write(struct.pack('>QQ', offset & _U64_MASK, pts & _U64_MASK))
        self._ap.flush()

    def _write_sc(self, offset: int, dat: int) -> None:
        self._sc.write(struct.pack('>QQ', offset & _U64_MASK, dat & _U64_MASK))
        self._sc.flush()

    def _trim(self) -> None:
        """Drop consumed prefix bytes so memory stays bounded regardless of
        recording length. Keeps a 188-byte margin before the scan cursor
        (a match's packet-aligned start can be up to 187 bytes behind it).
        """
        if self._video_pid is not None:
            drop = max(0, self._scan_from - 188)
            drop -= drop % 188
            if drop >= 4096:
                del self._buf[:drop]
                self._base_pos += drop
                self._scan_from -= drop
        else:
            drop = self._next_pkt * 188
            if drop >= 4096:
                del self._buf[:drop]
                self._base_pos += drop
                self._next_pkt = 0

    def close(self) -> None:
        if self._h264_aud_seen:
            logger.debug('%s: H.264 summary: AUD-hint kf %s/%s'
                         " (0 here means this stream's encoder never signals primary_pic_type==0,"
                         ' as confirmed in production) | IDR-NAL access points seen: %s'
                         ' (diagnostic only - .ap is deliberately not written, see module docstring)',
                         self._path, self._h264_kf_seen, self._h264_aud_seen, self._h264_idr_seen)
        if self._ap is not None:
            self._ap.close()
            self._ap = None
        if self._sc is not None:
            self._sc.close()
            self._sc = None
