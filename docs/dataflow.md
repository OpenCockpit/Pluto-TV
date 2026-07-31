# PlutoProxy data flow

High-level reference for how a live channel goes from Enigma2's tune request
to muxed MPEG-TS segments, and how a recording of that same channel gets a
container-continuous re-mux instead. Source: `src/PlutoProxy.py` and
`src/RecordingProxy.py`, entry points in `src/PlutoRequest.py`.

## Why this exists

Pluto TV serves many live channels as **demuxed** HLS: the master playlist
references a video-only variant plus a separate `EXT-X-MEDIA:TYPE=AUDIO`
rendition with its own playlist and TS segments. Enigma2's DVB hardware
pipeline (servicemp3/GStreamer → DVB-S2 demux) expects a single muxed
MPEG-TS per segment. PlutoProxy sits between Enigma2 and the real CDN as a
local HTTP server (`127.0.0.1:7654`), rewriting playlists to point back at
itself and muxing video+audio together with `ffmpeg` before Enigma2 ever
sees a segment.

Channels that are *not* demuxed are detected and redirected straight to the
CDN (no muxing needed).

Recording a channel needs something extra on top: Enigma2's recording
pipeline writes whatever raw MPEG-TS bytes it receives straight to a file
with no further remuxing, so it needs a single continuous byte stream
instead of independently-served ~5s HLS segments. `RecordingProxy.py`
handles that (see its own section below) by reusing the *same* per-segment
muxed bytes PlutoProxy already produces for live playback.

## Entry points

```
Live playback:
  Enigma2 tunes a channel
    → PlutoRequest.buildStreamURL(channel_id)
        → active_channel_url(channel_id) - reuse if already running
          (prevents a second session with a different pool-slot device
          identity from silently repointing an already-live channel)
        → PlutoProxy.start()              (idempotent: starts the HTTP server once)
        → PlutoProxy.register_channel(channel_id, real_master_url)
            → creates a fresh _ChannelState, stores it in _channels[channel_id]
            → closes the previously-active *different* channel's threads (zap cleanup)
            → returns http://127.0.0.1:7654/auto/{channel_id}.m3u8
    → Enigma2 (eServiceMP3) plays that local URL

Recording:
  Enigma2 (or instant-record) resolves a recording sref
    → PlutoRequest.recordServiceExtension(sref)
        → _resolve_pluto_sref() - same register_channel() call as live
          playback, so the pre-fetch threads are already running (or get
          started) regardless of whether anyone is watching live
        → rewrites the sref to http://127.0.0.1:7654/rec/{channel_id}.ts
    → Enigma2's recording pipeline (eServiceMP3Record) reads that URL
      straight through to the file, no muxing/demuxing of its own
```

## Request flow (HLSProxyHandler)

```
 /auto/{id}.m3u8 ──────────────────────────────────────────────────────────
   fetch real master playlist
   muxed stream?  ──yes──▶ 302 redirect straight to the CDN (no proxying)
        │no (has EXT-X-MEDIA:TYPE=AUDIO)
        ▼
 /master/{id}.m3u8  (or inline, when reached via /auto)
   - strips EXT-X-MEDIA lines, remembers the audio rendition URL
   - rewrites each EXT-X-STREAM-INF's variant URL → /pl/{id}/{idx}.m3u8
   - starts the audio pre-fetch thread (once)
        ▼
 /pl/{id}/{idx}.m3u8                              [requested every ~seg_duration]
   - fetches the real video variant playlist
   - registers segment URLs/keys in state.segments
   - on the FIRST call only: synchronously pre-populates the audio ring
   - starts the video pre-fetch+mux thread (once)
   - rewrites segment lines → /seg/{id}/{seq}.ts, injects EXT-X-DISCONTINUITY
     on every segment (not just real transitions - see PlutoTVCockpit
     project memory on this)
   - omits segments the video pre-fetch loop already confirmed as either a
     duplicate repeat of the prior one, or not yet muxed at all (a short,
     one-poll-cycle withholding - see is_ready()/cold_start below); a fixed
     "always withhold the newest N segments" head start (`_PLAYLIST_TAIL_DROP`)
     used to do this job but was removed once this readiness check made it
     redundant
        ▼
 /seg/{id}/{seq}.ts
   - fast path: pop the pre-muxed bytes out of state._ready_segs
   - fallback (cache miss): fetch+decrypt video on demand, grab whatever
     audio is immediately available (no wait), mux, serve

 /rec/{id}.ts  ── see "Recording pipeline" below; does not go through
                  /master, /pl, or /seg at all
```

