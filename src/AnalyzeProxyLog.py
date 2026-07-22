#!/usr/bin/env python3
# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0
#
#   AnalyzeProxyLog - dev-only command-line tool that parses LiveProxy's
#   own logger.debug() lines out of an Enigma2 log file and reports whether
#   the continuous-buffer window-cutting and late-audio-recovery mechanism
#   in LiveProxy.py is behaving: silence-muxed output windows that never
#   got a late-audio upgrade, and any error-like lines.
#
#   Regexes below match the windowed-mux engine (chunk_seq-keyed
#   _cut_and_mux_next_window/_slice_audio_for_window/
#   _resolve_late_audio_window, see LiveProxy.py) - they will not match
#   logs from the older per-CDN-segment pop_audio_for_pts engine. Two
#   metrics that mechanism used to report no longer apply and are expected
#   to always read 0 against a new-engine log: "Epoch resyncs" (the
#   video/audio epoch-counter resync dance was eliminated - see
#   _slice_audio_for_window's docstring) and "Served via on-demand fallback
#   path" (the old synchronous CDN-segment reconstruction fallback was
#   replaced by a narrower extended-wait-then-reserve-last-chunk fallback -
#   see _handle_segment's comment on that tradeoff).
#
#   Not part of the installed plugin (see tools placement note in
#   src/Makefile.am's install_PYTHON) - run it by hand against a copy of
#   an Enigma2_debug_*.log, e.g.:
#       python3 AnalyzeProxyLog.py /path/to/Enigma2_debug_2026-06-27.log

import argparse
import re

PREFIX = r"PTV: \w+: LiveProxy\.py: \w+: "

SILENCE_RE = re.compile(PREFIX + r"(\S+)/(\d+): Event: window cut epoch=\d+ pts=\[\d+,\d+\)"
                        r" is_disc=\S+ audio_covered=False \| Action: mux \((\d+) bytes\)")
UPGRADE_RE = re.compile(PREFIX + r"(\S+)/(\d+): late-audio window upgrade applied")
SLIVER_RE = re.compile(PREFIX + r"(\S+)/(\d+): Event: no audio ever covered this window")
DISC_RE = re.compile(PREFIX + r"(\S+)/(\d+): Event: CDN discontinuity tag \| Action:"
                     r" flush partial window, video_epoch -> (\d+) \(audio_epoch=(\d+)\)")
DUPLICATE_DROPPED_RE = re.compile(PREFIX + r"(\S+)/(\d+): Event: duplicate video PTS"
                                  r" \(CDN frame-repeat at splice\) v_pts=(\d+)")
RESYNC_RE = re.compile(PREFIX + r"(\S+)/(\d+): Event: (.+?) \| Action: resync video_epoch (\d+) -> (\d+)")
PROVISIONAL_CACHE_HIT_RE = re.compile(PREFIX + r"(\S+)/(\d+): cache hit (\d+) bytes \(provisional, upgrade never landed\)")
CACHE_HIT_RE = re.compile(PREFIX + r"(\S+)/(\d+): cache hit (\d+) bytes(?! \(provisional)")
FALLBACK_RE = re.compile(PREFIX + r"(\S+)/(\d+): fallback (\d+) bytes aseq=(\S+)")
PREFETCH_MISS_RE = re.compile(PREFIX + r"(\S+)/(\d+): Event: chunk never produced by prefetch loop")
STALE_EPOCH_RE = re.compile(PREFIX + r"(\S+)/(\d+): audio window matched epoch=(\d+) .*\(stale-epoch fallback\)")
STALE_RESERVE_RE = re.compile(PREFIX + r"(\S+)/(\d+): Event: (.+?)"
                              r" \| Action: re-serving last segment \((\d+)\) instead of erroring")
MUX_EXCEPTION_RE = re.compile(PREFIX + r"_ffmpeg_mux exception: (.+)")
MUX_FAIL_RE = re.compile(PREFIX + r"_ffmpeg_mux (\S+) rc=(-?\d+) out=(\d+)b: (.*)")
TEMPFILE_FAIL_RE = re.compile(PREFIX + r"temp file write failed: (.+)")
ITSOFFSET_RE = re.compile(PREFIX + r"_ffmpeg_mux: pts v=(\d+) a=(\d+) itsoffset=(-?[\d.]+)s")
ERROR_RE = re.compile(r"error|Error|Traceback|Exception|exception|failed|Failed")


