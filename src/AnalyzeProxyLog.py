#!/usr/bin/env python3
# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0
#
#   AnalyzeProxyLog - dev-only command-line tool that parses LiveProxy's
#   own logger.debug() lines out of an Enigma2 log file and reports whether
#   the video/audio epoch-sync and late-audio-recovery mechanism in
#   LiveProxy.py is behaving: epoch resyncs, silence-muxed segments that
#   never got a late-audio upgrade, and any error-like lines.
#
#   Not part of the installed plugin (see tools placement note in
#   src/Makefile.am's install_PYTHON) - run it by hand against a copy of
#   an Enigma2_debug_*.log, e.g.:
#       python3 AnalyzeProxyLog.py /path/to/Enigma2_debug_2026-06-27.log

import argparse
import re
from collections import defaultdict

PREFIX = r"PTV: \w+: LiveProxy\.py: \w+: "

SILENCE_RE = re.compile(PREFIX + r"(\S+)/(\d+): Event: .*\| Action: pair with silence.*\((\d+) bytes\)")
UPGRADE_RE = re.compile(PREFIX + r"(\S+)/(\d+): late-audio upgrade applied \(aseq=(\d+)\)")
SLIVER_RE = re.compile(PREFIX + r"(\S+)/(\d+): Event: no audio ever found for this segment"
                       r" \(confirmed sliver")
DISC_RE = re.compile(PREFIX + r"(\S+)/(\d+): Event: CDN discontinuity tag \| Action: reset PTS state, video_epoch -> (\d+) \(audio_epoch=(\d+)\)")
MISS_RE = re.compile(PREFIX + r"(\S+)/(\d+): audio MISS")
RESYNC_RE = re.compile(PREFIX + r"(\S+)/(\d+): Event: (.+?) \| Action: resync video_epoch (\d+) -> (\d+)")
PROVISIONAL_CACHE_HIT_RE = re.compile(PREFIX + r"(\S+)/(\d+): cache hit (\d+) bytes \(provisional, upgrade never landed\)")
CACHE_HIT_RE = re.compile(PREFIX + r"(\S+)/(\d+): cache hit (\d+) bytes(?! \(provisional)")
FALLBACK_RE = re.compile(PREFIX + r"(\S+)/(\d+): fallback (\d+) bytes aseq=(\S+)")
PREFETCH_MISS_RE = re.compile(PREFIX + r"(\S+)/(\d+): prefetch miss")
STALE_EPOCH_RE = re.compile(PREFIX + r"(\S+)/(\d+): audio match aseq=(\d+) .*\(stale-epoch fallback\)")
MUX_EXCEPTION_RE = re.compile(PREFIX + r"_ffmpeg_mux exception: (.+)")
MUX_FAIL_RE = re.compile(PREFIX + r"_ffmpeg_mux (\S+) rc=(-?\d+) out=(\d+)b: (.*)")
TEMPFILE_FAIL_RE = re.compile(PREFIX + r"temp file write failed: (.+)")
ITSOFFSET_RE = re.compile(PREFIX + r"_ffmpeg_mux: pts v=(\d+) a=(\d+) itsoffset=(-?[\d.]+)s")
ERROR_RE = re.compile(r"error|Error|Traceback|Exception|exception|failed|Failed")


def analyze(path: str, example_limit: int = 20):
    silence = {}
    upgraded = {}
    slivers = set()
    first_serve = {}
    cache_hits = []
    fallback_serves = []
    prefetch_misses = []
    disc_boundaries = []
    resyncs = []
    miss_count = defaultdict(int)
    stale_epoch_matches = []
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
                upgraded[(m.group(1), int(m.group(2)))] = m.group(3)
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
            m = RESYNC_RE.search(line)
            if m:
                track(m.group(1))
                resyncs.append((m.group(1), int(m.group(2)), m.group(4), m.group(5), m.group(3)))
                continue
            m = MISS_RE.search(line)
            if m:
                track(m.group(1))
                miss_count[(m.group(1), int(m.group(2)))] += 1
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
        ch_upgraded = {k: v for k, v in upgraded.items() if k[0] == channel}
        ch_never_upgraded = sorted(seq for (_, seq) in ch_silence if (channel, seq) not in ch_upgraded)
        ch_slivers = sorted(seq for (ch, seq) in slivers if ch == channel)
        ch_served_silence = sorted(
            seq for (_, seq), muxed_bytes in ch_silence.items()
            if (fs := first_serve.get((channel, seq))) is not None
            and (fs[1] or fs[0] == muxed_bytes)
        )
        ch_miss_total = sum(v for k, v in miss_count.items() if k[0] == channel)
        ch_disc = [d for d in disc_boundaries if d[0] == channel]
        ch_resync = [r for r in resyncs if r[0] == channel]
        ch_cache_hits = [c for c in cache_hits if c[0] == channel]
        ch_fallback = [fb for fb in fallback_serves if fb[0] == channel]
        ch_prefetch_miss = [p for p in prefetch_misses if p[0] == channel]
        ch_stale_epoch = [s for s in stale_epoch_matches if s[0] == channel]

        print(f"\n=== Channel {channel} ===")
        print(f"Total silence-muxed segments: {len(ch_silence)}")
        print(f"Total late-audio-upgrade events: {len(ch_upgraded)}")
        print(f"Silence segments with NO later upgrade: {len(ch_never_upgraded)}")
        print(f"Silence segments actually SERVED as silence ("
              f" regardless of a later upgrade): {len(ch_served_silence)}")
        for seq in ch_served_silence[:example_limit]:
            print("  ", seq)

        ch_missed_seqs = sorted({seq for (ch, seq) in miss_count if ch == channel})
        ch_missed_recovered = [seq for seq in ch_missed_seqs if (channel, seq) in ch_upgraded]
        print(f"\nTotal 'audio MISS' events: {ch_miss_total}"
              f" ({len(ch_missed_seqs)} distinct segments)")
        print(f"  Recovered via late-audio upgrade: {len(ch_missed_recovered)}/{len(ch_missed_seqs)}")
        print(f"Disc boundaries seen: {len(ch_disc)}")
        print(f"Epoch resyncs: {len(ch_resync)}")
        for r in ch_resync:
            print("  resync:", r)
        print(f"Stale-epoch fallback matches (audio reused from the prior epoch): {len(ch_stale_epoch)}")
        for s in ch_stale_epoch[:example_limit]:
            print("  ", s)

        print(f"\nServed from cache (cache hit): {len(ch_cache_hits)}")
        print(f"Served via on-demand fallback path: {len(ch_fallback)}")
        print(f"Prefetch-miss events (forced on-demand fallback): {len(ch_prefetch_miss)}")

        print(f"\nFirst {example_limit} never-upgraded silence segments (seq):")
        for seq in ch_never_upgraded[:example_limit]:
            confirmed = " (confirmed sliver: no matching audio in source)" if seq in ch_slivers else ""
            print("  ", seq, confirmed)

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
