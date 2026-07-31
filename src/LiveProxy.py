# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0
#
#   LiveProxy – local HLS proxy that converts demuxed Pluto TV streams
#   (separate video-TS + audio-TS delivered via EXT-X-MEDIA:TYPE=AUDIO) into
#   single muxed TS segments that Enigma2's DVB hardware pipeline handles.


import bisect
import os
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict, namedtuple
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urljoin, urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor

import requests

from .Debug import logger
from .HLSParser import HLSPlaylist
from .AES128 import AES128

PROXY_HOST = '127.0.0.1'
PROXY_PORT = 7654
_SEG_DURATION = 5.0
_MAX_SEG_CACHE = 200
_SEQ_REPROCESS_WINDOW = 10
_PREFETCH_LEAD_SEGMENTS = 20
_AUDIO_BUFFER_RETAIN_EPOCHS = 1
_MAX_AUDIO_EPOCH_LAG = 2
_DISC_LOOKAHEAD_SEGMENTS = 30
_MAX_AUDIO_BUFFER_BYTES = 50_000_000
_VIDEO_BUFFER_RETAIN_MARGIN_BYTES = 400000
_AUDIO_FETCH_WORKERS = 4
_AUDIO_PREPOPULATE_CAP = 10
_LATE_AUDIO_WAIT_FACTOR = 2.0
_DISC_AUDIO_PEEK_WAIT = 1.5
_HANDLE_SEGMENT_DEADLINE_FACTOR = 1.0
_COLD_START_FIRST_SEGMENT_WAIT = 5.0
_VOD_COLD_START_MIN_SEGMENTS = 5
_VOD_COLD_START_WAIT = 10.0
_FFMPEG_TIMEOUT = 30
_SESSION = requests.Session()

_FFMPEG = 'ffmpeg'
_FFPROBE = 'ffprobe'


def _first_pts(data: bytes) -> 'int | None':
    """Return the first PTS (90 kHz ticks) found in raw MPEG-TS *data*.

    Scans up to the first 200 TS packets for a PES with a PTS field.
    Returns None if no PTS is found.
    """
    lim = min(len(data) - 187, 188 * 200)
    for off in range(0, lim, 188):
        if data[off] != 0x47:
            continue
        if not ((data[off + 1] >> 6) & 1):
            continue
        afc = (data[off + 3] >> 4) & 3
        if not (afc & 1):
            continue
        pl = off + 4 + (1 + data[off + 4] if afc & 2 else 0)
        if pl + 14 > off + 188:
            continue
        if data[pl:pl + 3] != b'\x00\x00\x01':
            continue
        if not (data[pl + 7] & 0x80):
            continue
        b = data[pl + 9:pl + 14]
        return (((b[0] & 0x0E) << 29) | (b[1] << 22) |
                ((b[2] & 0xFE) << 14) | (b[3] << 7) |
                ((b[4] & 0xFE) >> 1))
    return None


def _probe_audio_format(path: str) -> 'tuple[int, int] | None':
    """Return (sample_rate, channels) of *path*'s first audio stream, or None on failure."""
    try:
        r = subprocess.run(
            [_FFPROBE, '-v', 'error', '-select_streams', 'a:0',
             '-show_entries', 'stream=sample_rate,channels',
             '-of', 'csv=p=0', path],
            capture_output=True, timeout=5, check=False,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        sample_rate, channels = r.stdout.decode(errors='replace').strip().split(',')
        return int(sample_rate), int(channels)
    except Exception:
        return None


def _patch_continuity(data: bytes, start_cc: dict) -> 'tuple[bytes, dict]':
    """Rewrite each PID's continuity-counter nibble so it continues from
    *start_cc* instead of wherever ffmpeg's fresh mux process happened to
    start it (always 0 - a new process has no memory of the previous
    segment's last CC per PID).

    *start_cc* maps pid -> last CC used (or absent/-1 for "never seen",
    which reproduces a fresh 0-start - i.e. passing {} is a no-op measure
    pass that returns the same bytes ffmpeg produced). Returns
    (patched_bytes, ending_cc); the caller feeds ending_cc back in as
    start_cc for the next segment to keep the chain going.

    Only packets that actually carry a payload increment CC, per spec -
    adaptation-field-only packets (no payload) must not bump it.
    """
    packet_size = 188
    buf = bytearray(data)
    cc_state = dict(start_cc)
    n = len(buf) // packet_size
    for i in range(n):
        off = i * packet_size
        if buf[off] != 0x47:
            continue
        pid = ((buf[off + 1] & 0x1F) << 8) | buf[off + 2]
        afc = (buf[off + 3] >> 4) & 0x03
        if afc not in (0x01, 0x03):
            continue
        new_cc = (cc_state.get(pid, -1) + 1) % 16
        cc_state[pid] = new_cc
        buf[off + 3] = (buf[off + 3] & 0xF0) | new_cc
    return bytes(buf), cc_state


_WindowMeta = namedtuple('_WindowMeta', 'epoch start_pts end_pts is_disc')


def _scan_ts_index(ts_bytes: bytes) -> 'tuple[list[tuple[int, int]], bytes | None]':
    """Scan raw TS bytes for every PUSI'd PTS-carrying packet's (offset, pts)
    and the most recent PAT (pid 0) + immediately-following PMT packet pair.

    Generalizes _first_pts's single-packet early-exit scan to index every
    occurrence in the given bytes, not just the first - this is what lets
    _RenditionBuffer.cut() slice an exact PTS range instead of only ever
    handing back a whole segment. Pure function (no shared state touched),
    deliberately kept callable outside any lock: the per-packet scan is the
    expensive part of appending a segment to a _RenditionBuffer, and doing
    it while holding state._lock would stall every other thread's
    concurrent add/cut/trim call the same way logger.debug() would (see
    pop_audio_for_pts's own reasoning for logging outside the lock).
    """
    pts_offsets = []
    pat_pmt = None
    pending_pat = None
    n = len(ts_bytes) // 188
    for i in range(n):
        off = i * 188
        if ts_bytes[off] != 0x47:
            continue
        pid = ((ts_bytes[off + 1] & 0x1F) << 8) | ts_bytes[off + 2]
        if pid == 0:
            pending_pat = bytes(ts_bytes[off:off + 188])
            continue
        if pending_pat is not None and pid != 0x1FFF:
            pat_pmt = pending_pat + bytes(ts_bytes[off:off + 188])
            pending_pat = None
        if not ((ts_bytes[off + 1] >> 6) & 1):
            continue
        afc = (ts_bytes[off + 3] >> 4) & 3
        if not (afc & 1):
            continue
        pl = off + 4 + (1 + ts_bytes[off + 4] if afc & 2 else 0)
        if pl + 14 > off + 188:
            continue
        if ts_bytes[pl:pl + 3] != b'\x00\x00\x01':
            continue
        if not (ts_bytes[pl + 7] & 0x80):
            continue
        b = ts_bytes[pl + 9:pl + 14]
        pts = (((b[0] & 0x0E) << 29) | (b[1] << 22) |
               ((b[2] & 0xFE) << 14) | (b[3] << 7) |
               ((b[4] & 0xFE) >> 1))
        pts_offsets.append((off, pts))
    return pts_offsets, pat_pmt


class _RenditionBuffer:
    """Continuous per-epoch byte buffer for one HLS rendition (video-only or
    audio-only TS).

    Bytes from consecutive CDN segments within the same epoch are appended
    in arrival order - real CDN PTS is already continuous within an epoch
    (the same reason -copyts is already correct for ordinary same-epoch
    segment pairs, see _ffmpeg_mux's docstring), so cut() can slice an
    exact PTS range out of this buffer freely, drawing across CDN segment
    boundaries instead of requiring one whole discrete segment to match
    another.

    A PTS index (pts_list/off_list, kept sorted by construction - see
    append()) is built incrementally alongside each append so cut() can
    binary-search for byte offsets instead of rescanning the whole buffer
    on every call.
    """
    __slots__ = ('epoch', 'is_disc_epoch', 'first_window_emitted',
                 'data', 'pts_list', 'off_list',
                 'last_pat_pmt', 'created_at', 'last_append_at', 'closed_at')

    def __init__(self, epoch: int, is_disc_epoch: bool):
        self.epoch = epoch
        self.is_disc_epoch = is_disc_epoch
        self.first_window_emitted = False
        self.data = bytearray()
        self.pts_list: list = []
        self.off_list: list = []
        self.last_pat_pmt: 'bytes | None' = None
        self.created_at = time.monotonic()
        self.last_append_at = self.created_at
        self.closed_at: 'float | None' = None

    def append(self, ts_bytes: bytes, pts_offsets: list, pat_pmt: 'bytes | None'):
        """Extend the buffer with *ts_bytes*, whose PTS index and PAT/PMT
        were already scanned outside any lock by _scan_ts_index (offsets in
        *pts_offsets* are relative to the start of *ts_bytes*, rebased here
        to this buffer's own coordinates).

        Rejects the whole segment - bytes included, not appended at all -
        if it would go backwards relative to the buffer's current tail
        (e.g. two independent callers racing on overlapping seq ranges at
        channel startup; see _append_audio's own seq-level dedup for the
        primary guard against that). An earlier version of this method
        still appended ts_bytes unconditionally and only skipped the
        offending entries from the PTS index, defending pts_list/off_list's
        required monotonicity - but left that segment's raw bytes
        physically spliced into self.data anyway, unindexed and invisible
        to any cut() whose byte range doesn't happen to span them, but
        silently *included*, out of temporal order, in any cut() whose
        range does. Rejecting the whole segment here is the only way to
        keep every byte in self.data reachable exclusively through
        pts_list/off_list, which cut()'s slicing assumes.
        """
        if self.pts_list and pts_offsets and pts_offsets[0][1] < self.pts_list[-1]:
            return
        base = len(self.data)
        self.data += ts_bytes
        self.last_append_at = time.monotonic()
        for off, pts in pts_offsets:
            self.pts_list.append(pts)
            self.off_list.append(base + off)
        if pat_pmt is not None:
            self.last_pat_pmt = pat_pmt

    def cut(self, start_pts: int, end_pts: 'int | None'):
        """Return (sliced_bytes, actual_start_pts, actual_end_pts) covering
        the buffered index entries in [start_pts, end_pts), or None if not
        (yet) coverable. *end_pts*=None cuts everything currently buffered
        from start_pts on (used to flush a closing epoch's remainder).

        The returned bytes are prepended with the buffer's most recently
        observed PAT+PMT pair (see _scan_ts_index): a slice can start
        mid-CDN-segment, where the nearest PAT/PMT in the original stream
        may sit well before the slice's own start offset.
        """
        if not self.pts_list:
            return None
        start_i = bisect.bisect_left(self.pts_list, start_pts)
        if start_i >= len(self.pts_list):
            return None
        if end_pts is None:
            end_i = len(self.pts_list) - 2
            if end_i < start_i:
                return None
        else:
            if self.pts_list[-1] < end_pts:
                return None
            end_i = bisect.bisect_left(self.pts_list, end_pts) - 1
            if end_i < start_i:
                return None
        start_off = self.off_list[start_i]
        end_off = self.off_list[end_i + 1] if end_i + 1 < len(self.off_list) else len(self.data)
        chunk = bytes(self.data[start_off:end_off])
        if self.last_pat_pmt is not None:
            chunk = self.last_pat_pmt + chunk
        return chunk, self.pts_list[start_i], self.pts_list[end_i]

    def trim_before(self, pts: int, retain_bytes: int = 0):
        """Drop the consumed prefix (bounding memory), keeping *retain_bytes*
        behind the trim point so the narrowed on-demand fallback in
        _handle_segment can still re-derive a just-produced window without
        a fresh CDN re-fetch (see _VIDEO_BUFFER_RETAIN_MARGIN_BYTES).
        """
        i = bisect.bisect_left(self.pts_list, pts)
        if i <= 0:
            return
        cut_off = max(0, self.off_list[i] - retain_bytes)
        if cut_off <= 0:
            return
        del self.data[:cut_off]
        self.off_list = [o - cut_off for o in self.off_list]
        keep = bisect.bisect_left(self.off_list, 0)
        if keep > 0:
            self.pts_list = self.pts_list[keep:]
            self.off_list = self.off_list[keep:]

    def trim_to_max_bytes(self, max_bytes: int):
        """Drop the oldest indexed entries (and their bytes) until this
        buffer's total size is at most *max_bytes*, regardless of whether
        anything has actually consumed them via a successful cut() yet.

        See _MAX_AUDIO_BUFFER_BYTES - a defensive backstop for a buffer
        that never gets a successful match at all (trim_before() alone
        never fires for one), not a routine eviction path.
        """
        if len(self.data) <= max_bytes or not self.off_list:
            return
        target_off = len(self.data) - max_bytes
        i = bisect.bisect_left(self.off_list, target_off)
        if i <= 0 or i >= len(self.off_list):
            return
        cut_off = self.off_list[i]
        del self.data[:cut_off]
        self.off_list = [o - cut_off for o in self.off_list[i:]]
        self.pts_list = self.pts_list[i:]

    def mark_closed(self):
        """Stop receiving appends but stay readable (e.g. for
        _slice_audio_for_window's one-epoch-back fallback) until pruned -
        see _prune_audio_bufs.
        """
        self.closed_at = time.monotonic()


def _ffmpeg_mux(video_data: bytes, audio_data: 'bytes | None' = None,
                state: '_ChannelState | None' = None, is_disc: bool = False,
                track_cc: bool = False, reset_cc: 'bool | None' = None,
                v_pts: 'int | None' = None, a_pts: 'int | None' = None) -> bytes:
    """Mux video-only and audio-only TS segments via system ffmpeg.

    Both inputs are written to seekable temp files so ffmpeg can probe each
    stream's start_time and normalise output PTS.  When *audio_data* is None
    the single stream is re-muxed without explicit stream mapping.
    Falls back to returning the input data unchanged on any ffmpeg error.

    itsoffset is derived from the PTS difference between the two streams -
    it reflects whatever CDN-side alignment exists between them, typically
    near-zero for an ordinary window and up to the PTS span of one window
    at most for a genuine content transition.

    *v_pts*/*a_pts*, if given, are used directly instead of independently
    re-deriving them with _first_pts. Every current caller already knows
    both exactly - they came straight from _RenditionBuffer.cut()'s return
    when the slices being muxed here were cut - so scanning the same bytes
    a second time here would be pure waste. Left optional (falling back to
    _first_pts) so this function stays independently correct if ever called
    with bytes whose PTS isn't already known by the caller.

    When *state* is given, the real audio segment's sample rate/channel count
    is probed once and cached on state.audio_format so later silence-injected
    segments (see the *audio_data* is None branch below) match it instead of
    a hardcoded guess - switching the decoder between two different audio
    formats mid-stream is its own, separate source of playback trouble on top
    of whatever a real content transition already causes.

    -copyts preserves each stream's real absolute PTS/PCR instead of ffmpeg's
    default of rebasing every independent mux to start near zero - confirmed
    against real segment pairs that the underlying CDN timeline is already
    continuous across ordinary segment boundaries, so this alone is enough
    to stop PCR/PTS from resetting every ~5s. It does *not* touch continuity
    counters, which every fresh ffmpeg process still starts at 0 per PID
    regardless - that part is fixed separately below via _patch_continuity.

    *is_disc* controls whether this segment is flagged as a genuine content
    transition (-mpegts_flags initial_discontinuity, matching a real CDN
    #EXT-X-DISCONTINUITY - the underlying encoder actually restarted, so a
    real PCR/PTS jump here is correct, not a bug) versus an ordinary segment
    that should carry on the previous one's timeline with no flag at all.
    This is the one flag consumers (GStreamer's hlsdemux for live, and
    RecordingProxy's _segment_is_disc/_continuize for recordings) ever see
    baked into the TS bytes - it must stay tied to a *real* transition only.

    *track_cc* opts this call into the shared per-channel CC continuation
    chain in state._cc_state, updating it for the next call. Only safe for
    the primary, strictly-in-order video-prefetch path: state._cc_state
    reflects "whatever the highest seq processed so far ended at", so a
    call that might land out of order relative to that (the late-audio
    upgrade overwriting an already-passed seq, or the disconnected
    synchronous fallback) must leave it False - they still get -copyts and
    an is_disc flag that accurately reflects whether their segment is a
    real transition, just not chained CC continuity.

    *reset_cc* controls only whether _patch_continuity restarts the CC chain
    at {} instead of continuing state._cc_state - defaults to *is_disc* (a
    real transition always needs a fresh CC baseline too) but can be set
    True independently of is_disc, for a segment whose *content* is
    continuous but whose CC baseline needs restarting for an unrelated
    bookkeeping reason (see state._cc_resync_pending). Deliberately a
    separate parameter from is_disc: forcing is_disc True to get a CC reset
    would also plant a false -mpegts_flags initial_discontinuity in that
    segment's bytes, telling GStreamer's hlsdemux (or RecordingProxy) a real
    content cut happened right there when it didn't.
    """
    if reset_cc is None:
        reset_cc = is_disc
    vfile = None
    afile = None
    try:
        with tempfile.NamedTemporaryFile(
                prefix='plutov_', suffix='.ts', dir='/tmp', delete=False) as tf:
            tf.write(video_data)
            vfile = tf.name
        if audio_data:
            with tempfile.NamedTemporaryFile(
                    prefix='plutoa_', suffix='.ts', dir='/tmp', delete=False) as tf:
                tf.write(audio_data)
                afile = tf.name
    except OSError as exc:
        logger.debug('temp file write failed: %s', exc)
        for p in filter(None, (vfile, afile)):
            try:
                os.unlink(p)
            except OSError:
                pass
        return video_data

    if v_pts is None:
        v_pts = _first_pts(video_data)
    itsoffset = 0.0
    if audio_data:
        if a_pts is None:
            a_pts = _first_pts(audio_data)
        if v_pts is not None and a_pts is not None:
            itsoffset = (v_pts - a_pts) / 90000.0
            if abs(itsoffset) > 0.020:
                logger.debug('_ffmpeg_mux: pts v=%s a=%s itsoffset=%.3fs',
                             v_pts, a_pts, itsoffset)
        else:
            logger.debug('_ffmpeg_mux: _first_pts failed v=%s a=%s', v_pts, a_pts)
        if state is not None and state.audio_format is None:
            fmt = _probe_audio_format(afile)
            if fmt is not None:
                state.audio_format = fmt

    mpegts_flags = 'initial_discontinuity' if is_disc else ''

    if audio_data:
        cmd = [_FFMPEG, '-y', '-loglevel', 'error',
               '-probesize', '200000', '-analyzeduration', '1000000',
               '-i', vfile,
               '-probesize', '200000', '-analyzeduration', '1000000',
               '-itsoffset', f'{itsoffset:.6f}',
               '-i', afile,
               '-c', 'copy',
               '-map', '0:v:0', '-map', '1:a:0',
               '-f', 'mpegts', '-copyts']
        if mpegts_flags:
            cmd += ['-mpegts_flags', mpegts_flags]
        cmd += ['-pat_period', '2', 'pipe:1']
    elif v_pts is not None:
        if state is not None and state.audio_format is not None:
            _sample_rate, _channels = state.audio_format
        else:
            _sample_rate, _channels = 48000, 2
        _channel_layout = {1: 'mono', 2: 'stereo'}.get(_channels, 'stereo')
        cmd = [_FFMPEG, '-y', '-loglevel', 'error',
               '-probesize', '200000', '-analyzeduration', '1000000',
               '-i', vfile,
               '-itsoffset', f'{v_pts / 90000.0:.6f}',
               '-f', 'lavfi', '-i', f'anullsrc=channel_layout={_channel_layout}:sample_rate={_sample_rate}',
               '-c:v', 'copy', '-c:a', 'aac', '-b:a', '64k',
               '-map', '0:v:0', '-map', '1:a:0',
               '-shortest',
               '-f', 'mpegts', '-copyts']
        if mpegts_flags:
            cmd += ['-mpegts_flags', mpegts_flags]
        cmd += ['-pat_period', '2', 'pipe:1']
    else:
        cmd = [_FFMPEG, '-y', '-loglevel', 'error',
               '-probesize', '200000', '-analyzeduration', '1000000',
               '-i', vfile,
               '-c', 'copy', '-f', 'mpegts', '-copyts']
        if mpegts_flags:
            cmd += ['-mpegts_flags', mpegts_flags]
        cmd += ['-pat_period', '2', 'pipe:1']

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT, check=False)
        if r.returncode == 0 and r.stdout:
            if track_cc and state is not None:
                start_cc = {} if reset_cc else state._cc_state
                patched, state._cc_state = _patch_continuity(r.stdout, start_cc)
                return patched
            return r.stdout
        label = 'av' if audio_data else ('silence' if v_pts is not None else 'video-only')
        logger.debug('_ffmpeg_mux %s rc=%s out=%sb: %s',
                     label, r.returncode, len(r.stdout), r.stderr.decode(errors="replace")[-300:])
        return video_data
    except Exception as exc:
        logger.debug('_ffmpeg_mux exception: %s', exc)
        return video_data
    finally:
        for p in filter(None, (vfile, afile)):
            try:
                os.unlink(p)
            except OSError:
                pass


