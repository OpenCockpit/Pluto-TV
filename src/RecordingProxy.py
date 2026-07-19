# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0
#
#   RecordingProxy - serves a single, container-continuous MPEG-TS stream for
#   recording a Pluto TV channel, instead of the segment-chunked HLS delivery
#   LiveProxy uses for live playback.
#
#   LiveProxy produces a correctly muxed (audio/video-paired) TS chunk per
#   ~5s HLS segment via ffmpeg (see LiveProxy._ffmpeg_mux), and caches it in
#   state._ready_segs. Enigma2's recording pipeline (eServiceMP3Record:
#   uridecodebin capped at video/mpegts caps, piped straight to filesink)
#   writes whatever raw MPEG-TS bytes arrive straight to the file with no
#   further remuxing, so this used to matter: concatenating independently
#   muxed segments raw got the recorded file a fresh PAT/PMT and a
#   PCR/continuity-counter reset every ~5 seconds for the whole recording -
#   invisible to live playback, but exactly the kind of fragmentation that
#   breaks seeking/trick-play/duration when the file is played back later.
#
#   _ffmpeg_mux now avoids that fragmentation at the source instead: -copyts
#   keeps each segment's real absolute PCR/PTS (already continuous across
#   ordinary segment boundaries on the CDN side - confirmed empirically, not
#   rebased to zero per segment like ffmpeg's default), and _patch_continuity
#   chains each segment's continuity counters onto the previous one's ending
#   values per PID (state._cc_state) rather than letting every independent
#   mux restart them at 0. Only genuine content transitions (is_disc, a real
#   CDN #EXT-X-DISCONTINUITY) still carry a discontinuity flag - correctly,
#   since the encoder actually did restart there.
#
#   That means state._ready_segs is already container-continuous by the time
#   it gets here, for ordinary segments. This module just concatenates those
#   bytes straight into the response stream, in order - no re-remux, no
#   subprocess, no gap where an in-progress recording has broken framing
#   (matters for anything that plays back a still-recording file, e.g.
#   timeshift-style viewing of a recording in progress).
#
#   is_disc segments are the one remaining exception. Live playback wants
#   their real (correctly discontinuous) PCR/PTS - gstreamer's hlsdemux is
#   built to resync on a genuinely flagged discontinuity, and that's what
#   made ad-transition audio/video pairing work at all. But a recording is
#   read start-to-end (or from any seek point) by a single decoder that
#   never expects the underlying clock to jump: observed on real playback of
#   a recording, the video decoder stalls for a couple of seconds at each
#   is_disc transition (ad content uses its own near-zero PTS timeline,
#   wildly discontinuous from the main content's large absolute one) while
#   audio - evidently more tolerant of a DTS jump - keeps playing through
#   it. _rewrite_timeline below fixes this for the recording only: every
#   is_disc segment's PCR/PTS/DTS get shifted by a running offset so they
#   continue the recording's own timeline instead of genuinely jumping,
#   without touching state._ready_segs itself (live playback is untouched).


import threading
import time
from queue import Queue, Full, Empty

from .ApSc import ApScWriter
from .Debug import logger
from .LiveProxy import _ensure_pipeline, _get_state, _first_pts
from .PlutoTVRequest import recording_filename_for

_POLL_INTERVAL = 0.1
_QUEUE_MAXSIZE = 64
_PTS_MASK = (1 << 33) - 1


def _rw_ts_field(buf: bytearray, off: int, delta: int):
    """Shift the 5-byte PTS/DTS-format timestamp at buf[off:off+5] by
    *delta* (90kHz ticks, 33-bit wraparound), preserving its prefix nibble
    (which distinguishes PTS-only/PTS-of-pair/DTS) and marker bits.
    """
    b0, b1, b2, b3, b4 = buf[off], buf[off + 1], buf[off + 2], buf[off + 3], buf[off + 4]
    val = (((b0 & 0x0E) << 29) | (b1 << 22) | ((b2 & 0xFE) << 14)
           | (b3 << 7) | ((b4 & 0xFE) >> 1))
    new_val = (val + delta) & _PTS_MASK
    prefix = b0 & 0xF0
    buf[off] = prefix | (((new_val >> 30) & 0x07) << 1) | 0x01
    buf[off + 1] = (new_val >> 22) & 0xFF
    buf[off + 2] = (((new_val >> 15) & 0x7F) << 1) | 0x01
    buf[off + 3] = (new_val >> 7) & 0xFF
    buf[off + 4] = ((new_val & 0x7F) << 1) | 0x01