## Background threads (per channel)

Two daemon threads do all the real work off the HTTP request path:

```
 _audio_prefetch_loop                    _video_prefetch_loop
 ────────────────────                    ────────────────────
 poll audio rendition playlist           poll video variant playlist
 fetch + decrypt new segments            fetch + decrypt new segments
 tag each with (pts, epoch, seq)         on a disc boundary: bump video_epoch
 push into state._audio_queue            pair this segment's PTS against
 (FIFO ring, capped at 30,                 state._audio_queue (same epoch only)
  deduped by seq)                        mux video+audio (or +silence) via ffmpeg,
                                          chaining PCR/PTS/continuity-counters onto
                                          the previous segment (see ffmpeg muxing)
                                          store result in state._ready_segs[seq]
```

They communicate only through `_ChannelState`, guarded by one lock per
channel (`state._lock`):

| Field                  | Written by                                     | Read by                                                    |
|------------------------|-------------------------------------------------|--------------------------------------------------------------|
| `segments`             | `_handle_playlist`                              | `_handle_segment` fallback                                  |
| `_audio_queue`         | audio pre-fetch thread                          | `pop_audio_for_pts` (video thread, late-audio upgrade, handler fallback) |
| `_ready_segs`          | video pre-fetch thread, `_resolve_late_audio`   | `_handle_segment`, `RecordingSession` (see below)            |
| `_provisional_segs`    | video pre-fetch thread (`mark_provisional`)     | `_handle_segment`, `RecordingSession` (hold off serving a silence-muxed placeholder until upgraded or timed out) |
| `_dup_seqs`            | video pre-fetch thread                          | `_handle_playlist` (omits the segment), `RecordingSession` (skips it) |
| `_audio_epoch`         | both (via `assign_audio_epoch`)                 | both (epoch-gated pairing)                                    |
| `_video_epoch`         | video pre-fetch thread (`set_video_epoch`)      | audio thread's ring eviction, resync checks                   |
| `_cc_state`            | video pre-fetch thread only (`track_cc=True`)   | `_ffmpeg_mux`/`_patch_continuity` (chains continuity counters across segments) |
| `_cc_resync_pending`   | `_resolve_late_audio`                           | video pre-fetch thread (forces the *next* segment's CC chain to restart, without falsely flagging it as a content transition - see ffmpeg muxing) |

## Audio/video pairing: epochs, not just PTS

Both renditions are fetched independently by two unsynchronized loops, so
PTS values alone cannot prove two segments belong to the same piece of
content — some CDN splices keep a continuous clock across a cut. Each
rendition's own `#EXT-X-DISCONTINUITY` tags increment a shared **epoch**
counter; `pop_audio_for_pts` only considers audio entries whose epoch
matches the video segment's epoch, regardless of how close the PTS values
look.

```
 disc boundary on video → video_epoch += 1, reset duplicate-frame state
 disc boundary on audio → state._audio_epoch += 1 (shared, via assign_audio_epoch -
                           resolved exactly once per seq, so two independent
                           parsers of the same playlist - the one-shot
                           pre-population pass and the ongoing audio
                           pre-fetch thread - never double-bump it)

 pop_audio_for_pts(v_pts, epoch):
   scan _audio_queue for entries where entry.epoch == epoch
   pick the closest PTS, whether or not it's within tolerance
   within tolerance (seg_duration/2 in 90kHz ticks) → pop it, return (pts, seq, data)
   not found by the wait deadline → return None  (caller muxes with silence)
   closest candidate sits *ahead* of v_pts and outside tolerance the whole
     time → "stuck ahead": no future wait can produce anything closer (the
     ring only ever gains higher-PTS entries), so this fails fast instead of
     burning the full wait budget
```

There's no separate state-machine layer summarizing which of these is
currently happening - a per-channel `Steady`/`Disc Boundary`/`Late-Audio
Wait`/`Resync` tracker existed for a while purely to log transitions, but
every one of its transition points already sits next to a more specific
`Event: ... | Action: ...` print (disc boundary, resync, late-audio
upgrade, ...), which is what actually carries the diagnostic signal when
reading a production log - the coarser state layer was removed as dead
weight on top of it.

Special cases layered on top of the basic pairing:

- **Duplicate video frame** (CDN repeats the final frame at a cut): reuse
  the previous segment's audio instead of pairing again.
- **Right at a disc boundary**: take a zero-wait peek only (don't block the
  loop waiting for audio that may not have updated its epoch yet), mux with
  silence immediately, and fire `_resolve_late_audio` in the background to
  retry with the full wait budget and upgrade `_ready_segs[seq]` in place if
  real audio turns up before Enigma2 requests it.
- **Video epoch resync**: if every entry left in the audio ring already has
  a *higher* epoch than the video side expects, the video rendition missed
  its own disc marker for that transition — waiting longer can't help, so
  `video_epoch` jumps forward to the ring's floor instead of mismatching
  every subsequent segment. The reverse (audio persistently behind video by
  more than a small lag tolerance) resyncs `video_epoch` back down the same
  way.
- **Stale-epoch fallback**: if nothing in the *current* epoch matches by the
  deadline, one last look at the *previous* epoch's leftovers (bounded by
  the same PTS tolerance) covers the case where the audio rendition's own
  disc tag simply landed a beat later than the video rendition's for the
  same real transition - not disabled for the disc-introducing segment
  itself, where a coincidental PTS collision with unrelated already-concluded
  content is a real risk right at the reset-to-small-values moment.