class _ChannelState:
    """Holds state accumulated while proxying one live channel."""

    __slots__ = ('master_url', 'url_refresher', 'audio_url', 'variant_urls',
                 'seg_duration', 'last_access', 'audio_format',
                 '_video_buf', '_video_cursor_pts', '_audio_bufs',
                 '_audio_epoch', '_audio_disc_seqs_seen', '_audio_appended_seqs', '_video_epoch',
                 '_next_chunk_seq', '_window_meta',
                 '_last_requested_seq', '_ready_segs',
                 '_audio_prepopulated', '_provisional_segs',
                 '_audio_prefetch_thread', '_audio_prefetch_stop',
                 '_video_prefetch_thread', '_video_prefetch_stop', '_lock', '_cc_state',
                 '_cc_resync_pending', '_last_playlist_out', '_last_segment_out',
                 '_pending_audio_floors')

    def __init__(self, master_url: str):
        self.master_url = master_url
        self.url_refresher = None
        self.seg_duration: float = _SEG_DURATION
        self.last_access = time.monotonic()
        self.audio_url = None
        self.variant_urls = []
        self.audio_format: 'tuple[int, int] | None' = None
        self._video_buf: '_RenditionBuffer | None' = None
        self._video_cursor_pts: 'int | None' = None
        self._audio_bufs: 'list[_RenditionBuffer]' = []
        self._audio_epoch = 0
        self._audio_disc_seqs_seen: 'set[int]' = set()
        self._audio_appended_seqs: 'set[int]' = set()
        self._video_epoch = 0
        self._next_chunk_seq = 0
        self._window_meta: 'OrderedDict[int, _WindowMeta]' = OrderedDict()
        self._last_requested_seq = 0
        self._audio_prepopulated = False
        self._audio_prefetch_thread: 'threading.Thread | None' = None
        self._audio_prefetch_stop = threading.Event()
        self._ready_segs: 'OrderedDict[int, bytes]' = OrderedDict()
        self._provisional_segs: set = set()
        self._video_prefetch_thread: 'threading.Thread | None' = None
        self._video_prefetch_stop = threading.Event()
        self._lock = threading.Lock()
        self._cc_state: dict = {}
        self._cc_resync_pending = False
        self._last_playlist_out: 'dict[int, tuple[bytes, float]]' = {}
        self._last_segment_out: 'tuple[int, bytes] | None' = None
        self._pending_audio_floors: 'list[tuple[int, int]]' = []

    def current_audio_epoch(self) -> int:
        """Thread-safe read of the current epoch, with no side effects."""
        with self._lock:
            return self._audio_epoch

    def try_start_audio_prepopulate(self) -> bool:
        """Atomically claim the one-shot audio pre-population pass.

        Returns True for the first caller only; every later call (including
        concurrent ones racing on the first request) returns False. Must be
        a real flag rather than inferring "not yet run" from buffer state
        (e.g. state._audio_bufs being empty): a fresh channel legitimately
        has no audio buffer yet for reasons unrelated to whether
        pre-population has run, and re-running this block would re-parse the
        same still-in-window #EXT-X-DISCONTINUITY tag the live audio
        pre-fetch thread already counted, double-bumping _audio_epoch for
        one real transition (see _append_audio's own dedup, which this flag
        complements at a coarser, whole-pass granularity).
        """
        with self._lock:
            if self._audio_prepopulated:
                return False
            self._audio_prepopulated = True
            return True

    def set_video_epoch(self, epoch: int):
        """Record _video_prefetch_loop's current epoch on shared state, so a
        respawned thread instance can resume from it - see
        current_video_epoch()'s own docstring. Not read by anything else:
        buffer selection/pruning today is keyed by _RenditionBuffer.epoch
        directly (see _slice_audio_for_window/_prune_audio_bufs), not by
        comparing against this mirrored value.
        """
        with self._lock:
            self._video_epoch = epoch

    def current_video_epoch(self) -> int:
        """Thread-safe read of the last-recorded video-side epoch.

        _video_prefetch_loop seeds its local video_epoch counter from this
        instead of hardcoding 0, so that a restarted thread instance (e.g.
        _handle_playlist respawning it after an uncaught exception killed
        the previous one) resumes at the epoch already recorded on this
        shared state rather than racing back from scratch. _audio_epoch
        doesn't need this because it's mutated directly on self, immune to
        the audio thread restarting; video_epoch used to be a bare local
        variable with no such persistence, which let a respawned thread's
        video_epoch=0 sit desynced from an already-advanced _audio_epoch for
        several segments until the resync-on-confirmation logic caught up
        (observed as a real, if self-healing, multi-segment silence gap).
        """
        with self._lock:
            return self._video_epoch

    def record_segment_request(self, seq: int):
        """Record that Enigma2 has actually requested segment *seq*.

        Called from _handle_segment for every request, hit or miss. Read by
        both pre-fetch threads to pace themselves against real playback
        progress - see _PREFETCH_LEAD_SEGMENTS.
        """
        with self._lock:
            self._last_requested_seq = max(self._last_requested_seq, seq)

    def last_requested_seq(self) -> int:
        """Thread-safe read of the highest segment actually requested so far."""
        with self._lock:
            return self._last_requested_seq

    def has_ready_segs(self) -> bool:
        """True once the video prefetch loop has produced anything at all.

        Lets _handle_playlist distinguish "prefetch hasn't caught up to
        this one specific new segment yet" (worth a short wait - see
        is_ready's use) from "this channel just registered and nothing has
        been fetched at all yet" (must not wait, or the very first playlist
        response would come back empty and stall live-start).
        """
        with self._lock:
            return bool(self._ready_segs)

    def ready_seg_count(self) -> int:
        """Thread-safe count of currently-produced segments.

        Used by _handle_playlist's VOD cold-start wait to hold out for
        several ready segments (see _VOD_COLD_START_MIN_SEGMENTS), not just
        the first one has_ready_segs() alone would confirm.
        """
        with self._lock:
            return len(self._ready_segs)

    def mark_provisional(self, seq: int):
        """Record that _ready_segs[seq] is a silence-muxed placeholder with a
        _resolve_late_audio_window upgrade in flight, not the final AV mux.

        _handle_segment consults this so its existing wait loop actually
        waits for the upgrade instead of returning the placeholder the
        instant it sees any bytes at all in _ready_segs[seq].

        No self-pruning here (unlike the seq-keyed dedup sets elsewhere in
        this file): *seq* is chunk_seq, this proxy's own monotonic counter,
        which - unlike a CDN media-sequence number - never wraps or repeats,
        so there's no stale-reuse hazard to guard against by age. Entries
        are removed only by the matching clear_provisional() call, which
        _resolve_late_audio_window always makes exactly once, in its
        finally block, regardless of outcome - pruning by age here as well
        would risk dropping a still-genuinely-provisional entry early
        (e.g. during a burst of several transitions close together) and
        letting _handle_segment treat an unresolved upgrade as finished.
        """
        with self._lock:
            self._provisional_segs.add(seq)

    def clear_provisional(self, seq: int):
        """Mark seq's _resolve_late_audio_window retry concluded (upgraded or not)."""
        with self._lock:
            self._provisional_segs.discard(seq)

    def add_pending_audio_floor(self, epoch: int, start_pts: int):
        """Register that a _resolve_late_audio_window retry is still waiting
        to cover [start_pts, ...) of *epoch*'s audio buffer.

        See audio_trim_floor - a later window's own successful audio match
        on the same buffer must not trim away bytes this retry still needs,
        even though the trim is otherwise perfectly safe from the later
        window's own point of view.
        """
        with self._lock:
            self._pending_audio_floors.append((epoch, start_pts))

    def remove_pending_audio_floor(self, epoch: int, start_pts: int):
        """Undo add_pending_audio_floor once a retry concludes, either way."""
        with self._lock:
            try:
                self._pending_audio_floors.remove((epoch, start_pts))
            except ValueError:
                pass

    def audio_trim_floor(self, epoch: int, requested_pts: int) -> int:
        """Clamp a trim point to never go past any still-in-flight
        _resolve_late_audio_window retry's own start_pts for *epoch*.

        Without this, _slice_audio_for_window's trim_before() call - made
        on every successful match, including one found by a *later* window
        while an *earlier* window's background retry is still polling the
        same shared buffer - can delete the earlier window's still-needed
        range out from under it. The lock inside trim_before/append
        prevents the two calls from corrupting the buffer concurrently, but
        does nothing to stop this logical data loss: the earlier retry then
        exhausts its whole wait budget and reports "no audio ever covered
        this window" even though matching audio genuinely arrived - it was
        just consumed by the later window's own trim first.

        Caller must already hold self._lock: its one call site
        (_slice_audio_for_window) reads/trims the same buffer under that
        same lock, and self._lock is a plain Lock, not an RLock - a second,
        nested acquisition here would deadlock the thread against itself,
        not just race another one.
        """
        pending = [p for e, p in self._pending_audio_floors if e == epoch]
        return min([requested_pts] + pending) if pending else requested_pts

    def close(self):
        """Signal all pre-fetch threads to stop and wait for them to exit.

        A thread only notices _audio_prefetch_stop/_video_prefetch_stop
        between blocking calls - one stuck in a slow or hanging _fetch()
        (network I/O has no hard upper bound beyond whatever timeout that
        specific call passed, which is not guaranteed to be <=5s - see
        _fetch's own docstring) won't see the stop signal until that call
        returns. join(timeout=5) can therefore expire while the thread is
        still genuinely running - previously silent, so a caller had no way
        to know cleanup didn't actually finish. Logged here instead of
        swallowed so a thread left running past this point (still holding
        whatever connection/resource its in-flight fetch is using) is at
        least visible for diagnosing exactly this class of resource leak.
        """
        self._audio_prefetch_stop.set()
        self._video_prefetch_stop.set()
        for t in (self._audio_prefetch_thread, self._video_prefetch_thread):
            if t is not None and t.is_alive():
                t.join(timeout=5)
                if t.is_alive():
                    logger.debug('%s: did not stop within 5s of close() - still running, '
                                 'likely blocked in a slow/hanging fetch; left as a detached '
                                 'daemon thread rather than blocking close() further', t.name)

    def start_audio_prefetch_if_needed(self, channel_id: str):
        """Idempotently start the audio pre-fetch thread, if not already running.

        A live viewer (_handle_master) and a recording with no live viewer
        (_ensure_pipeline) can both reach this for the same channel_id from
        independent HTTP handler threads. Without the lock covering the
        is_alive() check and the thread creation together, both could
        observe "not running yet" and each start their own thread, leaving
        two audio pre-fetch loops racing to fill the same ring.
        """
        with self._lock:
            if self._audio_prefetch_thread is not None and self._audio_prefetch_thread.is_alive():
                return
            t = threading.Thread(
                target=_audio_prefetch_loop, args=(channel_id, self), daemon=True,
                name=f'PlutoAudioPF-{channel_id[:8]}',
            )
            self._audio_prefetch_thread = t
            t.start()

    def start_video_prefetch_if_needed(self, channel_id: str, variant_idx: int):
        """Idempotently start the video pre-fetch+mux thread, if not already running.

        See start_audio_prefetch_if_needed - same race, same fix, for the
        thread that owns video_epoch. Two independent video pre-fetch
        threads for one channel would each keep their own local video_epoch
        and both call set_video_epoch(), corrupting the shared counter.
        """
        with self._lock:
            if self._video_prefetch_thread is not None and self._video_prefetch_thread.is_alive():
                return
            vt = threading.Thread(
                target=_video_prefetch_loop, args=(channel_id, self, variant_idx), daemon=True,
                name=f'PlutoVideoPF-{channel_id[:8]}',
            )
            self._video_prefetch_thread = vt
            vt.start()


