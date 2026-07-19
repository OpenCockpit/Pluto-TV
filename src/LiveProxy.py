# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0
#
#   LiveProxy – local HLS proxy that converts demuxed Pluto TV streams
#   (separate video-TS + audio-TS delivered via EXT-X-MEDIA:TYPE=AUDIO) into
#   single muxed TS segments that Enigma2's DVB hardware pipeline handles.


import math
import os
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict, deque
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
_MAX_AUDIO_RING = 30
_PREFETCH_LEAD_SEGMENTS = 20
_DISC_LOOKAHEAD_SEGMENTS = 30
_MAX_AUDIO_EPOCH_LAG = 2
_AUDIO_FETCH_WORKERS = 4
_AUDIO_STRAGGLER_GRACE_POLLS = -(-_PREFETCH_LEAD_SEGMENTS // _AUDIO_FETCH_WORKERS)
_AUDIO_PREPOPULATE_CAP = 10
_LATE_AUDIO_WAIT_FACTOR = 6.0
_HANDLE_SEGMENT_DEADLINE_FACTOR = 1.0
_COLD_START_FIRST_SEGMENT_WAIT = 2.0
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


def _ffmpeg_mux(video_data: bytes, audio_data: 'bytes | None' = None,
                state: '_ChannelState | None' = None, is_disc: bool = False,
                track_cc: bool = False, reset_cc: 'bool | None' = None) -> bytes:
    """Mux video-only and audio-only TS segments via system ffmpeg.

    Both inputs are written to seekable temp files so ffmpeg can probe each
    stream's start_time and normalise output PTS.  When *audio_data* is None
    the single stream is re-muxed without explicit stream mapping.
    Falls back to returning the input data unchanged on any ffmpeg error.

    itsoffset is derived from the PTS difference between the two streams.
    Audio and video segments are paired sequentially (FIFO queue); the
    itsoffset reflects whatever CDN-side alignment exists between the streams.

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

    v_pts = _first_pts(video_data)
    itsoffset = 0.0
    if audio_data:
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

    __slots__ = ('master_url', 'url_refresher', 'audio_url', 'variant_urls', 'segments',
                 'seg_duration', 'last_access', 'audio_format',
                 '_audio_queue', '_audio_epoch', '_audio_epoch_assigned', '_video_epoch',
                 '_last_requested_seq', '_ready_segs', '_dup_seqs',
                 '_audio_prepopulated', '_provisional_segs',
                 '_audio_prefetch_thread', '_audio_prefetch_stop',
                 '_video_prefetch_thread', '_video_prefetch_stop', '_lock', '_cc_state',
                 '_cc_resync_pending', '_last_playlist_out', '_last_segment_out')

    def __init__(self, master_url: str):
        self.master_url = master_url
        self.url_refresher = None
        self.seg_duration: float = _SEG_DURATION
        self.last_access = time.monotonic()
        self.audio_url = None
        self.variant_urls = []
        self.segments = OrderedDict()
        self.audio_format: 'tuple[int, int] | None' = None
        self._audio_queue: 'deque[tuple[int, int, int, bytes, float]]' = deque()
        self._audio_epoch = 0
        self._audio_epoch_assigned: 'dict[int, int]' = {}
        self._video_epoch = 0
        self._last_requested_seq = 0
        self._audio_prepopulated = False
        self._audio_prefetch_thread: 'threading.Thread | None' = None
        self._audio_prefetch_stop = threading.Event()
        self._ready_segs: 'OrderedDict[int, bytes]' = OrderedDict()
        self._provisional_segs: set = set()
        self._dup_seqs: set = set()
        self._video_prefetch_thread: 'threading.Thread | None' = None
        self._video_prefetch_stop = threading.Event()
        self._lock = threading.Lock()
        self._cc_state: dict = {}
        self._cc_resync_pending = False
        self._last_playlist_out: 'dict[int, tuple[bytes, float]]' = {}
        self._last_segment_out: 'tuple[int, bytes] | None' = None

    def add_segments(self, base_seq: int, video_urls: list,
                     key_urls: list = None, ivs: list = None):
        """Register video segment URLs (audio is handled by the pre-fetch thread)."""
        with self._lock:
            for i, v in enumerate(video_urls):
                k = key_urls[i] if key_urls and i < len(key_urls) else None
                iv = ivs[i] if ivs and i < len(ivs) else None
                self.segments[base_seq + i] = (v, k, iv)
            while len(self.segments) > _MAX_SEG_CACHE:
                self.segments.popitem(last=False)

    def get_segment(self, seq: int):
        with self._lock:
            return self.segments.get(seq)

    def pop_audio_for_pts(self, v_pts: int, epoch: int,
                          tolerance: int = None,
                          wait_secs: float = None,
                          channel_id: str = '',
                          video_seq: 'int | None' = None,
                          allow_stale_epoch: bool = True,
                          diag: 'dict | None' = None) -> 'tuple[int, int, bytes] | None':
        """Find and remove the audio entry whose PTS is closest to v_pts.

        *diag*, if given, is filled in (only on a None return) with
        {'stuck_ahead': bool, 'best_diff': int | None} describing the closest
        same-epoch candidate ever seen - lets a caller distinguish "audio for
        this exact slot was genuinely never produced upstream" (stuck_ahead;
        see stuck_ahead_since's own comment below) from "nothing arrived at
        all" (e.g. a real fetch outage), instead of treating every miss alike.

        Only entries tagged with the same *epoch* (the count of audio-rendition
        discontinuities seen so far) are eligible: PTS values alone cannot be
        trusted to identify which side of a content transition a queued entry
        belongs to (some CDNs keep a continuous clock across the splice, and
        the audio prefetch thread races independently of the caller), so epoch
        equality is what actually proves "same piece of content."

        Scans the whole queue so out-of-order insertions (parallel fetch workers)
        don't cause wrong pairings.  Waits up to wait_secs seconds for a match
        to arrive; returns (pts, seq, data) if found within tolerance ticks,
        else None.  Defaults are derived from self.seg_duration (updated from
        #EXTINF).

        When *channel_id* is given, logs one line per call: on a match, which
        audio seq got paired and how far its PTS was from v_pts; on a miss, a
        full snapshot of the ring (every buffered seq/epoch/pts) so a mismatch
        can be diagnosed straight from the log — epoch desync (ring has the
        wrong epoch entirely) looks different from a genuine late fetch (ring
        has the right epoch but PTS is still outside tolerance, or is empty).

        Fails fast (skips the rest of wait_secs) once every ring entry has
        moved on to a higher epoch than *epoch*: the audio-rendition disc
        marker for that transition has already fired, so there is no future
        in which an entry tagged with the now-passed *epoch* shows up — only
        evicted by the time a video-side caller is stale relative to it.
        Burning the full wait on a request that can never succeed is what
        lets the video pre-fetch loop fall behind the live edge and miss the
        video-side disc marker that would let its epoch counter catch up
        (see _video_prefetch_loop's resync after a miss).

        *allow_stale_epoch* gates the one-epoch-back fallback below; pass
        False for the segment that itself carries (or was just resynced to)
        the new epoch. Every transition on this CDN resets PTS to the same
        small values (90000, 540000, ...), so exactly on that segment a
        leftover entry from a separate, already-concluded prior epoch (e.g.
        a one-segment bumper between the movie and the real ad) can
        coincidentally land within tolerance of v_pts despite being
        genuinely different content — the fallback must stay disabled there
        and only re-enable once the epoch has run for at least one prior
        segment, where a coincidental collision is no longer the common case.
        """
        if tolerance is None:
            tolerance = int(self.seg_duration / 2 * 90000)
        if wait_secs is None:
            wait_secs = self.seg_duration * 1.6
        wait_start = time.monotonic()
        deadline = wait_start + wait_secs
        snapshot: list = []
        ring_passed_epoch_since: 'float | None' = None
        stuck_ahead_since: 'float | None' = None
        while True:
            with self._lock:
                best_idx, best_diff, best_pts = None, math.inf, None
                snapshot = [(s, e, p) for p, e, s, _, _ in self._audio_queue]
                for i, (pts, e, _seq, _data, _added_at) in enumerate(self._audio_queue):
                    if e != epoch:
                        continue
                    diff = abs(pts - v_pts)
                    if diff < best_diff:
                        best_diff = diff
                        best_idx = i
                        best_pts = pts
                if best_idx is not None and best_diff <= tolerance:
                    pts, _e, aseq, data, _added_at = self._audio_queue[best_idx]
                    del self._audio_queue[best_idx]
                    match = (pts, aseq, data)
                else:
                    match = None
                ring_passed_epoch = bool(snapshot) and min(e for _, e, _ in snapshot) > epoch
                stuck_ahead = (best_pts is not None and best_diff > tolerance
                               and best_pts > v_pts)
                epoch_abandoned = self._video_epoch < epoch
            if match is not None:
                pts, aseq, data = match
                if channel_id:
                    logger.debug('%s/%s: audio match aseq=%s v_pts=%s a_pts=%s'
                                 ' diff=%s epoch=%s waited=%.2fs',
                                 channel_id, video_seq, aseq, v_pts, pts,
                                 pts - v_pts, epoch, time.monotonic() - wait_start)
                return pts, aseq, data
            if ring_passed_epoch:
                if ring_passed_epoch_since is None:
                    ring_passed_epoch_since = time.monotonic()
            else:
                ring_passed_epoch_since = None
            if stuck_ahead:
                if stuck_ahead_since is None:
                    stuck_ahead_since = time.monotonic()
            else:
                stuck_ahead_since = None
            ring_passed_epoch_expired = (
                ring_passed_epoch_since is not None
                and (time.monotonic() - ring_passed_epoch_since
                     >= self.seg_duration / 5 * _AUDIO_STRAGGLER_GRACE_POLLS))
            stuck_ahead_expired = (
                stuck_ahead_since is not None
                and (time.monotonic() - stuck_ahead_since
                     >= self.seg_duration / 5 * _AUDIO_STRAGGLER_GRACE_POLLS))
            if ring_passed_epoch_expired or stuck_ahead_expired or epoch_abandoned or time.monotonic() >= deadline:
                if epoch > 0 and allow_stale_epoch:
                    with self._lock:
                        best_idx, best_diff = None, tolerance + 1
                        for i, (pts, e, _seq, _data, _added_at) in enumerate(self._audio_queue):
                            if e != epoch - 1:
                                continue
                            diff = abs(pts - v_pts)
                            if diff < best_diff:
                                best_diff = diff
                                best_idx = i
                        if best_idx is not None and best_diff <= tolerance:
                            pts, _e, aseq, data, _added_at = self._audio_queue[best_idx]
                            del self._audio_queue[best_idx]
                            stale_match = (pts, aseq, data)
                        else:
                            stale_match = None
                    if stale_match is not None:
                        pts, aseq, data = stale_match
                        if channel_id:
                            logger.debug('%s/%s: audio match aseq=%s v_pts=%s a_pts=%s'
                                         ' diff=%s epoch=%s->%s waited=%.2fs (stale-epoch fallback)',
                                         channel_id, video_seq, aseq, v_pts, pts,
                                         pts - v_pts, epoch - 1, epoch, time.monotonic() - wait_start)
                        return pts, aseq, data
                if channel_id:
                    ring = ', '.join(
                        f'aseq={s} epoch={e} pts={p} diff={p - v_pts}'
                        for s, e, p in snapshot
                    ) or 'empty'
                    logger.debug('%s/%s: audio MISS v_pts=%s epoch=%s tolerance=%s'
                                 ' waited=%.2fs/%.2fs ring_passed_epoch=%s'
                                 ' stuck_ahead_expired=%s epoch_abandoned=%s ring=[%s]',
                                 channel_id, video_seq, v_pts, epoch, tolerance,
                                 time.monotonic() - wait_start, wait_secs, ring_passed_epoch,
                                 stuck_ahead_expired, epoch_abandoned, ring)
                if diag is not None:
                    diag['stuck_ahead'] = stuck_ahead_expired
                    diag['best_diff'] = best_diff if best_pts is not None else None
                return None
            time.sleep(0.1)

    def ring_max_epoch(self) -> 'int | None':
        """Highest epoch currently buffered in the audio ring, or None if empty.

        Used by _video_prefetch_loop to detect that its local video_epoch has
        overtaken the audio side (e.g. the video rendition flagged a disc that
        the audio rendition never mirrored) so it can resync back down instead
        of waiting out the full audio-wait budget on every segment forever.
        """
        with self._lock:
            if not self._audio_queue:
                return None
            return max(e for _, e, _, _, _ in self._audio_queue)

    def assign_audio_epoch(self, seq: int, disc: bool) -> int:
        """Resolve the epoch for audio segment *seq*, exactly once per
        recent occurrence of that seq number (see _SEQ_REPROCESS_WINDOW).

        Bumps the shared counter first when *disc* is True (an
        #EXT-X-DISCONTINUITY immediately precedes *seq* in the audio
        rendition) - but only the first time *seq* is seen within the
        window. The one-shot playlist pre-population pass
        (_handle_playlist) and the ongoing background audio pre-fetch
        thread (_audio_prefetch_loop) both parse the same audio playlist
        independently and both start from the very first segment -
        without this de-dup, whichever of the two runs second would call
        this again for the same early segments and double-bump
        _audio_epoch for one real transition, desyncing it from
        video_epoch. A plain counter with only a lock (the previous
        implementation) doesn't prevent this: the lock makes each call
        atomic, but two callers each resolving seq 0..N in their own loop
        will each bump the counter once per real disc tag they encounter
        in that range, regardless of the other caller having already done
        the same for the same seqs.

        The window must stay narrow (_SEQ_REPROCESS_WINDOW, matching what
        _audio_prefetch_loop's own fetched_seqs and _video_prefetch_loop's
        fetched_seqs/bumped_disc_seqs already use for this exact seq) and
        not the much wider _MAX_SEG_CACHE this used previously: some Pluto
        channels serve a stitched ad-pod/filler loop whose playlist
        media-sequence numbers wrap back down and repeat rather than
        growing monotonically forever, and a wide window made a repeated
        seq permanently reuse whatever epoch (and disc decision) its
        first-ever occurrence resolved to - silently ignoring every real
        #EXT-X-DISCONTINUITY tag on that seq number in every later loop.
        That left _audio_epoch stuck for minutes while video_epoch (whose
        own dedup already used the narrow window) correctly kept
        advancing across each real repeated transition, which in turn
        made add_audio prune every freshly-fetched real audio segment as
        too far behind video_epoch - observed in production as most of a
        channel's audio being muxed with silence for minutes at a time.
        """
        with self._lock:
            if seq in self._audio_epoch_assigned:
                return self._audio_epoch_assigned[seq]
            if disc:
                self._audio_epoch += 1
            epoch = self._audio_epoch
            self._audio_epoch_assigned[seq] = epoch
            self._audio_epoch_assigned = {
                s: e for s, e in self._audio_epoch_assigned.items()
                if s >= seq - _SEQ_REPROCESS_WINDOW
            }
            return epoch

    def current_audio_epoch(self) -> int:
        """Thread-safe read of the current epoch, with no side effects."""
        with self._lock:
            return self._audio_epoch

    def try_start_audio_prepopulate(self) -> bool:
        """Atomically claim the one-shot audio pre-population pass.

        Returns True for the first caller only; every later call (including
        concurrent ones racing on the first request) returns False. Must be
        a real flag rather than checking len(_audio_queue) == 0: the ring
        legitimately drains to empty for a moment at every real content
        transition, and re-running this block then would re-parse the same
        still-in-window #EXT-X-DISCONTINUITY tag the live audio pre-fetch
        thread already counted, double-bumping _audio_epoch for one real
        transition and permanently desyncing it from video_epoch.
        """
        with self._lock:
            if self._audio_prepopulated:
                return False
            self._audio_prepopulated = True
            return True

    def set_video_epoch(self, epoch: int):
        """Record _video_prefetch_loop's current epoch so add_audio can
        evict ring entries that have fallen behind it immediately, instead
        of waiting on _MAX_AUDIO_EPOCH_LAG or the ring's size cap.
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

    def add_audio(self, pts: int, epoch: int, seq: int, data: bytes):
        """Append an audio segment to the queue; drop stale or excess entries.

        No-ops if *seq* is already queued: the synchronous pre-population
        pass (first playlist request) and the background audio pre-fetch
        thread each fetch independently with their own dedup set, so on
        startup both can land the same initial segments — whichever calls
        add_audio first wins, the second is a no-op rather than a stale
        duplicate that never gets consumed.

        Entries more than _MAX_AUDIO_EPOCH_LAG epochs behind the current
        audio epoch are pruned unconditionally, not just when the ring is
        over capacity, and so is anything more than _MAX_AUDIO_EPOCH_LAG
        behind the video loop's own current epoch (see set_video_epoch): a
        video-side lookup only ever matches the *current* epoch, so once
        video has moved permanently past N, nothing tagged epoch N can ever
        be consumed again, regardless of how recently it arrived.

        The video_epoch comparison uses the same lag tolerance as the
        audio_epoch one rather than an exact cutoff, on purpose: some ad
        pods carry a separate #EXT-X-DISCONTINUITY per stitched creative on
        the video rendition while the audio rendition tags far fewer of the
        same boundaries, so video_epoch can legitimately run 1-2 epochs
        ahead of whatever epoch fresh audio is currently arriving tagged
        with. An exact (zero-lag) cutoff there discards every fresh audio
        entry the instant it's added — before any video-side lookup, or the
        stale-epoch fallback in pop_audio_for_pts, can ever consume it —
        observed in production as several minutes of continuous silence
        that outlasted the ad pod itself. _video_prefetch_loop's resync
        check reads current_audio_epoch() directly rather than ring
        contents, so it isn't blocked by this lag; the tolerance here exists
        so pop_audio_for_pts still has something to find via the
        stale-epoch fallback (and ring_max_epoch() has real data for the
        short-wait check) while that resync — or the ordinary one-epoch
        tag-delay it's usually just waiting out — catches up.
        """
        with self._lock:
            if any(s == seq for _, _, s, _, _ in self._audio_queue):
                return
            self._audio_queue.append((pts, epoch, seq, data, time.monotonic()))
            max_age = self.seg_duration * _LATE_AUDIO_WAIT_FACTOR
            while (self._audio_queue
                    and (self._audio_queue[0][1] < self._audio_epoch - _MAX_AUDIO_EPOCH_LAG
                         or self._audio_queue[0][1] < self._video_epoch - _MAX_AUDIO_EPOCH_LAG
                         or time.monotonic() - self._audio_queue[0][4] > max_age)):
                self._audio_queue.popleft()
            while len(self._audio_queue) > _MAX_AUDIO_RING:
                self._audio_queue.popleft()

    def mark_duplicate(self, seq: int):
        """Record that *seq*'s video repeats the prior segment's video.

        _handle_playlist consults this to omit the segment from what it
        serves Enigma2, instead of advertising a chunk that would just play
        the same content over again.
        """
        with self._lock:
            self._dup_seqs.add(seq)
            self._dup_seqs = {s for s in self._dup_seqs if s >= seq - _SEQ_REPROCESS_WINDOW}

    def is_duplicate(self, seq: int) -> bool:
        with self._lock:
            return seq in self._dup_seqs

    def is_ready(self, seq: int) -> bool:
        """True once the video prefetch loop has actually processed *seq*
        (present in _ready_segs) - see _handle_playlist's use for why this
        matters beyond just "has content": is_duplicate(seq) is only
        meaningful once processing has completed, since mark_duplicate is
        called from the same iteration that populates _ready_segs.
        """
        with self._lock:
            return seq in self._ready_segs

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

    def mark_provisional(self, seq: int):
        """Record that _ready_segs[seq] is a silence-muxed placeholder with a
        _resolve_late_audio upgrade in flight, not the final AV mux.

        _handle_segment consults this so its existing wait loop actually
        waits for the upgrade instead of returning the placeholder the
        instant it sees any bytes at all in _ready_segs[seq].
        """
        with self._lock:
            self._provisional_segs.add(seq)
            self._provisional_segs = {
                s for s in self._provisional_segs if s >= seq - _SEQ_REPROCESS_WINDOW
            }

    def clear_provisional(self, seq: int):
        """Mark seq's _resolve_late_audio retry concluded (upgraded or not)."""
        with self._lock:
            self._provisional_segs.discard(seq)

    def is_provisional(self, seq: int) -> bool:
        with self._lock:
            return seq in self._provisional_segs

    def invalidate_segment(self, seq: int):
        """Drop any existing _ready_segs[seq] before reprocessing that seq.

        Some Pluto channels wrap their live playlist's media-sequence
        numbers back down and reuse them (see _SEQ_REPROCESS_WINDOW) - once
        a seq ages out of fetched_seqs's narrow dedup window, the loop
        below correctly treats a reappearance as a brand new segment and
        re-fetches/re-muxes it. But without this, the *stale* entry from
        the seq's earlier, completely different appearance stays sitting in
        _ready_segs, fully servable by _handle_segment's fast path, for the
        entire re-fetch+re-mux duration - a real request landing in that
        window gets the wrong (old) segment's bytes under the right-looking
        URL. Confirmed in production: exactly this made a live channel
        appear to flip between an ad and the real program and back,
        segment requests for the same reused seq number resolving to
        whichever generation of _ready_segs[seq] happened to be current at
        request time.
        """
        with self._lock:
            self._ready_segs.pop(seq, None)
            self._provisional_segs.discard(seq)

    def close(self):
        """Signal all pre-fetch threads to stop and wait for them to exit."""
        self._audio_prefetch_stop.set()
        self._video_prefetch_stop.set()
        for t in (self._audio_prefetch_thread, self._video_prefetch_thread):
            if t is not None and t.is_alive():
                t.join(timeout=5)

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


def _audio_prefetch_loop(channel_id: str, state: '_ChannelState'):
    """Background thread: continuously fill the per-channel audio PTS ring.

    Polls the audio rendition playlist, fetches any segments not yet in the
    ring, decrypts them if necessary, reads their first PTS, and stores them
    as  (pts, epoch, bytes)  entries.  epoch is bumped whenever a segment is
    preceded by #EXT-X-DISCONTINUITY, so video-side lookups can refuse to pair
    across a content transition even when PTS values alone would suggest a
    plausible match.  Old entries are evicted (oldest first) when the ring
    reaches _MAX_AUDIO_RING so memory stays bounded.
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
                epoch = state.assign_audio_epoch(seq, disc)
                to_fetch.append((seq, url, k, iv, epoch))

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

        def _fetch_one(seq, url, key_url, iv_explicit, epoch):
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
                    pts = _first_pts(adata)
                    if pts is None:
                        fail_reason = 'no PTS found in decrypted segment'
                        continue
                    if _attempt > 0:
                        logger.debug('%s/%s: Event: audio segment recovered on retry'
                                     ' (attempt 1 failed: %s)', channel_id, seq, fail_reason)
                    state.add_audio(pts, epoch, seq, adata)
                    fetched_seqs.add(seq)
                    return
                except Exception as exc:
                    fail_reason = f'{type(exc).__name__}: {exc}'
                    continue
            logger.debug('%s/%s: Event: audio segment unavailable after retry (%s)'
                         ' | Action: drop this segment, next poll will retry',
                         channel_id, seq, fail_reason)

        workers = min(_AUDIO_FETCH_WORKERS, len(to_fetch))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for args in to_fetch:
                pool.submit(_fetch_one, *args)

        if state._audio_prefetch_stop.wait(timeout=state.seg_duration / 5):
            break


def _resolve_late_audio(channel_id: str, state: '_ChannelState', seq: int,
                        vdata: bytes, v_pts: int, epoch: int,
                        allow_stale_epoch: bool = False):
    """Background upgrade for a segment muxed with silence after a zero-wait peek.

    _video_prefetch_loop takes a zero-wait peek at the audio ring — for the
    segment that itself introduces a new epoch, and for every later segment
    in that same epoch once one such peek has already confirmed it empty
    (see epoch_known_empty) — so a slow audio fetch never stalls the loop.
    This runs off to the side with the extended seg_duration ×
    _LATE_AUDIO_WAIT_FACTOR budget, giving the audio rendition's own disc tag
    for the same transition extra time to catch up with the video
    rendition's; if the same-epoch audio turns up in time and Enigma2
    hasn't already been served the silence version, it re-muxes and
    overwrites state._ready_segs[seq]
    in place. Safe to wait this long because it never blocks the critical
    path — only handle_segment's own deadline (sized to roughly match this
    budget) determines whether that window actually elapses before
    Enigma2's request forces a decision.

    *allow_stale_epoch* must stay False when this is the segment that itself
    just introduced *epoch*: every transition resets PTS to the same small
    values, so right there a leftover entry from a separate, already-
    concluded epoch can coincidentally land within tolerance despite being
    genuinely different content. The caller passes True for later segments
    in an already-running epoch, where that coincidence risk doesn't apply.
    """
    try:
        diag: dict = {}
        audio_pair = state.pop_audio_for_pts(
            v_pts, epoch, wait_secs=state.seg_duration * _LATE_AUDIO_WAIT_FACTOR,
            channel_id=channel_id, video_seq=seq, allow_stale_epoch=allow_stale_epoch,
            diag=diag)
        if audio_pair is None:
            if diag.get('stuck_ahead'):
                logger.debug('%s/%s: Event: no audio ever found for this segment'
                             ' (confirmed sliver: closest candidate stuck %s ticks ahead,'
                             ' kept as silence) | epoch=%s v_pts=%s',
                             channel_id, seq, diag.get('best_diff'), epoch, v_pts)
            else:
                logger.debug('%s/%s: Event: no audio ever found for this segment'
                             ' (unverified - not a confirmed sliver, kept as silence)'
                             ' | epoch=%s v_pts=%s best_diff=%s',
                             channel_id, seq, epoch, v_pts, diag.get('best_diff'))
            return
        with state._lock:
            if seq not in state._ready_segs:
                return
        _a_pts, a_seq, adata = audio_pair
        muxed = _ffmpeg_mux(vdata, adata, state, is_disc=True, track_cc=False)
        with state._lock:
            if seq in state._ready_segs:
                state._ready_segs[seq] = muxed
                state._cc_resync_pending = True
                logger.debug('%s/%s: late-audio upgrade applied (aseq=%s)',
                             channel_id, seq, a_seq)
    finally:
        state.clear_provisional(seq)


def _video_prefetch_loop(channel_id: str, state: '_ChannelState',
                         variant_idx: int):
    """Background thread: pre-fetch video segments, pair with audio, mux.

    Polls the video variant playlist, fetches each new segment (decrypting
    as needed), waits up to 8 s for the matching audio entry from the audio
    queue (safe because we're off the HTTP handler critical path), muxes with
    ffmpeg, and stores the result in state._ready_segs[seq].  _handle_segment
    then serves from that cache with no blocking wait.

    Combined with _handle_playlist's own readiness check (withholding a
    segment from the served playlist until this loop has actually muxed
    it) the muxed segment is normally ready well before Enigma2 is allowed
    to request it, eliminating audio dropouts caused by ordinary CDN audio
    lag. At a real content transition
    where the matching audio's own disc tag is late, _resolve_late_audio's
    extended retry (see _LATE_AUDIO_WAIT_FACTOR) and handle_segment's own
    deadline are what give that case a real chance to land in time too.

    variant_idx is re-resolved against state.variant_urls on every poll so
    that CDN URL rotations (signed-URL token refresh) are picked up without
    restarting the thread.
    """
    fetched_seqs: set = set()
    bumped_disc_seqs: set = set()
    last_v_pts: 'int | None' = None
    last_adata: 'bytes | None' = None
    last_a_seq: 'int | None' = None
    video_epoch = state.current_video_epoch()
    epoch_known_empty: 'int | None' = None
    poll_gen = 0
    pending_resync: 'tuple[int, int] | None' = None

    def _resync_up_if_confirmed(seq: int, audio_epoch_now: int) -> bool:
        nonlocal video_epoch, pending_resync
        if (pending_resync is not None and pending_resync[0] == audio_epoch_now  # pylint: disable=unsubscriptable-object
                and pending_resync[1] != poll_gen):  # pylint: disable=unsubscriptable-object
            logger.debug('%s/%s: Event: audio_epoch ahead of video_epoch,'
                         ' confirmed over a later poll (missed video-side disc marker)'
                         ' | Action: resync video_epoch %s -> %s',
                         channel_id, seq, video_epoch, audio_epoch_now)
            video_epoch = audio_epoch_now
            state.set_video_epoch(video_epoch)
            pending_resync = None
            return True
        if pending_resync is None or pending_resync[0] != audio_epoch_now:  # pylint: disable=unsubscriptable-object
            pending_resync = (audio_epoch_now, poll_gen)
        return False

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

        poll_gen += 1
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

            state.invalidate_segment(seq)

            pace_wait_start = None
            while seq > state.last_requested_seq() + _PREFETCH_LEAD_SEGMENTS:
                if pace_wait_start is None:
                    pace_wait_start = time.monotonic()
                    logger.debug('%s/%s: pacing - last requested seq is %s,'
                                 ' waiting for it to reach %s before muxing further ahead',
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
            pending_disc_ahead = any(all_discs[i + 1:i + 1 + _DISC_LOOKAHEAD_SEGMENTS])
            if is_disc and seq not in bumped_disc_seqs:
                bumped_disc_seqs.add(seq)
                last_v_pts = None
                last_adata = None
                last_a_seq = None
                epoch_known_empty = None
                video_epoch += 1
                state.set_video_epoch(video_epoch)
                pending_resync = None
                logger.debug('%s/%s: Event: CDN discontinuity tag | Action:'
                             ' reset PTS state, video_epoch -> %s (audio_epoch=%s)',
                             channel_id, seq, video_epoch, state.current_audio_epoch())

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

            v_pts = _first_pts(vdata)
            logger.debug('%s/%s: vpf v_pts=%s', channel_id, seq, v_pts)

            adata = None
            a_seq = None
            _reused = False
            _upgrade_pending = False
            _late_audio_allow_stale = False
            if v_pts is not None:
                if v_pts == last_v_pts and last_adata is not None:
                    adata = last_adata
                    a_seq = last_a_seq
                    _reused = True
                    state.mark_duplicate(seq)
                elif is_disc:
                    audio_pair = state.pop_audio_for_pts(
                        v_pts, video_epoch, wait_secs=0,
                        channel_id=channel_id, video_seq=seq,
                        allow_stale_epoch=False)
                    if audio_pair is None:
                        audio_epoch_now = state.current_audio_epoch()
                        if (audio_epoch_now > video_epoch and not pending_disc_ahead
                                and _resync_up_if_confirmed(seq, audio_epoch_now)):
                            audio_pair = state.pop_audio_for_pts(
                                v_pts, video_epoch, wait_secs=0,
                                channel_id=channel_id, video_seq=seq,
                                allow_stale_epoch=False)
                    if audio_pair is not None:
                        _a_pts, a_seq, adata = audio_pair
                    else:
                        _upgrade_pending = True
                else:
                    ring_epoch_max_pre = state.ring_max_epoch()
                    short_wait = (epoch_known_empty == video_epoch
                                  or (ring_epoch_max_pre is not None
                                      and ring_epoch_max_pre < video_epoch))
                    audio_pair = state.pop_audio_for_pts(
                        v_pts, video_epoch,
                        wait_secs=(0 if short_wait else None),
                        channel_id=channel_id, video_seq=seq)
                    if audio_pair is not None:
                        _a_pts, a_seq, adata = audio_pair
                        epoch_known_empty = None
                    else:
                        epoch_known_empty = video_epoch
                        _upgrade_pending = True
                        _late_audio_allow_stale = short_wait
                        audio_epoch_now = state.current_audio_epoch()
                        if audio_epoch_now > video_epoch and not pending_disc_ahead:
                            _resync_up_if_confirmed(seq, audio_epoch_now)
                        elif audio_epoch_now <= video_epoch - _MAX_AUDIO_EPOCH_LAG:
                            logger.debug('%s/%s: Event: audio_epoch persistently'
                                         ' behind video_epoch by >= %s (missed audio-side disc'
                                         ' marker) | Action: resync video_epoch %s -> %s',
                                         channel_id, seq, _MAX_AUDIO_EPOCH_LAG,
                                         video_epoch, audio_epoch_now)
                            video_epoch = audio_epoch_now
                            state.set_video_epoch(video_epoch)
            last_v_pts = v_pts
            last_adata = adata
            last_a_seq = a_seq

            reset_cc = is_disc
            with state._lock:
                if state._cc_resync_pending:
                    reset_cc = True
                    state._cc_resync_pending = False
            muxed = _ffmpeg_mux(vdata, adata, state, is_disc=is_disc, track_cc=True,
                                reset_cc=reset_cc)
            if _reused:
                _event = f'duplicate video PTS v_pts={v_pts}'
                _action = f'reuse previous audio (aseq={a_seq})'
            elif adata:
                _event = ('disc boundary, epoch-matched audio available' if is_disc
                          else f'audio matched (v_pts={v_pts})')
                _action = f'pair audio aseq={a_seq}'
            else:
                _event = ('disc boundary, no same-epoch audio yet (zero-wait peek)' if is_disc
                          else f'no audio matched (v_pts={v_pts})')
                _action = 'pair with silence, retry via late-audio resolver'
            logger.debug('%s/%s: Event: %s | Action: %s (%s bytes)',
                         channel_id, seq, _event, _action, len(muxed))

            with state._lock:
                state._ready_segs[seq] = muxed
                while len(state._ready_segs) > _MAX_SEG_CACHE:
                    state._ready_segs.popitem(last=False)
            fetched_seqs.add(seq)
            new_seqs += 1

            if _upgrade_pending:
                state.mark_provisional(seq)
                threading.Thread(
                    target=_resolve_late_audio,
                    args=(channel_id, state, seq, vdata, v_pts, video_epoch,
                          _late_audio_allow_stale),
                    daemon=True,
                    name=f'PlutoLateAudio-{channel_id[:8]}-{seq}',
                ).start()

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
            if new_sid is not None and new_sid != old_sid:
                with existing._lock:
                    existing._ready_segs.clear()
                    existing._provisional_segs.clear()
            was_closed = existing._audio_prefetch_stop.is_set()
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

_SEGMENT_FETCH_RETRIES = 1
_SEGMENT_FETCH_RETRY_DELAY = 0.3


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
        all_key_urls, all_ivs, _ = HLSPlaylist.segment_keys(vtext, vbase)

        _fill_key = all_key_urls[-1] if all_key_urls else None
        _fill_iv = all_ivs[-1] if all_ivs else None
        while len(all_key_urls) < len(vsegs):
            all_key_urls.append(_fill_key)
            all_ivs.append(_fill_iv)

        state.add_segments(vseq, vsegs, all_key_urls, all_ivs)

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

                def _pre_fetch_one(seq, url, key_url, iv_explicit, epoch):
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
                    pts = _first_pts(adata)
                    if pts is not None:
                        logger.debug('%s pre-pop audio seq=%s a_pts=%s epoch=%s',
                                     channel_id, seq, pts, epoch)
                        pre_results.append((pts, epoch, seq, adata))

                pre_results = []
                workers = min(_AUDIO_FETCH_WORKERS, max(1, len(asegs_pre)))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for i, url in enumerate(asegs_pre):
                        k = akey_pre[i] if i < len(akey_pre) else None
                        iv = aiv_pre[i] if i < len(aiv_pre) else None
                        disc = adisc_pre[i] if i < len(adisc_pre) else False
                        epoch = state.assign_audio_epoch(aseq_pre + i, disc)
                        pool.submit(_pre_fetch_one, aseq_pre + i, url, k, iv, epoch)
                for pts, epoch, seq, adata in sorted(pre_results, key=lambda x: (x[1], x[0])):
                    state.add_audio(pts, epoch, seq, adata)
                logger.debug('%s: pre-populated audio queue with %s entries',
                             channel_id, len(state._audio_queue))

        state.start_video_prefetch_if_needed(channel_id, idx)

        cold_start = not state.has_ready_segs()
        if cold_start:
            cold_deadline = time.monotonic() + _COLD_START_FIRST_SEGMENT_WAIT
            while not state.is_ready(vseq) and time.monotonic() < cold_deadline:
                time.sleep(0.05)

        out = []
        seg_idx = 0
        skip_current = False
        for line in vtext.splitlines():
            stripped = line.strip()
            if stripped.startswith('#EXT-X-KEY:'):
                pass
            elif stripped == '#EXT-X-DISCONTINUITY':
                pass
            elif stripped.startswith('#EXT-X-MEDIA-SEQUENCE:'):
                out.append(f'#EXT-X-MEDIA-SEQUENCE:{vseq}')
            elif stripped.startswith('#EXTINF:'):
                seq_i = vseq + seg_idx
                skip_current = state.is_duplicate(seq_i)
                if not skip_current and not cold_start and not state.is_ready(seq_i):
                    skip_current = True
                if not skip_current:
                    out.append('#EXT-X-DISCONTINUITY')
                    out.append(line)
            elif stripped and not stripped.startswith('#'):
                if not skip_current:
                    out.append(
                        f'http://{PROXY_HOST}:{PROXY_PORT}'
                        f'/seg/{channel_id}/{vseq + seg_idx}.ts'
                    )
                seg_idx += 1
                skip_current = False
            else:
                out.append(line)

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

        logger.debug('%s/%s: prefetch miss, on-demand fallback', channel_id, seq)
        pair = state.get_segment(seq)
        if not pair:
            if self._serveLastSegment(state, channel_id, seq, 'no cached segment info'):
                return
            self._send(404, b'Segment not in cache', 'text/plain')
            return

        video_url, key_url, iv_explicit = pair
        vdata = _fetch(video_url, binary=True, timeout=state.seg_duration * 0.8,
                       tag='handler:segment-fallback')
        for attempt in range(_SEGMENT_FETCH_RETRIES):
            if vdata:
                break
            time.sleep(_SEGMENT_FETCH_RETRY_DELAY)
            vdata = _fetch(video_url, binary=True, timeout=state.seg_duration * 0.8,
                           tag=f'handler:segment-fallback-retry{attempt + 1}')
        if not vdata:
            if self._serveLastSegment(state, channel_id, seq, 'on-demand fetch failed'):
                return
            self._send(502, b'Video segment unavailable', 'text/plain')
            return

        if key_url:
            key_bytes = AES128.fetch_key(key_url, _fetch)
            if key_bytes:
                vdata = AES128.decrypt(vdata, key_bytes,
                                       AES128.iv(iv_explicit, seq))

        adata = None
        a_seq = None
        if state.audio_url:
            v_pts = _first_pts(vdata)
            if v_pts is not None:
                audio_pair = state.pop_audio_for_pts(
                    v_pts, state.current_audio_epoch(), wait_secs=0,
                    channel_id=channel_id, video_seq=seq,
                    allow_stale_epoch=False)
                if audio_pair is not None:
                    _a_pts, a_seq, adata = audio_pair

        merged = _ffmpeg_mux(vdata, adata, state, is_disc=True, track_cc=False)
        logger.debug('%s/%s: fallback %s bytes aseq=%s',
                     channel_id, seq, len(merged), a_seq)
        state._last_segment_out = (seq, merged)
        self._send(200, merged, 'video/MP2T')

    def _serveLastSegment(self, state, channel_id, seq, reason):
        """Re-serve the last segment actually sent to Enigma2, in place of a
        hard 404/502, if we have one. See state._last_segment_out's comment -
        one repeated stale segment is a brief visible stutter, but far better
        than risking hlsdemux wedging on a hard error with no recourse but a
        manual re-zap. Returns True if it served something (caller should
        return without sending its own error response).
        """
        last = state._last_segment_out
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