def _rw_pcr(buf: bytearray, off: int, delta: int):
    """Shift the 6-byte PCR field at buf[off:off+6] by *delta* (90kHz ticks).

    Only program_clock_reference_base is touched - it shares PTS/DTS's
    33-bit/90kHz representation, so the same delta applies directly. The
    reserved bits and program_clock_reference_extension (finer than one
    90kHz tick) are left exactly as they were.
    """
    b0, b1, b2, b3, b4 = buf[off], buf[off + 1], buf[off + 2], buf[off + 3], buf[off + 4]
    pcr_base = (b0 << 25) | (b1 << 17) | (b2 << 9) | (b3 << 1) | (b4 >> 7)
    new_base = (pcr_base + delta) & _PTS_MASK
    reserved_and_ext_hi = b4 & 0x7F
    buf[off] = (new_base >> 25) & 0xFF
    buf[off + 1] = (new_base >> 17) & 0xFF
    buf[off + 2] = (new_base >> 9) & 0xFF
    buf[off + 3] = (new_base >> 1) & 0xFF
    buf[off + 4] = ((new_base & 0x01) << 7) | reserved_and_ext_hi


def _segment_is_disc(data: bytes) -> bool:
    """True if any packet in *data* carries discontinuity_indicator=1.

    _ffmpeg_mux sets -mpegts_flags initial_discontinuity only for genuine
    is_disc segments (see LiveProxy.py) - detecting the flag directly from
    the bytes here avoids needing a second, separately-threaded is_disc
    signal from the video prefetch loop just for this module's benefit.
    """
    n = len(data) // 188
    for i in range(n):
        off = i * 188
        if data[off] != 0x47:
            continue
        afc = (data[off + 3] >> 4) & 0x03
        if afc not in (0x02, 0x03):
            continue
        af_len = data[off + 4]
        if af_len > 0 and (data[off + 5] & 0x80):
            return True
    return False


def _rewrite_timeline(data: bytes, delta: int) -> bytes:
    """Shift every PCR/PTS/DTS field in *data* by *delta* (90kHz ticks).

    Continuity counters and everything else are left untouched. A no-op
    (returns *data* unchanged, skipping the scan) when delta is 0, which is
    the common case for every segment until the next is_disc.
    """
    if delta == 0:
        return data
    buf = bytearray(data)
    n = len(buf) // 188
    for i in range(n):
        off = i * 188
        if buf[off] != 0x47:
            continue
        afc = (buf[off + 3] >> 4) & 0x03
        has_adaptation = afc in {0x02, 0x03}
        has_payload = afc in {0x01, 0x03}
        pos = off + 4
        if has_adaptation:
            af_len = buf[pos]
            af_start = pos + 1
            if af_len > 0 and (buf[af_start] & 0x10):
                _rw_pcr(buf, af_start + 1, delta)
            pos += 1 + af_len
        if has_payload:
            pusi = (buf[off + 1] >> 6) & 1
            if pusi and pos + 9 <= off + 188 and buf[pos:pos + 3] == b'\x00\x00\x01':
                ptsdts_flags = (buf[pos + 7] >> 6) & 0x03
                if ptsdts_flags in {0x02, 0x03} and pos + 14 <= off + 188:
                    _rw_ts_field(buf, pos + 9, delta)
                if ptsdts_flags == 0x03 and pos + 19 <= off + 188:
                    _rw_ts_field(buf, pos + 14, delta)
    return bytes(buf)