_state_lock = threading.Lock()
_channels: dict = {}


def _get_state(channel_id: str) -> '_ChannelState | None':
    with _state_lock:
        state = _channels.get(channel_id)
    if state is not None:
        state.last_access = time.monotonic()
    return state


def _ensure_pipeline(channel_id: str, state: '_ChannelState') -> bool:
    """Make sure the fetch/prefetch pipeline for *channel_id* is running.

    Live playback drives /master then /pl itself (GStreamer following the
    rewritten HLS playlist), which starts the audio/video pre-fetch threads
    as a side effect of _handle_master/_handle_playlist. RecordingProxy
    connects straight to /rec/{channel_id}.ts and never touches /master or
    /pl, so a recording started with no concurrent live viewer would
    otherwise never start those threads - state._ready_segs would stay
    empty forever and the recording would get no data. This is called once
    per /rec/ connection to guarantee the pipeline is up regardless of
    whether a live viewer ever triggered it.

    Idempotent and safe to call repeatedly. Returns True if the channel
    needs muxing (demuxed audio); False if it's already a single continuous
    rendition (nothing for this proxy to do) or the master fetch failed.

    Gated on state.variant_urls being empty, not on the video prefetch
    thread's is_alive(): thread liveness alone left a narrow window - any
    /rec/ reconnect (recording retries, HTTP keep-alive drops, ...) calls
    this again, and if it landed in the brief gap between the thread dying
    and being noticed dead, execution fell through to the unconditional
    re-fetch+overwrite below, silently swapping state.audio_url/
    state.variant_urls for freshly-signed CDN URLs out from under an
    already-running live viewer's prefetch threads mid-stream - the same
    class of bug fixed in PlutoTVRequest.buildStreamURL (see
    active_channel_url), just here instead: this function only exists
    because RecordingProxy.handle_recording() calls it on every
    connection, live playback never goes through it at all.

    Once variant_urls is populated at all, restarting a dead thread needs
    no fresh fetch - start_audio_prefetch_if_needed/
    start_video_prefetch_if_needed are already independently idempotent
    (their own is_alive() check under state._lock) and the loops they
    start read state.variant_urls/state.audio_url live, so calling them
    again here with the existing values is exactly what a dead-thread
    restart needs, without touching URLs a live viewer might depend on.
    """
    if state.variant_urls:
        if state.audio_url:
            state.start_audio_prefetch_if_needed(channel_id)
        state.start_video_prefetch_if_needed(channel_id, 0)
        return True

    text = _fetch(state.master_url, tag='ensure-pipeline:master')
    if not text and _has_empty_jwt(state.master_url) and state.url_refresher:
        fresh_url = state.url_refresher()
        if fresh_url:
            state.master_url = fresh_url
            text = _fetch(state.master_url, tag='ensure-pipeline:master-refreshed')
    if not text:
        return False

    needs_mux = any(
        line.startswith('#EXT-X-MEDIA:') and 'TYPE=AUDIO' in line
        for line in text.splitlines()
    )
    if not needs_mux:
        return False

    master_base = state.master_url.split('?')[0].rsplit('/', 1)[0] + '/'
    audio_candidates = []
    variant_urls = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('#EXT-X-MEDIA:'):
            if 'TYPE=AUDIO' in line:
                cand = HLSPlaylist.audio_candidate(line, master_base)
                if cand:
                    audio_candidates.append(cand)
            i += 1
            continue
        if line.startswith('#EXT-X-STREAM-INF:'):
            i += 1
            if i < len(lines):
                variant_urls.append(urljoin(master_base, lines[i]))
                i += 1
            continue
        i += 1

    audio_url = HLSPlaylist.select_primary_audio(audio_candidates)
    with state._lock:
        state.audio_url = audio_url
        state.variant_urls = variant_urls

    if audio_url:
        state.start_audio_prefetch_if_needed(channel_id)

    if variant_urls:
        state.start_video_prefetch_if_needed(channel_id, 0)

    logger.debug('%s: recording-only bootstrap started prefetch pipeline (%s variant(s))',
                 channel_id, len(variant_urls))
    return True