def analyze(path: str, example_limit: int = 20):
    silence = {}
    upgraded = set()
    slivers = set()
    first_serve = {}
    cache_hits = []
    fallback_serves = []
    prefetch_misses = []
    disc_boundaries = []
    duplicates_dropped = []
    resyncs = []
    stale_epoch_matches = []
    stale_reserves = []
    mux_failures = []
    itsoffset_events = []
    errors = []

    channel_order = []
    seen_channels = set()

    def track(channel):
        if channel not in seen_channels:
            seen_channels.add(channel)
            channel_order.append(channel)

    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            m = SILENCE_RE.search(line)
            if m:
                track(m.group(1))
                silence[(m.group(1), int(m.group(2)))] = int(m.group(3))
                continue
            m = UPGRADE_RE.search(line)
            if m:
                track(m.group(1))
                upgraded.add((m.group(1), int(m.group(2))))
                continue
            m = SLIVER_RE.search(line)
            if m:
                track(m.group(1))
                slivers.add((m.group(1), int(m.group(2))))
                continue
            m = DISC_RE.search(line)
            if m:
                track(m.group(1))
                disc_boundaries.append((m.group(1), int(m.group(2)), m.group(3), m.group(4)))
                continue
            m = DUPLICATE_DROPPED_RE.search(line)
            if m:
                track(m.group(1))
                duplicates_dropped.append((m.group(1), int(m.group(2)), m.group(3)))
                continue
            m = RESYNC_RE.search(line)
            if m:
                track(m.group(1))
                resyncs.append((m.group(1), int(m.group(2)), m.group(4), m.group(5), m.group(3)))
                continue
            m = PROVISIONAL_CACHE_HIT_RE.search(line)
            if m:
                track(m.group(1))
                key = (m.group(1), int(m.group(2)))
                cache_hits.append(key)
                first_serve.setdefault(key, (int(m.group(3)), True))
                continue
            m = CACHE_HIT_RE.search(line)
            if m:
                track(m.group(1))
                key = (m.group(1), int(m.group(2)))
                cache_hits.append(key)
                first_serve.setdefault(key, (int(m.group(3)), False))
                continue
            m = FALLBACK_RE.search(line)
            if m:
                track(m.group(1))
                fallback_serves.append((m.group(1), int(m.group(2)), m.group(4)))
                continue
            m = PREFETCH_MISS_RE.search(line)
            if m:
                track(m.group(1))
                prefetch_misses.append((m.group(1), int(m.group(2))))
                continue
            m = STALE_EPOCH_RE.search(line)
            if m:
                track(m.group(1))
                stale_epoch_matches.append((m.group(1), int(m.group(2)), m.group(3)))
                continue
            m = STALE_RESERVE_RE.search(line)
            if m:
                track(m.group(1))
                stale_reserves.append((m.group(1), int(m.group(2)), m.group(3), int(m.group(4))))
                continue
            m = MUX_EXCEPTION_RE.search(line) or MUX_FAIL_RE.search(line) or TEMPFILE_FAIL_RE.search(line)
            if m:
                mux_failures.append(line.strip())
                continue
            m = ITSOFFSET_RE.search(line)
            if m:
                itsoffset_events.append((m.group(1), m.group(2), m.group(3)))
                continue
            if 'LiveProxy' in line and ERROR_RE.search(line):
                errors.append(line.strip())

    print(f"Channels found ({len(channel_order)}): {', '.join(channel_order)}")

    for channel in channel_order:
        ch_silence = {k: v for k, v in silence.items() if k[0] == channel}
        ch_upgraded = {k for k in upgraded if k[0] == channel}
        ch_never_upgraded = sorted(seq for (_, seq) in ch_silence if (channel, seq) not in ch_upgraded)
        ch_slivers = sorted(seq for (ch, seq) in slivers if ch == channel)
        ch_duplicates = [d for d in duplicates_dropped if d[0] == channel]
        ch_served_silence = sorted(
            seq for (_, seq), muxed_bytes in ch_silence.items()
            if (fs := first_serve.get((channel, seq))) is not None
            and (fs[1] or fs[0] == muxed_bytes)
        )
        ch_disc = [d for d in disc_boundaries if d[0] == channel]
        ch_resync = [r for r in resyncs if r[0] == channel]
        ch_cache_hits = [c for c in cache_hits if c[0] == channel]
        ch_fallback = [fb for fb in fallback_serves if fb[0] == channel]
        ch_prefetch_miss = [p for p in prefetch_misses if p[0] == channel]
        ch_stale_epoch = [s for s in stale_epoch_matches if s[0] == channel]
        ch_stale_reserve = [r for r in stale_reserves if r[0] == channel]

        ch_unresolved = sorted(set(ch_never_upgraded) - set(ch_slivers))

        print(f"\n=== Channel {channel} ===")
        print(f"Silence-muxed windows this session (no audio covered at cut time,"
              f" each one spawns exactly one late-audio retry): {len(ch_silence)}")
        print(f"  Retry succeeded (real audio swapped in before being served): {len(ch_upgraded)}")
        print(f"  Retry gave up (confirmed: no audio ever covered the window): {len(ch_slivers)}")
        if ch_unresolved:
            print(f"  Still unresolved as of end of log: {len(ch_unresolved)}  {ch_unresolved[:example_limit]}")
        print(f"Silence segments actually SERVED as silence ("
              f" regardless of whether a retry later succeeded): {len(ch_served_silence)}")
        for seq in ch_served_silence[:example_limit]:
            print(f"  chunk_seq={seq}")

        print(f"\nDisc boundaries seen: {len(ch_disc)}")
        print(f"Epoch resyncs: {len(ch_resync)}")
        for r in ch_resync:
            print("  resync:", r)
        print(f"Stale-epoch fallback matches (audio reused from the prior epoch): {len(ch_stale_epoch)}")
        for _ch, s_seq, s_epoch in ch_stale_epoch[:example_limit]:
            print(f"  chunk_seq={s_seq} matched_epoch={s_epoch}")

        print(f"\nServed from cache (cache hit): {len(ch_cache_hits)}")
        print(f"Served via on-demand fallback path: {len(ch_fallback)}")
        print(f"Prefetch-miss events (chunk never produced, extended-wait/last-chunk fallback): {len(ch_prefetch_miss)}")
        print(f"Last-resort stale re-serves (no chunk available at all, repeated a prior one): {len(ch_stale_reserve)}")
        for _ch, seq, reason, reused_seq in ch_stale_reserve[:example_limit]:
            print(f"  chunk_seq={seq} reason={reason!r} repeated_chunk_seq={reused_seq}")
        print(f"Duplicate video segments dropped (CDN frame-repeat at splice, not appended): {len(ch_duplicates)}")

        print(f"\nFirst {example_limit} never-upgraded silence windows:")
        for seq in ch_never_upgraded[:example_limit]:
            if seq in ch_slivers:
                status = "confirmed: _resolve_late_audio_window gave up, no audio ever covered this window"
            else:
                status = "retry unresolved as of end of log - still in flight, or evicted before it could conclude"
            print(f"  chunk_seq={seq}: {status}")

    print(f"\nMux/tempfile failures (session-wide): {len(mux_failures)}")
    for f in mux_failures[:example_limit]:
        print("  ", f)

    print(f"\nPTS itsoffset-drift events (session-wide, >20ms pre-correction gap): {len(itsoffset_events)}")
    for v_pts, a_pts, offset in itsoffset_events[:example_limit]:
        print(f"   v_pts={v_pts} a_pts={a_pts} itsoffset={offset}s")

    print(f"\nError-like LiveProxy lines (session-wide, not split by channel): {len(errors)}")
    for e in errors[:example_limit]:
        print("  ", e)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logfile', help='Path to an Enigma2_debug_*.log file')
    parser.add_argument('--limit', type=int, default=20,
                        help='Max example lines to print per section (default: 20)')
    args = parser.parse_args()
    analyze(args.logfile, args.limit)


if __name__ == '__main__':
    main()