class RecordingSession:
    """Feeds one recording connection with already-continuous muxed segments.

    Not shared across connections - a recording connection maps 1:1 to one
    timer/file, so each GET on /rec/{channel_id}.ts gets its own session,
    created and torn down within that request's lifetime.
    """

    def __init__(self, channel_id, state):
        self.channel_id = channel_id
        self.state = state
        self._stopped = False
        self._queue: 'Queue[bytes | None]' = Queue(maxsize=_QUEUE_MAXSIZE)
        with state._lock:
            self._next_seq = max(state._ready_segs) if state._ready_segs else None
        self._pts_delta = 0
        self._expected_next_pts: 'int | None' = None
        self._seg_ticks = int(state.seg_duration * 90000)
        self._apsc = self._make_apsc(channel_id)
        self._feeder_stop = threading.Event()
        self._feeder_thread = threading.Thread(target=self._feed_loop, daemon=True,
                                               name=f'PlutoRecFeeder-{channel_id[:8]}')
        self._feeder_thread.start()

    @staticmethod
    def _make_apsc(channel_id) -> 'ApScWriter | None':
        """Resolve this recording's destination file and set up an
        ApScWriter for it, or None if no owning timer could be found (e.g.
        an instant-record path recording_filename_for doesn't recognize) -
        recording proceeds either way, just without a jump/trick-play index.

        Failures here must never take the recording itself down with them -
        same best-effort posture as _continuize below - so any lookup error
        is swallowed and logged rather than propagated.
        """
        try:
            filename = recording_filename_for(channel_id)
        except Exception as exc:
            logger.debug('%s: recording_filename_for failed: %r', channel_id, exc)
            return None
        if filename is None:
            logger.debug('%s: no owning timer found, skipping AP/SC', channel_id)
            return None
        return ApScWriter(filename)

    def _output(self, muxed: bytes) -> None:
        """Continuize *muxed*, mirror the result into this session's AP/SC
        index (if any), then enqueue it for the client - in that order,
        since ApSc's offsets must describe the bytes actually landing in
        the recorded file, i.e. post-_continuize, not the pre-shift ones.
        """
        out = self._continuize(muxed)
        if self._apsc is not None:
            self._apsc.feed(out)
        self._enqueue(out)

    def _continuize(self, muxed: bytes) -> bytes:
        """Rewrite *muxed* so a genuine is_disc segment continues this
        recording's running timeline instead of jumping to its real (but
        discontinuous) source PCR/PTS; ordinary segments get whatever delta
        is currently in effect (usually 0, until the first is_disc fires).

        Reverted 2026-07-14: this briefly also resynced on any large,
        unflagged PTS gap (a CDN stall with no #EXT-X-DISCONTINUITY tag),
        to close a genuine dead-gap case - see git history/session notes if
        revisiting. Pulled back out because number-key jump seeking broke
        immediately after, and Enigma2's own eMPEGStreamInformation::
        fixupDiscontinuties (lib/dvb/pvrparse.cpp) already self-heals any
        gap over 10s between .ap entries at load time independent of what
        this method does - the extra trigger may have been solving a
        problem the engine already handles, while conflicting with it in a
        way not yet root-caused. Back to is_disc-only until that's
        understood.

        Best-effort: if this segment's first PTS can't be read at all, the
        running delta/expectation are left untouched and the segment passes
        through unmodified rather than risking a wrong shift from stale
        state.
        """
        first_pts = _first_pts(muxed)
        if first_pts is None:
            return muxed
        if _segment_is_disc(muxed) and self._expected_next_pts is not None:
            self._pts_delta = self._expected_next_pts - first_pts
        out = _rewrite_timeline(muxed, self._pts_delta)
        self._expected_next_pts = (first_pts + self._pts_delta + self._seg_ticks) & _PTS_MASK
        return out

    def _enqueue(self, chunk):
        """Blocking put that still notices stop() while the queue is full."""
        while not self._feeder_stop.is_set():
            try:
                self._queue.put(chunk, timeout=0.5)
                return
            except Full:
                continue

    def _feed_loop(self):
        state = self.state
        seq = self._next_seq
        wait_start = None
        try:
            while not self._feeder_stop.is_set():
                state.last_access = time.monotonic()
                if seq is None:
                    with state._lock:
                        if state._ready_segs:
                            seq = min(state._ready_segs)
                        else:
                            time.sleep(_POLL_INTERVAL)
                            continue
                with state._lock:
                    muxed = state._ready_segs.get(seq)
                    provisional = seq in state._provisional_segs
                    newest = max(state._ready_segs) if state._ready_segs else None
                if state.is_duplicate(seq):
                    seq += 1
                    wait_start = None
                    continue
                if muxed is not None and not provisional:
                    self._output(muxed)
                    seq += 1
                    wait_start = None
                    continue
                if wait_start is None:
                    wait_start = time.monotonic()
                elif time.monotonic() - wait_start > state.seg_duration * 4:
                    if muxed is not None:
                        self._output(muxed)
                        seq += 1
                        wait_start = None
                        continue
                    if newest is not None and newest > seq:
                        logger.debug('%s: seq=%s never arrived, skipping to %s', self.channel_id, seq, newest)
                        seq = newest
                        wait_start = None
                        continue
                time.sleep(_POLL_INTERVAL)
        finally:
            try:
                self._queue.put_nowait(None)
            except Full:
                pass

    def read_output(self):
        """Generator yielding muxed segment bytes in order until EOF."""
        while True:
            try:
                chunk = self._queue.get(timeout=1.0)
            except Empty:
                if self._feeder_stop.is_set():
                    break
                continue
            if chunk is None:
                break
            yield chunk

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self._feeder_stop.set()
        self._feeder_thread.join(timeout=5)
        if self._apsc is not None:
            self._apsc.close()


def handle_recording(handler, channel_id):
    """Serve /rec/{channel_id}.ts on the given BaseHTTPRequestHandler.

    Ensures the channel is registered/prefetching (same requirement live
    playback has - see LiveProxy.register_channel's docstring on
    recording-only sessions with no live viewer), starts a fresh
    RecordingSession, and streams its harmonized output until the client
    (Enigma2's recorder) disconnects.
    """
    state = _get_state(channel_id)
    if not state:
        logger.debug('%s: unknown channel, 404', channel_id)
        handler.send_response(404)
        handler.end_headers()
        return

    _ensure_pipeline(channel_id, state)

    session = RecordingSession(channel_id, state)
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'video/MP2T')
        handler.end_headers()
        for chunk in session.read_output():
            handler.wfile.write(chunk)
    finally:
        session.stop()