def _prune_audio_bufs(state: '_ChannelState'):
    """Bound state._audio_bufs to the current epoch's buffer plus
    _AUDIO_BUFFER_RETAIN_EPOCHS closed ones, additionally dropping any
    closed buffer older than seg_duration * _LATE_AUDIO_WAIT_FACTOR -
    mirrors the depth/age bounds the old audio ring (_MAX_AUDIO_RING /
    _MAX_AUDIO_EPOCH_LAG / add_audio's max_age pruning) used to enforce.

    Also caps every buffer - open or closed - to _MAX_AUDIO_BUFFER_BYTES
    regardless of whether it's ever been successfully matched: the
    depth/age bounds above only ever apply to a *closed* buffer, and
    trim_before() only ever shrinks one in response to a successful cut,
    so the *current* (open) buffer would otherwise grow without bound for
    as long as it keeps failing to match anything (see
    _MAX_AUDIO_EPOCH_LAG's resync, which should normally prevent that
    situation from lasting long enough for this cap to matter at all).

    Caller must hold state._lock.
    """
    for b in state._audio_bufs:
        b.trim_to_max_bytes(_MAX_AUDIO_BUFFER_BYTES)
    max_age = state.seg_duration * _LATE_AUDIO_WAIT_FACTOR
    now = time.monotonic()
    kept = []
    closed_kept = 0
    for b in state._audio_bufs:
        if b.closed_at is None:
            kept.append(b)
            continue
        if now - b.closed_at > max_age:
            continue
        if closed_kept >= _AUDIO_BUFFER_RETAIN_EPOCHS:
            continue
        kept.append(b)
        closed_kept += 1
    state._audio_bufs = kept


def _append_audio(state: '_ChannelState', channel_id: str, seq: int, disc: bool, data: bytes):
    """Append a freshly-fetched, decrypted audio segment's bytes into the
    current (or a freshly-opened) audio _RenditionBuffer.

    Mirrors the old assign_audio_epoch's exactly-once-per-seq disc dedup
    (see _SEQ_REPROCESS_WINDOW) via state._audio_disc_seqs_seen, and the old
    add_audio's already-queued dedup (both the one-shot playlist
    pre-population pass and the ongoing background audio pre-fetch thread
    parse the same playlist independently and both start from the first
    segment) via state._audio_appended_seqs - without the latter, the two
    could append the same seq's bytes twice into a buffer, corrupting its
    PTS index.

    Callers must invoke this in strictly ascending *seq* order relative to
    each other (see _audio_prefetch_loop's and _handle_playlist's own
    ordering) - _RenditionBuffer.append requires appends to arrive in PTS
    order to keep its index valid; unlike the old flat, order-independent
    audio ring, a continuous buffer cannot tolerate out-of-order writes.

    The expensive PTS/PAT-PMT scan runs before state._lock is taken (see
    _scan_ts_index's own docstring for why).
    """
    pts_offsets, pat_pmt = _scan_ts_index(data)
    if not pts_offsets:
        logger.debug('%s/%s: Event: no PTS found in audio segment'
                     ' | Action: drop, not appended to buffer', channel_id, seq)
        return
    with state._lock:
        if seq in state._audio_appended_seqs:
            return
        state._audio_appended_seqs.add(seq)
        state._audio_appended_seqs = {
            s for s in state._audio_appended_seqs if s >= seq - _SEQ_REPROCESS_WINDOW
        }
        if disc and seq not in state._audio_disc_seqs_seen:
            state._audio_epoch += 1
            for b in state._audio_bufs:
                b.mark_closed()
            state._audio_bufs.insert(0, _RenditionBuffer(state._audio_epoch, is_disc_epoch=True))
        state._audio_disc_seqs_seen.add(seq)
        state._audio_disc_seqs_seen = {
            s for s in state._audio_disc_seqs_seen if s >= seq - _SEQ_REPROCESS_WINDOW
        }
        if not state._audio_bufs:
            state._audio_bufs.insert(0, _RenditionBuffer(state._audio_epoch, is_disc_epoch=False))
        state._audio_bufs[0].append(data, pts_offsets, pat_pmt)
        _prune_audio_bufs(state)


def _slice_audio_for_window(state: '_ChannelState', epoch: int, start_pts: int, end_pts: int,
                            is_disc_window: bool, wait_secs: 'float | None' = None,
                            channel_id: str = '', chunk_seq: 'int | None' = None
                            ) -> 'tuple[bytes | None, int | None, bool]':
    """Slice audio bytes covering [start_pts, end_pts) for one output window.

    Replaces pop_audio_for_pts's discrete nearest-match search: searches
    state._audio_bufs for a buffer tagged *epoch* (or one epoch back,
    unless *is_disc_window* - the same reset-PTS coincidence-risk exemption
    pop_audio_for_pts gated via allow_stale_epoch) whose continuous PTS
    index already covers the full requested range, waiting up to
    *wait_secs* for a buffer to catch up. *is_disc_window* only affects the
    stale-epoch gating here - callers pass their own explicit *wait_secs*
    for the disc case (see _cut_and_mux_next_window's short bounded
    _DISC_AUDIO_PEEK_WAIT and _resolve_late_audio_window's much longer
    _LATE_AUDIO_WAIT_FACTOR budget); the state.seg_duration*1.6 default
    below only actually applies to an ordinary, non-disc window whose
    caller didn't specify one. Returns (sliced_bytes, actual_a_start_pts,
    True) on success - the middle value lets a caller feed _ffmpeg_mux's
    itsoffset calculation directly instead of re-deriving it with a second
    _first_pts scan over bytes already fully indexed here - or
    (None, None, False) on timeout.

    Unlike the old segment-granularity match, there is no "nearest but
    outside tolerance" concept here: either some buffer's continuous index
    already spans the exact range needed, or it doesn't yet (worth
    retrying) or never will (a genuine content gap - see silence-injection
    fallback in _ffmpeg_mux).
    """
    if wait_secs is None:
        wait_secs = state.seg_duration * 1.6
    deadline = time.monotonic() + wait_secs
    snapshot = ''
    while True:
        with state._lock:
            candidates = [b for b in state._audio_bufs if b.epoch == epoch]
            if not is_disc_window:
                candidates += [b for b in state._audio_bufs if b.epoch == epoch - 1]
            for b in candidates:
                cut = b.cut(start_pts, end_pts)
                if cut is not None:
                    data, a_start, _a_end = cut
                    b.trim_before(state.audio_trim_floor(b.epoch, a_start))
                    if channel_id:
                        stale = ' (stale-epoch fallback)' if b.epoch != epoch else ''
                        logger.debug('%s/%s: audio window matched epoch=%s pts=[%s,%s)%s',
                                     channel_id, chunk_seq, b.epoch, start_pts, end_pts, stale)
                    return data, a_start, True
            snapshot = ', '.join(
                f'epoch={b.epoch} pts=[{b.pts_list[0]},{b.pts_list[-1]}] n={len(b.pts_list)}'
                if b.pts_list else f'epoch={b.epoch} empty'
                for b in state._audio_bufs
            ) or 'no buffers held'
        if time.monotonic() >= deadline:
            if channel_id:
                logger.debug('%s/%s: audio MISS window epoch=%s pts=[%s,%s) waited=%.2fs'
                             ' is_disc_window=%s buffers=[%s]',
                             channel_id, chunk_seq, epoch, start_pts, end_pts, wait_secs,
                             is_disc_window, snapshot)
            return None, None, False
        time.sleep(0.1)


def _resolve_late_audio_window(channel_id: str, state: '_ChannelState', chunk_seq: int,
                               video_slice: bytes, epoch: int, start_pts: int, end_pts: int,
                               is_disc_window: bool):
    """Background retry for a window whose audio wasn't covered yet at cut
    time - replaces _resolve_late_audio, retargeted at a PTS range instead
    of a single discrete segment match.

    Runs off the critical path with the extended _LATE_AUDIO_WAIT_FACTOR
    budget, giving the audio rendition's own disc tag (or just ordinary CDN
    fetch lag) extra time to catch up; if the range becomes coverable in
    time and Enigma2 hasn't already been served the silence version, it
    re-muxes and overwrites state._ready_segs[chunk_seq] in place.
    """
    try:
        aslice, a_pts, covered = _slice_audio_for_window(
            state, epoch, start_pts, end_pts, is_disc_window,
            wait_secs=state.seg_duration * _LATE_AUDIO_WAIT_FACTOR,
            channel_id=channel_id, chunk_seq=chunk_seq)
        with state._lock:
            if chunk_seq not in state._ready_segs:
                return
        if not covered:
            logger.debug('%s/%s: Event: no audio ever covered this window'
                         ' (kept as silence) | epoch=%s pts=[%s,%s)',
                         channel_id, chunk_seq, epoch, start_pts, end_pts)
            return
        muxed = _ffmpeg_mux(video_slice, aslice, state, is_disc=is_disc_window, track_cc=False,
                            v_pts=start_pts, a_pts=a_pts)
        with state._lock:
            if chunk_seq in state._ready_segs:
                state._ready_segs[chunk_seq] = muxed
                state._cc_resync_pending = True
                logger.debug('%s/%s: late-audio window upgrade applied', channel_id, chunk_seq)
    finally:
        state.clear_provisional(chunk_seq)
        state.remove_pending_audio_floor(epoch, start_pts)