- **Seq number reuse (ad-pod/filler loops)**: some channels serve a stitched
  filler/ad-pod loop whose playlist media-sequence numbers wrap back down
  and repeat rather than growing monotonically forever. `assign_audio_epoch`,
  `mark_duplicate`, and `mark_provisional` all forget a seq after
  `_SEQ_REPROCESS_WINDOW` (10) segments - the same short horizon the video
  pre-fetch loop's own `fetched_seqs`/`bumped_disc_seqs` already use - so a
  reappearing seq gets its `#EXT-X-DISCONTINUITY` flag (and duplicate/
  provisional status) re-evaluated fresh instead of silently reusing
  whatever was decided the first time that seq number was seen. Confirmed in
  production at the old, much wider `_MAX_SEG_CACHE` window: `_audio_epoch`
  stayed stuck on a repeated seq while `video_epoch` correctly kept
  advancing each loop, and `add_audio` then pruned every freshly-fetched
  real audio segment as too far behind `video_epoch` - most of the channel's
  audio muxed with silence for minutes at a stretch.
- A sustained mismatch with no matching audio arriving at all (e.g. a
  CDN-side splice where the audio rendition emits one fewer segment than
  video across the cut) has no fix on the proxy side beyond the fail-fast
  above: there is no correct audio to pair, so that one segment permanently
  gets silence and every later segment re-pairs one slot down, back to
  normal.

## ffmpeg muxing (`_ffmpeg_mux`)

Given video bytes and (optionally) audio bytes:

1. Write each to a temp file (ffmpeg needs seekable input to probe `start_time`).
2. Compute `itsoffset` from the PTS difference between the two streams.
3. `-c copy -copyts` both streams into one MPEG-TS (`-map 0:v:0 -map 1:a:0`).
   `-copyts` preserves each stream's real absolute PCR/PTS instead of
   ffmpeg's default of rebasing every independent mux near zero - since the
   CDN's timeline is already continuous across ordinary segment boundaries,
   this alone stops PCR/PTS from resetting every ~5s.
4. `_patch_continuity` then chains each segment's continuity counters onto
   the previous segment's ending values per PID (`state._cc_state`), since
   a fresh ffmpeg process always starts CC at 0 regardless of `-copyts`.