def _cut_and_mux_next_window(channel_id: str, state: '_ChannelState') -> bool:
    """Cut one output window covering everything currently buffered in
    state._video_buf from the cursor on, slice matching audio, mux, and
    publish to state._ready_segs.

    Replaces the old per-CDN-segment mux call in _video_prefetch_loop.
    Deliberately cuts by *CDN segment boundary*, not by a fixed target
    duration: an HLS video segment is guaranteed self-contained (its own
    SPS/PPS and a leading IDR/keyframe), which is exactly why the old
    per-segment design never had a decode problem on the video side in the
    first place. Cutting at an arbitrary fixed-duration PTS boundary
    instead (an earlier version of this function did exactly that) breaks
    that guarantee: a window frequently starts mid-GOP with no leading
    keyframe, and sometimes with no SPS/PPS NAL in range at all - confirmed
    in production as periodic video freezes (decoder has nothing
    displayable until the next real keyframe) and outright _ffmpeg_mux
    failures ("non-existing PPS 0 referenced", rc=234, 0 bytes output,
    silently falling back to video-only - the exact video_data-only path
    _ffmpeg_mux's docstring already flags as worse than the mismatch it
    exists to paper over). Cutting at CDN segment boundaries restores that
    guarantee while still sourcing audio from the continuous buffer for
    whatever exact PTS range this video segment covers - audio no longer
    needs to be a matching *discrete segment*, just a PTS-range slice,
    which is the actual fix the windowed-buffer redesign was for; nothing
    about that benefit depends on how video's own boundaries are chosen.

    state._video_buf/_video_cursor_pts are touched only by the video
    prefetch thread (this function is only ever called synchronously from
    that same thread), so - like state._cc_state - they need no lock of
    their own; only the state._ready_segs/_window_meta/_next_chunk_seq
    publish step below is shared with HTTP handler threads and
    _resolve_late_audio_window, and is locked accordingly.

    Returns True if a window was produced, False if there was nothing new
    to cut (e.g. called again with no new append since the last call).
    """
    vbuf = state._video_buf
    cursor = state._video_cursor_pts
    if vbuf is None or cursor is None:
        return False
    cut = vbuf.cut(cursor, None)
    if cut is None:
        return False
    vslice, actual_start, actual_end = cut
    if actual_end <= actual_start:
        return False

    is_disc = vbuf.is_disc_epoch and not vbuf.first_window_emitted
    vbuf.first_window_emitted = True

    aslice, a_pts, covered = _slice_audio_for_window(
        state, vbuf.epoch, actual_start, actual_end, is_disc_window=is_disc,
        wait_secs=(_DISC_AUDIO_PEEK_WAIT if is_disc else None),
        channel_id=channel_id)

    reset_cc = is_disc
    with state._lock:
        if state._cc_resync_pending:
            reset_cc = True
            state._cc_resync_pending = False
    muxed = _ffmpeg_mux(vslice, aslice, state, is_disc=is_disc, track_cc=True, reset_cc=reset_cc,
                        v_pts=actual_start, a_pts=a_pts)

    with state._lock:
        chunk_seq = state._next_chunk_seq
        state._next_chunk_seq += 1
        state._ready_segs[chunk_seq] = muxed
        state._window_meta[chunk_seq] = _WindowMeta(vbuf.epoch, actual_start, actual_end, is_disc)
        while len(state._ready_segs) > _MAX_SEG_CACHE:
            k, _ = state._ready_segs.popitem(last=False)
            state._window_meta.pop(k, None)

    logger.debug('%s/%s: Event: window cut epoch=%s pts=[%s,%s) is_disc=%s audio_covered=%s'
                 ' | Action: mux (%s bytes)',
                 channel_id, chunk_seq, vbuf.epoch, actual_start, actual_end, is_disc, covered,
                 len(muxed))

    if not covered:
        state.mark_provisional(chunk_seq)
        state.add_pending_audio_floor(vbuf.epoch, actual_start)
        threading.Thread(
            target=_resolve_late_audio_window,
            args=(channel_id, state, chunk_seq, vslice, vbuf.epoch, actual_start, actual_end, is_disc),
            daemon=True,
            name=f'PlutoLateAudio-{channel_id[:8]}-{chunk_seq}',
        ).start()

        audio_epoch_now = state.current_audio_epoch()
        if audio_epoch_now > vbuf.epoch:
            logger.debug('%s/%s: Event: audio_epoch already ahead of video_epoch'
                         ' (video rendition never flagged this transition) | Action:'
                         ' resync video_epoch %s -> %s',
                         channel_id, chunk_seq, vbuf.epoch, audio_epoch_now)
            vbuf.mark_closed()
            state._video_buf = _RenditionBuffer(audio_epoch_now, is_disc_epoch=False)
            state._video_cursor_pts = None
            state.set_video_epoch(audio_epoch_now)
            with state._lock:
                state._cc_resync_pending = True
            return True
        if vbuf.epoch - audio_epoch_now >= _MAX_AUDIO_EPOCH_LAG:
            logger.debug('%s/%s: Event: audio_epoch persistently behind video_epoch'
                         ' by >= %s (missed audio-side disc marker) | Action:'
                         ' resync video_epoch %s -> %s',
                         channel_id, chunk_seq, _MAX_AUDIO_EPOCH_LAG, vbuf.epoch, audio_epoch_now)
            vbuf.mark_closed()
            state._video_buf = _RenditionBuffer(audio_epoch_now, is_disc_epoch=False)
            state._video_cursor_pts = None
            state.set_video_epoch(audio_epoch_now)
            with state._lock:
                state._cc_resync_pending = True
            return True

    state._video_cursor_pts = vbuf.pts_list[-1]
    vbuf.trim_before(actual_start, retain_bytes=_VIDEO_BUFFER_RETAIN_MARGIN_BYTES)
    return True


def _estimate_v_end_pts(all_pts: list) -> int:
    """Estimate a VOD video segment's own end PTS from its own frame
    spacing (the *most common* gap between consecutive distinct PTS
    values, sorted - robust to B-frame transmission-order reordering).

    Only a fallback for when no subsequent segment's real PTS is
    available to give an exact boundary instead - the final segment of a
    VOD asset, or one immediately preceding a real CDN discontinuity.
    Confirmed by directly comparing consecutive real windows' own logged
    PTS ranges in a production session that this estimate systematically
    overshoots the next segment's true start by ~40-55ms on *every*
    boundary - not occasional noise, every single one - which is exactly
    why _video_prefetch_loop now defers muxing a segment until its actual
    successor's own v_pts is known, using this only where no successor
    will ever exist to ask instead.
    """
    distinct_sorted = sorted(set(all_pts))
    if len(distinct_sorted) > 1:
        deltas = [b - a for a, b in zip(distinct_sorted, distinct_sorted[1:])]
        delta_counts: dict = {}
        for _d in deltas:
            delta_counts[_d] = delta_counts.get(_d, 0) + 1
        frame_interval = max(delta_counts, key=delta_counts.get)
    else:
        frame_interval = 1
    return max(all_pts) + frame_interval


def _mux_whole_video_segment(channel_id: str, state: '_ChannelState', epoch: int,
                             vdata: bytes, v_pts: int, v_end_pts: int, is_disc: bool,
                             pending_disc_ahead: bool = False):
    """VOD-only alternative to _cut_and_mux_next_window: mux one whole CDN
    video segment as its own output window, instead of slicing a sub-range
    out of a continuous buffer via _RenditionBuffer.cut().

    _RenditionBuffer.cut() locates its byte range with bisect() over
    pts_list, which requires pts_list to be sorted - true for audio (AAC
    has no B-frames, so transmission order already equals display order)
    but not guaranteed for H.264 video with B-frames: a P-frame is
    transmitted before the B-frames that display earlier than it, so a
    video segment's own pts_offsets (as scanned by _scan_ts_index, in
    transmission order) can be locally out of order. Confirmed on real
    Pluto content via a diagnostic in _RenditionBuffer.append(): up to
    roughly half the PTS entries in a single segment out of order, on both
    live and VOD channels equally. bisect() on unsorted input doesn't
    raise - it silently returns a technically-computed but wrong index,
    slicing the wrong byte range. Only VOD is switched to this whole-
    segment path (see the call site in _video_prefetch_loop) rather than
    fixing _RenditionBuffer.cut() itself, to keep live TV's already-working
    code path completely untouched.

    None of this needs fixing in the first place for the common case this
    function actually handles: in steady state, _video_prefetch_loop calls
    this once per newly-fetched CDN segment, and an HLS segment is already
    self-contained (its own SPS/PPS + leading keyframe) - there is no
    reason to slice it out of a bigger buffer by PTS at all. Muxing it
    whole, exactly as fetched, sidesteps the bisect dependency entirely.

    *v_pts* and *v_end_pts* must be this segment's true min/max PTS
    (order-independent - see the B-frame reasoning above; the caller
    computes both via min()/max() over every entry in this segment's own
    pts_offsets), not an approximated span. An earlier version of this
    function derived v_end_pts as v_pts + one nominal seg_duration instead
    - which is exactly the kind of imprecision the windowed-buffer redesign
    (see _cut_and_mux_next_window's own docstring on why it replaced the
    old per-segment design's tolerance-based matching with exact PTS-range
    coverage) moved away from for good reason: a real CDN segment's true
    span is never exactly the nominal target duration, so that
    approximation systematically over- or under-covers the audio query by
    a small amount on *every* window. cut()'s exact-range matching still
    "succeeds" either way (over-coverage doesn't fail the match, it just
    slices more bytes), so nothing here ever shows up as a miss or a large
    per-window itsoffset - but over hundreds of windows in a long VOD
    title, a systematic few-millisecond-per-window bias compounds into a
    multi-second, steadily growing A/V offset that survives seeking
    (every window carries the same bias) and never self-corrects (nothing
    re-anchors to true content position - each window's own start is
    exact, via min(), but its end, and therefore how much of the *next*
    window's rightful audio bleeds into this one, was not). Using this
    segment's own true max() instead removes the systematic bias entirely.

    Mirrors _cut_and_mux_next_window's own audio-matching, muxing,
    publishing, provisional/late-audio-retry, and epoch-resync logic
    exactly (see that function's comments for the reasoning behind each),
    just operating on a whole fetched segment instead of a buffer slice.
    """
    aslice, a_pts, covered = _slice_audio_for_window(
        state, epoch, v_pts, v_end_pts, is_disc_window=is_disc,
        wait_secs=(_DISC_AUDIO_PEEK_WAIT if is_disc else None),
        channel_id=channel_id)

    reset_cc = is_disc
    with state._lock:
        if state._cc_resync_pending:
            reset_cc = True
            state._cc_resync_pending = False
    muxed = _ffmpeg_mux(vdata, aslice, state, is_disc=is_disc, track_cc=True, reset_cc=reset_cc,
                        v_pts=v_pts, a_pts=a_pts)

    with state._lock:
        chunk_seq = state._next_chunk_seq
        state._next_chunk_seq += 1
        state._ready_segs[chunk_seq] = muxed
        state._window_meta[chunk_seq] = _WindowMeta(epoch, v_pts, v_end_pts, is_disc)
        while len(state._ready_segs) > _MAX_SEG_CACHE:
            k, _ = state._ready_segs.popitem(last=False)
            state._window_meta.pop(k, None)

    logger.debug('%s/%s: Event: whole-segment window epoch=%s pts=[%s,%s) is_disc=%s'
                 ' audio_covered=%s | Action: mux (%s bytes)',
                 channel_id, chunk_seq, epoch, v_pts, v_end_pts, is_disc, covered, len(muxed))

    if not covered:
        state.mark_provisional(chunk_seq)
        state.add_pending_audio_floor(epoch, v_pts)
        threading.Thread(
            target=_resolve_late_audio_window,
            args=(channel_id, state, chunk_seq, vdata, epoch, v_pts, v_end_pts, is_disc),
            daemon=True,
            name=f'PlutoLateAudio-{channel_id[:8]}-{chunk_seq}',
        ).start()

        audio_epoch_now = state.current_audio_epoch()
        if audio_epoch_now > epoch and not pending_disc_ahead:
            logger.debug('%s/%s: Event: audio_epoch already ahead of video_epoch'
                         ' (video rendition never flagged this transition) | Action:'
                         ' resync video_epoch %s -> %s',
                         channel_id, chunk_seq, epoch, audio_epoch_now)
            state.set_video_epoch(audio_epoch_now)
            with state._lock:
                state._cc_resync_pending = True
        elif epoch - audio_epoch_now >= _MAX_AUDIO_EPOCH_LAG:
            logger.debug('%s/%s: Event: audio_epoch persistently behind video_epoch'
                         ' by >= %s (missed audio-side disc marker) | Action:'
                         ' resync video_epoch %s -> %s',
                         channel_id, chunk_seq, _MAX_AUDIO_EPOCH_LAG, epoch, audio_epoch_now)
            state.set_video_epoch(audio_epoch_now)
            with state._lock:
                state._cc_resync_pending = True


def _audio_prefetch_loop(channel_id: str, state: '_ChannelState'):
    """Background thread: continuously fetch new audio segments and append
    them, in seq order, to the per-channel continuous audio buffer(s).

    Polls the audio rendition playlist, fetches any segments not yet
    appended, decrypts them if necessary, and hands each to _append_audio
    (which resolves/bumps the audio epoch and opens a fresh _RenditionBuffer
    on a real #EXT-X-DISCONTINUITY). Segments are fetched in parallel via
    _AUDIO_FETCH_WORKERS, but appended to the buffer strictly in seq order
    once the whole poll's batch has finished fetching - _RenditionBuffer's
    PTS index requires ascending-PTS appends to stay valid, unlike the old
    flat, order-independent audio ring, which tolerated parallel workers
    finishing in any order.
    """
    fetched_seqs: set[int] = set()
    stale_aseq_base: 'int | None' = None
    stale_since: 'float | None' = None

    while True:
        if state._audio_prefetch_stop.is_set():
            break
        audio_url = state.audio_url
        if not audio_url:
            time.sleep(0.1)
            continue

        atext = _fetch(audio_url, tag='audio-prefetch:playlist')
        if not atext:
            continue

        ad_markers = HLSPlaylist.ad_markers(atext)
        if ad_markers:
            logger.debug('%s: audio playlist ad markers: %s', channel_id, ad_markers)

        abase = audio_url.split('?')[0].rsplit('/', 1)[0] + '/'
        try:
            aseq_base, asegs = HLSPlaylist.segments(atext, abase)
            audio_key_urls, audio_ivs, audio_discs = HLSPlaylist.segment_keys(atext, abase)
        except Exception as exc:
            logger.debug('%s: audio playlist parse error: %s', channel_id, exc)
            continue

        state.record_segment_request(max(0, aseq_base - 1))

        fetched_seqs = {s for s in fetched_seqs if s >= aseq_base - _SEQ_REPROCESS_WINDOW}

        last_seq_ok = state.last_requested_seq() + _PREFETCH_LEAD_SEGMENTS
        to_fetch = []
        bound_hit_at = None
        for i, url in enumerate(asegs):
            seq = aseq_base + i
            if seq > last_seq_ok:
                bound_hit_at = seq
                break
            if seq not in fetched_seqs:
                k = audio_key_urls[i] if i < len(audio_key_urls) else None
                iv = audio_ivs[i] if i < len(audio_ivs) else None
                disc = audio_discs[i] if i < len(audio_discs) else False
                to_fetch.append((seq, url, k, iv, disc))

        if not to_fetch:
            if bound_hit_at is not None:
                logger.debug('%s: Event: audio prefetch withheld by lead bound'
                             ' - aseq_base=%s bound_hit_at=%s last_seq_ok=%s'
                             ' last_requested_seq=%s | Action: pair with silence until this clears',
                             channel_id, aseq_base, bound_hit_at, last_seq_ok,
                             state.last_requested_seq())
            if aseq_base != stale_aseq_base:
                stale_aseq_base = aseq_base
                stale_since = time.monotonic()
            elif time.monotonic() - stale_since >= state.seg_duration * 3:
                logger.debug('%s: Event: audio playlist media sequence stuck'
                             ' at aseq_base=%s for %.1fs | Action: clear fetched_seqs and retry',
                             channel_id, aseq_base, time.monotonic() - stale_since)
                fetched_seqs.clear()
                stale_aseq_base = None
                stale_since = None
            if state._audio_prefetch_stop.wait(timeout=state.seg_duration / 5):
                break
            continue

        stale_aseq_base = None
        stale_since = None

        results: dict = {}

        def _fetch_one(seq, url, key_url, iv_explicit):
            fail_reason = None
            for _attempt in range(2):
                try:
                    adata = _fetch(url, binary=True, timeout=state.seg_duration * 0.8,
                                   tag='audio-prefetch:segment')
                    if not adata:
                        fail_reason = 'segment fetch returned no data'
                        continue
                    if key_url:
                        kb = AES128.fetch_key(key_url, _fetch)
                        if not kb:
                            fail_reason = f'key fetch failed for {key_url}'
                            continue
                        adata = AES128.decrypt(adata, kb, AES128.iv(iv_explicit, seq))
                    if not adata or adata[0] != 0x47:
                        fail_reason = 'failed to decrypt/parse (bad sync byte)'
                        continue
                    if not _scan_ts_index(adata)[0]:
                        fail_reason = 'no PTS found in decrypted segment'
                        continue
                    if _attempt > 0:
                        logger.debug('%s/%s: Event: audio segment recovered on retry'
                                     ' (attempt 1 failed: %s)', channel_id, seq, fail_reason)
                    results[seq] = adata
                    return
                except Exception as exc:
                    fail_reason = f'{type(exc).__name__}: {exc}'
                    continue
            logger.debug('%s/%s: Event: audio segment unavailable after retry (%s)'
                         ' | Action: drop this segment, next poll will retry',
                         channel_id, seq, fail_reason)

        workers = min(_AUDIO_FETCH_WORKERS, len(to_fetch))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for seq, url, k, iv, _disc in to_fetch:
                pool.submit(_fetch_one, seq, url, k, iv)
        for seq, _url, _k, _iv, disc in to_fetch:
            adata = results.get(seq)
            if adata is None:
                continue
            _append_audio(state, channel_id, seq, disc, adata)
            fetched_seqs.add(seq)

        if state._audio_prefetch_stop.wait(timeout=state.seg_duration / 5):
            break


def _video_prefetch_loop(channel_id: str, state: '_ChannelState',
                         variant_idx: int):
    """Background thread: pre-fetch video segments into a continuous
    per-epoch buffer, and cut+mux fixed-duration output windows from it as
    soon as each window is fully buffered.

    Polls the video variant playlist, fetches each new segment (decrypting
    as needed), appends its bytes to state._video_buf (flushing any partial
    window and opening a fresh buffer at every real CDN discontinuity), and
    drains _cut_and_mux_next_window in a loop after each append - which
    slices whatever audio buffer covers the same PTS range (see
    _slice_audio_for_window) and stores the muxed result in
    state._ready_segs, keyed by an internal chunk_seq rather than the CDN's
    own media-sequence number. _handle_segment then serves from that cache
    with no blocking wait in the steady-state case.

    Combined with _handle_playlist's own readiness check (withholding a
    chunk_seq from the served playlist until this loop has actually
    produced it), the muxed window is normally ready well before Enigma2 is
    allowed to request it. At a real content transition where the matching
    audio hasn't been buffered yet, _resolve_late_audio_window's extended
    retry and _handle_segment's own deadline are what give that case a real
    chance to land in time too.

    variant_idx is re-resolved against state.variant_urls on every poll so
    that CDN URL rotations (signed-URL token refresh) are picked up without
    restarting the thread.
    """
    is_vod = channel_id.startswith('vod')
    vod_pending_video: 'dict | None' = None
    fetched_seqs: set = set()
    bumped_disc_seqs: set = set()
    video_epoch = state.current_video_epoch()
    with state._lock:
        state._video_buf = _RenditionBuffer(video_epoch, is_disc_epoch=True)
        state._video_cursor_pts = None
        state._cc_resync_pending = True

    while True:
        if state._video_prefetch_stop.is_set():
            break

        urls = state.variant_urls
        if variant_idx >= len(urls):
            if state._video_prefetch_stop.wait(timeout=state.seg_duration / 5):
                break
            continue
        variant_url = urls[variant_idx]

        vtext = _fetch(variant_url, tag='video-prefetch:playlist')
        if not vtext:
            if state._video_prefetch_stop.wait(timeout=state.seg_duration / 5):
                break
            continue

        ad_markers = HLSPlaylist.ad_markers(vtext)
        if ad_markers:
            logger.debug('%s: video playlist ad markers: %s', channel_id, ad_markers)

        vbase = variant_url.split('?')[0].rsplit('/', 1)[0] + '/'
        try:
            vseq, vsegs = HLSPlaylist.segments(vtext, vbase)
            all_key_urls, all_ivs, all_discs = HLSPlaylist.segment_keys(vtext, vbase)
        except Exception:
            if state._video_prefetch_stop.wait(timeout=state.seg_duration / 5):
                break
            continue

        state.record_segment_request(max(0, vseq - 1))

        fetched_seqs = {s for s in fetched_seqs if s >= vseq - _SEQ_REPROCESS_WINDOW}
        bumped_disc_seqs = {s for s in bumped_disc_seqs if s >= vseq - _SEQ_REPROCESS_WINDOW}

        _fill_key = all_key_urls[-1] if all_key_urls else None
        _fill_iv = all_ivs[-1] if all_ivs else None
        while len(all_key_urls) < len(vsegs):
            all_key_urls.append(_fill_key)
            all_ivs.append(_fill_iv)
        while len(all_discs) < len(vsegs):
            all_discs.append(False)

        new_seqs = 0
        for i, url in enumerate(vsegs):
            seq = vseq + i
            if seq < 0:
                logger.debug('%s/%s: Event: negative sequence number'
                             ' from CDN playlist | Action: skip segment, wait for next poll',
                             channel_id, seq)
                continue
            if seq in fetched_seqs:
                continue
            if state._video_prefetch_stop.is_set():
                break

            pace_wait_start = None
            while seq > state.last_requested_seq() + _PREFETCH_LEAD_SEGMENTS:
                if pace_wait_start is None:
                    pace_wait_start = time.monotonic()
                    logger.debug('%s/%s: pacing - last requested seq is %s,'
                                 ' waiting for it to reach %s before fetching further ahead',
                                 channel_id, seq, state.last_requested_seq(),
                                 seq - _PREFETCH_LEAD_SEGMENTS)
                if state._video_prefetch_stop.wait(timeout=0.5):
                    break
            if pace_wait_start is not None:
                logger.debug('%s/%s: pacing wait ended after %.1fs'
                             ' (last requested seq now %s)',
                             channel_id, seq, time.monotonic() - pace_wait_start,
                             state.last_requested_seq())
            if state._video_prefetch_stop.is_set():
                break

            is_disc = all_discs[i]
            pending_disc_ahead = is_vod and any(
                all_discs[i + 1:i + 1 + _DISC_LOOKAHEAD_SEGMENTS])
            if is_disc and seq not in bumped_disc_seqs:
                bumped_disc_seqs.add(seq)
                if is_vod and vod_pending_video is not None:
                    # pylint: disable=unsubscriptable-object
                    _mux_whole_video_segment(
                        channel_id, state, video_epoch,
                        vod_pending_video['vdata'], vod_pending_video['v_pts'],
                        _estimate_v_end_pts(vod_pending_video['all_pts']),
                        vod_pending_video['is_disc'], vod_pending_video['pending_disc_ahead'])
                    # pylint: enable=unsubscriptable-object
                    vod_pending_video = None
                    video_epoch = state.current_video_epoch()
                video_epoch += 1
                state.set_video_epoch(video_epoch)
                logger.debug('%s/%s: Event: CDN discontinuity tag | Action:'
                             ' flush partial window, video_epoch -> %s (audio_epoch=%s)',
                             channel_id, seq, video_epoch, state.current_audio_epoch())
                while _cut_and_mux_next_window(channel_id, state):
                    pass
                video_epoch = state.current_video_epoch()
                state._video_buf.mark_closed()
                state._video_buf = _RenditionBuffer(video_epoch, is_disc_epoch=True)
                state._video_cursor_pts = None

            vdata = _fetch(url, binary=True, timeout=state.seg_duration * 0.8,
                           tag='video-prefetch:segment')
            if not vdata:
                continue

            key_url = all_key_urls[i] if i < len(all_key_urls) else None
            iv_explicit = all_ivs[i] if i < len(all_ivs) else None
            if key_url:
                kb = AES128.fetch_key(key_url, _fetch)
                if kb:
                    vdata = AES128.decrypt(vdata, kb,
                                           AES128.iv(iv_explicit, seq))
            if not vdata or vdata[0] != 0x47:
                continue

            pts_offsets, pat_pmt = _scan_ts_index(vdata)
            if not pts_offsets:
                logger.debug('%s/%s: Event: no PTS found in video segment'
                             ' | Action: mux standalone (approx PTS), skip buffer',
                             channel_id, seq)
                if is_vod:
                    approx_start = vod_pending_video['v_pts'] if vod_pending_video else None
                else:
                    approx_start = state._video_cursor_pts
                    if approx_start is None and state._video_buf.pts_list:
                        approx_start = state._video_buf.pts_list[-1]
                if approx_start is None:
                    approx_start = 0
                approx_end = approx_start + int(state.seg_duration * 90000)
                muxed = _ffmpeg_mux(vdata, None, state, is_disc=False, track_cc=True,
                                    v_pts=approx_start)
                with state._lock:
                    chunk_seq = state._next_chunk_seq
                    state._next_chunk_seq += 1
                    state._ready_segs[chunk_seq] = muxed
                    state._window_meta[chunk_seq] = _WindowMeta(
                        video_epoch, approx_start, approx_end, False)
                    while len(state._ready_segs) > _MAX_SEG_CACHE:
                        k, _ = state._ready_segs.popitem(last=False)
                        state._window_meta.pop(k, None)
                fetched_seqs.add(seq)
                continue

            if is_vod:
                all_pts = [p for _, p in pts_offsets]
                v_pts = min(all_pts)
                # pylint: disable-next=unsubscriptable-object
                if vod_pending_video is not None and v_pts == vod_pending_video['v_pts']:
                    logger.debug('%s/%s: Event: duplicate video PTS (CDN frame-repeat'
                                 ' at splice) v_pts=%s | Action: drop, no window muxed',
                                 channel_id, seq, v_pts)
                    fetched_seqs.add(seq)
                    continue
                if vod_pending_video is not None:
                    _mux_whole_video_segment(
                        channel_id, state, video_epoch,
                        vod_pending_video['vdata'], vod_pending_video['v_pts'], v_pts,
                        vod_pending_video['is_disc'], vod_pending_video['pending_disc_ahead'])
                    video_epoch = state.current_video_epoch()
                vod_pending_video = {
                    'vdata': vdata, 'v_pts': v_pts, 'all_pts': all_pts,
                    'is_disc': is_disc, 'pending_disc_ahead': pending_disc_ahead,
                }
                fetched_seqs.add(seq)
                new_seqs += 1
                continue

            v_pts = pts_offsets[0][1]

            vbuf = state._video_buf
            if vbuf.pts_list and v_pts == vbuf.pts_list[-1]:
                logger.debug('%s/%s: Event: duplicate video PTS (CDN frame-repeat'
                             ' at splice) v_pts=%s | Action: drop, not appended to buffer',
                             channel_id, seq, v_pts)
                fetched_seqs.add(seq)
                continue

            vbuf.append(vdata, pts_offsets, pat_pmt)
            if state._video_cursor_pts is None:
                state._video_cursor_pts = v_pts

            fetched_seqs.add(seq)
            new_seqs += 1

            _cut_and_mux_next_window(channel_id, state)
            video_epoch = state.current_video_epoch()

        if is_vod and vod_pending_video is not None and '#EXT-X-ENDLIST' in vtext:
            _mux_whole_video_segment(
                channel_id, state, video_epoch,
                vod_pending_video['vdata'], vod_pending_video['v_pts'],
                _estimate_v_end_pts(vod_pending_video['all_pts']),
                vod_pending_video['is_disc'], vod_pending_video['pending_disc_ahead'])
            vod_pending_video = None
            video_epoch = state.current_video_epoch()

        if state._video_prefetch_stop.is_set():
            break
        if new_seqs == 0:
            if state._video_prefetch_stop.wait(timeout=state.seg_duration / 5):
                break