5. Two independent flags control the remaining per-segment behavior:
   - `is_disc` - real CDN `#EXT-X-DISCONTINUITY` only. Sets
     `-mpegts_flags initial_discontinuity`, the one flag consumers (live
     GStreamer's hlsdemux, and RecordingProxy's timeline rewriter) ever see
     baked into the TS bytes to detect a genuine content transition.
   - `reset_cc` - whether `_patch_continuity` restarts the CC chain at `{}`.
     Defaults to `is_disc`, but can be forced independently (see
     `_cc_resync_pending` in the table above) when a *bookkeeping* CC
     restart is needed without also telling downstream consumers a real cut
     happened.
6. No audio available but video PTS is readable → inject a silent AAC track
   (`anullsrc`) at the video's PTS instead of shipping video-only: Enigma2
   permanently drops the audio track on any video-only segment, so silence
   is what keeps the decoder alive until real audio returns.
7. Any ffmpeg failure → fall back to returning the raw video bytes unchanged.

Only the primary, strictly-in-order video pre-fetch loop call chains into
`state._cc_state` (`track_cc=True`); the late-audio upgrade and the
synchronous on-demand fallback both mux with `track_cc=False` since they can
land out of order relative to that chain, and flag their own segment as
`is_disc=True` instead (they're always overwriting/serving the disc-adjacent
segment itself).

## Recording pipeline (`RecordingProxy.py`)

Enigma2's recording pipeline (`eServiceMP3Record`) writes whatever raw
MPEG-TS bytes arrive straight to a file, with no remuxing of its own - so
concatenating independently-muxed ~5s segments raw would fragment the
recording's PAT/PMT/PCR/continuity-counters every ~5s (invisible live, but
exactly what breaks seeking/trick-play/duration on playback later). Because
`_ffmpeg_mux` now already produces a chained, container-continuous stream
in `state._ready_segs` (see above), `RecordingProxy` doesn't remux at all -
it just concatenates those same bytes, in order, straight into the
response:

```
 handle_recording(handler, channel_id)
   _ensure_pipeline(channel_id, state)     # start prefetch threads if this
                                            # recording has no live viewer
                                            # driving /master or /pl already
   RecordingSession(channel_id, state)
     _feed_loop (background thread):
       walk state._ready_segs forward from the live edge, in seq order
       skip duplicates (state.is_duplicate)
       wait (briefly) for a still-provisional (silence) segment to be
         upgraded by _resolve_late_audio; serve it anyway past a deadline
       _continuize(muxed) → push onto a bounded queue
     read_output() (HTTP handler thread): drains that queue back to Enigma2
```

`_continuize` is the one thing this module still does itself, and only for
`is_disc` segments: live playback *wants* the real, correctly-discontinuous
PCR/PTS at a genuine transition (GStreamer's hlsdemux is built to resync on
it), but a recording is read start-to-end by a single decoder that doesn't
expect the clock to jump - observed as a multi-second video stall at every
ad transition, with audio (more tolerant of a DTS jump) playing through it.
`_rewrite_timeline` shifts every PCR/PTS/DTS field in an `is_disc` segment
by a running offset so the recording's own timeline stays continuous,
without ever touching `state._ready_segs` itself (live playback is
untouched). Because `is_disc`/`reset_cc` are now separate flags upstream
(see ffmpeg muxing above), a CC-only bookkeeping restart never gets
mistaken here for a real transition needing a timeline shift.

## Channel lifecycle / zapping

`register_channel` always closes the *previously active* channel's threads
on every zap (not just on re-tuning the same channel), so switching away
from a channel stops its audio/video pre-fetch threads and their ffmpeg
calls instead of leaving them running against the CDN indefinitely. The old
`_ChannelState` is left in `_channels` (not deleted) so any in-flight
request for it during teardown still finds cached state instead of a 404.
`active_channel_url` lets a caller check whether a channel already has a
*running* session before minting a new one (see `buildStreamURL` above) -
important because two independent sessions for "the same channel" (e.g. a
different pool-slot device identity) aren't guaranteed to share an absolute
PTS timeline or ad-stitching state.