def active_channel_url(channel_id: str) -> 'str | None':
    """Return the local proxy master-playlist URL for *channel_id* if it
    already has a *running* session registered, or None otherwise.

    Lets a caller about to mint a fresh stream URL (see
    PlutoTVRequest.buildStreamURL) check first whether the channel is
    already running - under some other pool slot's session - so it can
    reuse that instead of silently repointing an already-live channel at
    a different device identity's session (see buildStreamURL's
    docstring for why that corrupts recordings of an already-live
    channel).

    Checks _audio_prefetch_stop rather than just presence in _channels:
    close_channel() deliberately leaves the state entry in place while
    its threads wind down (see its docstring), so a channel that was
    just zapped away from would otherwise look "active" here too and get
    handed a URL with nothing left running behind it - register_channel()
    is what actually knows how to undo that close and needs to run for
    this channel_id in that case, not be skipped.
    """
    state = _get_state(channel_id)
    if state is None or state._audio_prefetch_stop.is_set():
        return None
    return f'http://{PROXY_HOST}:{PROXY_PORT}/auto/{channel_id}.m3u8'


def _stitcher_sid(master_url: str) -> 'str | None':
    """Extract Pluto's stitcher session id (?sid=...) from a master URL.

    Identifies which real Pluto stitching session a master_url belongs to,
    as opposed to which JWT/device-slot it was minted with (those rotate
    independently of the session - see register_channel's sid-comparison
    use). Returns None if absent so a URL shape that never carries sid
    (e.g. no live stitcher session at all) compares unequal to itself only
    when actually different strings, never spuriously "changed".
    """
    return (parse_qs(urlparse(master_url).query).get('sid') or [None])[0]


def register_channel(channel_id: str, real_master_url: str, url_refresher=None) -> str:
    """Register a channel and return the local proxy URL for its master playlist.

    *url_refresher*, when supplied, is stored on the channel state and called
    from the handler thread (safe to block) when the master playlist fetch fails
    with an empty JWT.  It must return a fresh master URL with a valid token, or
    None on failure.

    Idempotent for an already-registered channel_id: reuses its existing
    state (pre-fetch threads, audio ring, muxed-segment cache) and just
    refreshes master_url, rather than tearing it down and starting from
    scratch. This is what makes it safe for *any* caller to invoke freely
    for the same channel_id - live playback, a recording of the channel
    currently being watched, or a recording timer simply re-resolving the
    same service mid-recording - without disrupting an already-running
    stream. The previous version always replaced same-channel_id state on
    the theory that this only happens on a deliberate re-tune; in
    production a single recording's service reference got re-resolved a
    dozen times in 10 minutes with no live viewer involved at all, each
    call restarting the channel from scratch (fresh master/variant fetch,
    empty audio ring, epoch reset to 0) and audibly corrupting the
    recording. There is no actual caller that depends on a same-channel_id
    call forcing a restart - that's what close_channel() is for, explicitly.

    Different channel_ids never evict each other through this call, so
    independent concurrent channels (e.g. one being recorded while another
    is watched live) each keep their own pre-fetch threads undisturbed.
    Closing a channel nobody asked to replace is handled elsewhere: callers
    that track "the live slot" call close_channel() explicitly the moment
    they know it moved on (see PlutoTVRequest.playServiceExtension), and the
    idle reaper started alongside the HTTP server in start() is the backstop
    for anything that stops being read without ever calling back in here
    (e.g. a recording that simply ends).
    """
    with _state_lock:
        existing = _channels.get(channel_id)
        if existing is not None:
            old_sid = _stitcher_sid(existing.master_url)
            new_sid = _stitcher_sid(real_master_url)
            existing.master_url = real_master_url
            if url_refresher is not None:
                existing.url_refresher = url_refresher
            was_closed = existing._audio_prefetch_stop.is_set()
            if was_closed or (new_sid is not None and new_sid != old_sid):
                with existing._lock:
                    existing._ready_segs.clear()
                    existing._window_meta.clear()
                    existing._provisional_segs.clear()
                    existing._audio_bufs.clear()
                    existing._audio_appended_seqs.clear()
                    existing._audio_disc_seqs_seen.clear()
            existing._audio_prefetch_stop.clear()
            existing._video_prefetch_stop.clear()
            if was_closed:
                existing._audio_prepopulated = False
        else:
            state = _ChannelState(real_master_url)
            state.url_refresher = url_refresher
            _channels[channel_id] = state
    return f'http://{PROXY_HOST}:{PROXY_PORT}/auto/{channel_id}.m3u8'


def close_channel(channel_id: str) -> bool:
    """Explicitly close one channel's pre-fetch threads without touching any
    other channel.  Used when a caller knows a specific channel is no longer
    needed (e.g. the live slot zapped to a different service) and wants that
    cleaned up immediately rather than waiting for the idle reaper.

    Returns False if the channel wasn't registered. As with register_channel's
    old-entry teardown, close() runs off-thread and the entry is left in
    _channels so an in-flight request still finds cached state instead of a
    404 while it winds down.
    """
    with _state_lock:
        state = _channels.get(channel_id)
    if state is None:
        return False
    threading.Thread(target=state.close, daemon=True).start()
    return True


_IDLE_TIMEOUT = 60.0
_REAP_INTERVAL = 15.0


def _reap_idle_channels():
    while True:
        time.sleep(_REAP_INTERVAL)
        now = time.monotonic()
        with _state_lock:
            idle_ids = [cid for cid, st in _channels.items()
                        if now - st.last_access > _IDLE_TIMEOUT]
        for cid in idle_ids:
            with _state_lock:
                st = _channels.get(cid)
                if st is None or time.monotonic() - st.last_access <= _IDLE_TIMEOUT:
                    continue
            logger.debug('%s: idle %.0fs+, reaping', cid, _IDLE_TIMEOUT)
            st.close()
            with _state_lock:
                if _channels.get(cid) is st:
                    _channels.pop(cid, None)


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def _fetch(url: str, binary: bool = False, timeout: int = 6, tag: str = ''):
    """Fetch *url* and return text or bytes; returns None on any error.

    Default timeout is kept close to _SEG_DURATION rather than requests'
    usual generous allowance: this covers playlist fetches (master,
    variant, audio) that run synchronously inside an HTTP handler thread
    while eServiceMP3 is waiting on the response - including, on the
    /auto/{channel}.m3u8 path, during the pipeline's own initial NULL ->
    READY setup for a just-zapped channel. A slow CDN response there should
    fail fast rather than let that handler thread - and whatever in the
    playback pipeline is blocked on it - sit for up to the old 15s.

    *tag* identifies the call site in the error log only (e.g.
    'handler:playlist' vs 'video-prefetch') - several different call sites
    (an HTTP handler thread serving Enigma2 directly, and a background
    pre-fetch thread) fetch the exact same URLs independently, and a bare
    "fetch error" line can't otherwise say which one actually hit a given
    timeout - which matters when diagnosing whether a CDN blip was ever
    exposed to Enigma2 at all or was fully absorbed by a background retry.
    """
    try:
        r = _SESSION.get(url, timeout=timeout)
        r.raise_for_status()
        return r.content if binary else r.text
    except Exception as exc:
        label = f' [{tag}]' if tag else ''
        logger.debug('fetch error%s %r: %s', label, url, exc)
        return None


def _has_empty_jwt(url: str) -> bool:
    """Return True when *url* contains an empty jwt= token (jwt=& or jwt= at end)."""
    return '?jwt=&' in url or url.endswith('?jwt=')


_AUTO_MASTER_RETRIES = 2
_AUTO_MASTER_RETRY_DELAY = 1.0


class HLSProxyHandler(BaseHTTPRequestHandler):
    """Handles three URL patterns:

        /master/{channel_id}.m3u8       – master playlist (modified)
        /pl/{channel_id}/{idx}.m3u8     – variant playlist (merged)
        /seg/{channel_id}/{seq}.ts      – TS segment (merged)
    """

    def log_message(self, format, *args):  # pylint: disable=redefined-builtin  # noqa: A002
        pass

    def do_GET(self):
        path = self.path.split('?')[0]
        parts = path.strip('/').split('/')
        try:
            kind = parts[0] if parts else ''
            if kind == 'master' and len(parts) >= 2:
                ch = parts[1].removesuffix('.m3u8')
                self._handle_master(ch)
            elif kind == 'auto' and len(parts) >= 2:
                ch = parts[1].removesuffix('.m3u8')
                self._handle_auto(ch)
            elif kind == 'pl' and len(parts) >= 3:
                ch = parts[1]
                idx = int(parts[2].removesuffix('.m3u8'))
                self._handle_playlist(ch, idx)
            elif kind == 'seg' and len(parts) >= 3:
                ch = parts[1]
                seq = int(parts[2].removesuffix('.ts'))
                self._handle_segment(ch, seq)
            elif kind == 'rec' and len(parts) >= 2:
                ch = parts[1].removesuffix('.ts')
                self._handle_recording(ch)
            else:
                self._send(404, b'Not found', 'text/plain')
        except BrokenPipeError:
            pass
        except Exception as exc:
            logger.debug('handler error: %s', exc)
            try:
                self._send(500, b'Internal error', 'text/plain')
            except OSError:
                pass

    def _handle_auto(self, channel_id: str):
        """Fetch the real master playlist, detect demuxed audio, and either
        process it through the muxing proxy or redirect servicemp3 directly
        to the real URL when no audio muxing is needed."""
        state = _get_state(channel_id)
        if not state:
            self._send(404, b'Unknown channel', 'text/plain')
            return
        text = _fetch(state.master_url, tag='handler:auto-master')
        if not text and _has_empty_jwt(state.master_url) and state.url_refresher:
            fresh_url = state.url_refresher()
            if fresh_url:
                state.master_url = fresh_url
                text = _fetch(state.master_url, tag='handler:auto-master-refreshed')
        for attempt in range(_AUTO_MASTER_RETRIES):
            if text:
                break
            time.sleep(_AUTO_MASTER_RETRY_DELAY)
            text = _fetch(state.master_url, tag=f'handler:auto-master-retry{attempt + 1}')
        if not text:
            self._send(502, b'Bad gateway', 'text/plain')
            return
        needs_mux = any(
            line.startswith('#EXT-X-MEDIA:') and 'TYPE=AUDIO' in line
            for line in text.splitlines()
        )
        if not needs_mux:
            logger.debug('%s: no demuxed audio – redirecting to CDN', channel_id)
            self.send_response(302)
            self.send_header('Location', state.master_url)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self._handle_master(channel_id, text)

    def _handle_master(self, channel_id: str, text: str = None):
        state = _get_state(channel_id)
        if not state:
            self._send(404, b'Unknown channel', 'text/plain')
            return
        if text is None:
            text = _fetch(state.master_url, tag='handler:master')
        if not text:
            self._send(502, b'Bad gateway', 'text/plain')
            return

        master_base = state.master_url.split('?')[0].rsplit('/', 1)[0] + '/'

        out = []
        variant_urls = []
        audio_candidates = []
        lines = text.splitlines()
        logger.debug('%s: master playlist fetched, %s lines', channel_id, len(lines))
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith('#EXT-X-MEDIA:'):
                if 'TYPE=AUDIO' in line:
                    cand = HLSPlaylist.audio_candidate(line, master_base)
                    if cand:
                        audio_candidates.append(cand)
                i += 1
                continue

            if line.startswith('#EXT-X-STREAM-INF:'):
                clean = HLSPlaylist.remove_attr('AUDIO', line)
                clean = HLSPlaylist.remove_attr('SUBTITLES', clean)
                clean = HLSPlaylist.remove_attr('CLOSED-CAPTIONS', clean)
                out.append(clean)
                i += 1
                if i < len(lines):
                    real_url = urljoin(master_base, lines[i])
                    idx = len(variant_urls)
                    variant_urls.append(real_url)
                    proxy_url = (
                        f'http://{PROXY_HOST}:{PROXY_PORT}'
                        f'/pl/{channel_id}/{idx}.m3u8'
                    )
                    out.append(proxy_url)
                    i += 1
                continue

            out.append(line)
            i += 1

        audio_url = HLSPlaylist.select_primary_audio(audio_candidates)
        with state._lock:
            state.audio_url = audio_url
            state.variant_urls = variant_urls

        if audio_url:
            logger.debug('%s: audio rendition → %s', channel_id, audio_url)
            state.start_audio_prefetch_if_needed(channel_id)
        logger.debug('%s: %s variant(s) registered', channel_id, len(variant_urls))
        self._send(200, '\n'.join(out).encode(), 'application/x-mpegURL')

    def _handle_playlist(self, channel_id: str, idx: int):
        state = _get_state(channel_id)
        if not state or idx >= len(state.variant_urls):
            self._send(404, b'Unknown variant', 'text/plain')
            return

        if not state.audio_url:
            text = _fetch(state.variant_urls[idx], tag='handler:playlist-passthrough')
            self._send(200, (text or '').encode(), 'application/x-mpegURL')
            return

        vtext = _fetch(state.variant_urls[idx], tag='handler:playlist')
        if not vtext:
            cached = state._last_playlist_out.get(idx)
            if cached is not None:
                cached_out, cached_at = cached
                age = time.monotonic() - cached_at
                logger.debug('%s: Event: variant playlist fetch failed'
                             ' | Action: serving cached playlist (%.1fs stale) instead of 502',
                             channel_id, age)
                self._send(200, cached_out, 'application/x-mpegURL')
                return
            logger.debug('%s: Event: variant playlist fetch failed,'
                         ' no cached playlist available | Action: 502', channel_id)
            self._send(502, b'Video playlist unavailable', 'text/plain')
            return

        vbase = state.variant_urls[idx].split('?')[0].rsplit('/', 1)[0] + '/'
        vseq, vsegs = HLSPlaylist.segments(vtext, vbase)
        if vseq < 0:
            logger.debug('%s: Event: negative #EXT-X-MEDIA-SEQUENCE (%s)'
                         ' from CDN playlist | Action: reject as unavailable, let client retry',
                         channel_id, vseq)
            self._send(502, b'Video playlist unavailable', 'text/plain')
            return
        _d = HLSPlaylist.extinf_duration(vtext)
        if _d is not None and _d != state.seg_duration:
            logger.debug('%s: seg_duration %.2fs -> %.2fs', channel_id, state.seg_duration, _d)
            state.seg_duration = _d

        if state.audio_url and state.try_start_audio_prepopulate():
            atext_pre = _fetch(state.audio_url, tag='handler:playlist-prepopulate')
            if atext_pre:
                abase_pre = state.audio_url.split('?')[0].rsplit('/', 1)[0] + '/'
                try:
                    aseq_pre, asegs_pre = HLSPlaylist.segments(atext_pre, abase_pre)
                    akey_pre, aiv_pre, adisc_pre = HLSPlaylist.segment_keys(atext_pre, abase_pre)
                except Exception:
                    asegs_pre = []
                    aseq_pre, akey_pre, aiv_pre, adisc_pre = 0, [], [], []

                asegs_pre = asegs_pre[:_AUDIO_PREPOPULATE_CAP]
                akey_pre = akey_pre[:_AUDIO_PREPOPULATE_CAP]
                aiv_pre = aiv_pre[:_AUDIO_PREPOPULATE_CAP]
                adisc_pre = adisc_pre[:_AUDIO_PREPOPULATE_CAP]

                def _pre_fetch_one(seq, url, key_url, iv_explicit):
                    adata = _fetch(url, binary=True, timeout=state.seg_duration * 0.8,
                                   tag='handler:playlist-prepopulate-segment')
                    if not adata:
                        return
                    if key_url:
                        kb = AES128.fetch_key(key_url, _fetch)
                        if kb:
                            adata = AES128.decrypt(adata, kb,
                                                   AES128.iv(iv_explicit, seq))
                    if not adata or adata[0] != 0x47:
                        return
                    pre_results[seq] = adata

                pre_results: dict = {}
                workers = min(_AUDIO_FETCH_WORKERS, max(1, len(asegs_pre)))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for i, url in enumerate(asegs_pre):
                        k = akey_pre[i] if i < len(akey_pre) else None
                        iv = aiv_pre[i] if i < len(aiv_pre) else None
                        pool.submit(_pre_fetch_one, aseq_pre + i, url, k, iv)
                for i in range(len(asegs_pre)):
                    seq = aseq_pre + i
                    adata = pre_results.get(seq)
                    if adata is None:
                        continue
                    disc = adisc_pre[i] if i < len(adisc_pre) else False
                    _append_audio(state, channel_id, seq, disc, adata)
                logger.debug('%s: pre-populated audio buffer with %s segment(s)',
                             channel_id, len(pre_results))

        state.start_video_prefetch_if_needed(channel_id, idx)

        if not state.has_ready_segs():
            is_vod = channel_id.startswith('vod')
            budget = _VOD_COLD_START_WAIT if is_vod else _COLD_START_FIRST_SEGMENT_WAIT
            min_segs = _VOD_COLD_START_MIN_SEGMENTS if is_vod else 1
            cold_deadline = time.monotonic() + budget
            while state.ready_seg_count() < min_segs and time.monotonic() < cold_deadline:
                time.sleep(0.05)

        out = []
        for line in vtext.splitlines():
            stripped = line.strip()
            if (stripped.startswith('#EXT-X-KEY:')
                    or stripped == '#EXT-X-DISCONTINUITY'
                    or stripped.startswith('#EXT-X-MEDIA-SEQUENCE:')
                    or stripped.startswith('#EXTINF:')
                    or stripped.startswith('#EXT-X-PLAYLIST-TYPE:')
                    or stripped == '#EXT-X-ENDLIST'
                    or (stripped and not stripped.startswith('#'))):
                continue
            out.append(line)

        window_size = max(1, len(vsegs))
        with state._lock:
            advertise_seqs = list(state._ready_segs.keys())[-window_size:]
            window_meta = dict(state._window_meta)

        media_seq = advertise_seqs[0] if advertise_seqs else state._next_chunk_seq
        insert_at = 1 if out and out[0].strip() == '#EXTM3U' else 0
        out.insert(insert_at, f'#EXT-X-MEDIA-SEQUENCE:{media_seq}')
        if channel_id.startswith('vod'):
            out.insert(insert_at + 1, '#EXT-X-START:TIME-OFFSET=0,PRECISE=YES')

        for cseq in advertise_seqs:
            meta = window_meta.get(cseq)
            if meta is None:
                continue
            duration = max(0.0, (meta.end_pts - meta.start_pts) / 90000.0)
            if meta.is_disc:
                out.append('#EXT-X-DISCONTINUITY')
            out.append(f'#EXTINF:{duration:.3f},')
            out.append(
                f'http://{PROXY_HOST}:{PROXY_PORT}'
                f'/seg/{channel_id}/{cseq}.ts'
            )

        out_bytes = '\n'.join(out).encode()
        state._last_playlist_out[idx] = (out_bytes, time.monotonic())
        self._send(200, out_bytes, 'application/x-mpegURL')

    def _handle_segment(self, channel_id: str, seq: int):
        state = _get_state(channel_id)
        if not state:
            self._send(404, b'Unknown channel', 'text/plain')
            return

        state.record_segment_request(seq)

        deadline = time.monotonic() + state.seg_duration * _HANDLE_SEGMENT_DEADLINE_FACTOR
        provisional_fallback = None
        while True:
            with state._lock:
                muxed = state._ready_segs.get(seq)
                provisional = seq in state._provisional_segs
            if muxed is not None and not provisional:
                logger.debug('%s/%s: cache hit %s bytes', channel_id, seq, len(muxed))
                state._last_segment_out = (seq, muxed)
                self._send(200, muxed, 'video/MP2T')
                return
            if muxed is not None:
                provisional_fallback = muxed
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)

        if provisional_fallback is not None:
            logger.debug('%s/%s: cache hit %s bytes (provisional, upgrade never landed)',
                         channel_id, seq, len(provisional_fallback))
            state._last_segment_out = (seq, provisional_fallback)
            self._send(200, provisional_fallback, 'video/MP2T')
            return

        logger.debug('%s/%s: Event: chunk never produced by prefetch loop'
                     ' | Action: wait a little longer, then re-serve last chunk', channel_id, seq)
        extra_deadline = time.monotonic() + state.seg_duration
        while time.monotonic() < extra_deadline:
            with state._lock:
                muxed = state._ready_segs.get(seq)
                provisional = seq in state._provisional_segs
            if muxed is not None and not provisional:
                logger.debug('%s/%s: cache hit %s bytes (after extended wait)',
                             channel_id, seq, len(muxed))
                state._last_segment_out = (seq, muxed)
                self._send(200, muxed, 'video/MP2T')
                return
            time.sleep(0.05)

        if self._serveLastSegment(state, channel_id, seq, 'chunk never produced'):
            return
        self._send(404, b'Segment not in cache', 'text/plain')

    def _serveLastSegment(self, state, channel_id, seq, reason):
        """Re-serve the last segment actually sent to Enigma2, in place of a
        hard 404/502, if we have one. See state._last_segment_out's comment -
        one repeated stale segment is a brief visible stutter, but far better
        than risking hlsdemux wedging on a hard error with no recourse but a
        manual re-zap. Returns True if it served something (caller should
        return without sending its own error response).

        Unlike the old per-CDN-segment design, there's no way to
        reconstruct a genuinely never-produced chunk_seq on demand here:
        chunk_seq is minted only at production time (see
        state._next_chunk_seq), so one that was never produced was never
        associated with any CDN URL to re-fetch from in the first place -
        not merely evicted from a cache, but never assigned a source at
        all. _handle_playlist only ever advertises chunk_seqs already
        present in state._ready_segs, so reaching this fallback with
        nothing to serve should be rare to the point of near-unreachability
        in practice; it exists purely as a last-resort backstop.
        """
        last = state._last_segment_out
        if last is None:
            with state._lock:
                last = next(reversed(state._ready_segs.items()), None)
            if last is None:
                return False
        last_seq, last_bytes = last
        logger.debug('%s/%s: Event: %s | Action: re-serving last segment (%s) instead of erroring',
                     channel_id, seq, reason, last_seq)
        self._send(200, last_bytes, 'video/MP2T')
        return True

    def _handle_recording(self, channel_id: str):
        from . import RecordingProxy
        RecordingProxy.handle_recording(self, channel_id)

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


_server = None
_server_lock = threading.Lock()


def start():
    """Start the proxy HTTP server.  Safe to call multiple times (idempotent)."""
    global _server
    with _server_lock:
        if _server is not None:
            return
        try:
            _server = _ThreadingHTTPServer((PROXY_HOST, PROXY_PORT), HLSProxyHandler)
            t = threading.Thread(target=_server.serve_forever, daemon=True)
            t.name = 'PlutoHLSProxy'
            t.start()
            threading.Thread(target=_reap_idle_channels, daemon=True,
                             name='LiveProxyReaper').start()
            logger.debug('HLS proxy started on %s:%s', PROXY_HOST, PROXY_PORT)
        except Exception as exc:
            logger.debug('HLS proxy start failed: %s', exc)
