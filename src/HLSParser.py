# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0

import re
from urllib.parse import urljoin


class HLSPlaylist:
    """Generic HLS playlist parser utilities."""

    @staticmethod
    def attr_value(attr: str, line: str) -> 'str | None':
        """Return the value of *attr* from an HLS tag attribute list.
        Handles both quoted (URI="...") and unquoted (METHOD=AES-128) forms."""
        m = re.search(rf'{attr}="([^"]*)"', line)
        if m:
            return m.group(1)
        m = re.search(rf'{attr}=([^,\s"]+)', line)
        return m.group(1) if m else None

    @staticmethod
    def audio_candidate(line: str, base_url: str) -> 'tuple[str, bool, bool] | None':
        """Parse a single ``#EXT-X-MEDIA:TYPE=AUDIO`` line into a
        ``(uri, is_default, is_accessibility)`` candidate for
        :meth:`select_primary_audio`, or ``None`` if it has no ``URI``."""
        raw_uri = HLSPlaylist.attr_value('URI', line)
        if not raw_uri:
            return None
        is_default = HLSPlaylist.attr_value('DEFAULT', line) == 'YES'
        characteristics = (HLSPlaylist.attr_value('CHARACTERISTICS', line) or '').lower()
        name = (HLSPlaylist.attr_value('NAME', line) or '').lower()
        is_accessibility = (
            'accessibility' in characteristics
            or 'description' in name
            or 'descriptive' in name
            or 'hearing impaired' in name
            or 'visually impaired' in name
        )
        return (urljoin(base_url, raw_uri), is_default, is_accessibility)

    @staticmethod
    def select_primary_audio(candidates: list) -> 'str | None':
        """Pick the main-program audio URI out of a master playlist's
        ``TYPE=AUDIO`` renditions (*candidates*, in playlist order, as
        returned by :meth:`audio_candidate`).

        Some channels carry more than one audio rendition - e.g. a main
        track plus a hearing-impaired or audio-description track - so
        picking "whichever line appears last" (the previous behaviour) can
        silently select the accessibility track instead of the main one.
        Prefers DEFAULT=YES, then the first non-accessibility rendition,
        then just the first rendition listed.
        """
        if not candidates:
            return None
        for uri, is_default, _ in candidates:
            if is_default:
                return uri
        for uri, _, is_accessibility in candidates:
            if not is_accessibility:
                return uri
        return candidates[0][0]

    @staticmethod
    def remove_attr(attr: str, line: str) -> str:
        """Remove  ,ATTR="value"  or  ATTR="value",  from an HLS attribute list."""
        line = re.sub(rf',\s*{attr}="[^"]*"', '', line)
        line = re.sub(rf'{attr}="[^"]*"\s*,\s*', '', line)
        return line

    @staticmethod
    def seq_base(text: str) -> int:
        """Return the EXT-X-MEDIA-SEQUENCE value, or 0 if absent."""
        for line in text.splitlines():
            if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
                try:
                    return int(line.split(':', 1)[1].strip())
                except ValueError:
                    pass
        return 0

    @staticmethod
    def segments(text: str, base_url: str) -> tuple:
        """Return ``(seq_base, [absolute_segment_url, ...])`` from a media playlist."""
        base = HLSPlaylist.seq_base(text)
        urls = []
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                urls.append(urljoin(base_url, line))
        return base, urls

    @staticmethod
    def extinf_duration(text: str) -> 'float | None':
        """Return the median #EXTINF segment duration (seconds) from a media playlist.

        Uses the median so a short final segment (or a single abnormal segment)
        does not skew the result.  Returns None if no EXTINF tags are present.
        """
        durations = []
        for line in text.splitlines():
            s = line.strip()
            if s.startswith('#EXTINF:'):
                try:
                    durations.append(float(s[8:].split(',')[0]))
                except ValueError:
                    pass
        if not durations:
            return None
        durations.sort()
        return durations[len(durations) // 2]

    @staticmethod
    def ad_markers(text: str) -> list:
        """Return any SCTE-35-derived ad-boundary tag lines found in *text*
        (``#EXT-X-CUE-OUT``, ``#EXT-X-CUE-OUT-CONT``, ``#EXT-X-CUE-IN``,
        ``#EXT-X-DATERANGE``), verbatim and in playlist order.

        Diagnostic only, for now - LiveProxy's own pairing logic reacts to
        plain ``#EXT-X-DISCONTINUITY`` alone and doesn't parse or act on any
        of these. This exists to find out whether Pluto's demuxed playlists
        actually carry richer ad-boundary metadata at all before spending
        effort building real handling around it - see callers for where the
        results get logged.
        """
        return [
            stripped for line in text.splitlines()
            if (stripped := line.strip()).startswith(
                ('#EXT-X-CUE-OUT', '#EXT-X-CUE-IN', '#EXT-X-DATERANGE'))
        ]

    @staticmethod
    def segment_keys(text: str, base_url: str) -> tuple:
        """Parse per-segment AES-128 key/IV and discontinuity markers.

        Returns ``(key_urls, ivs, discs)`` – three parallel lists, one entry
        per media segment in *text*.

        * ``key_urls[i]``  – absolute URI of the AES-128 key, or ``None``
        * ``ivs[i]``       – explicit IV as ``bytes``, or ``None`` (use seq number)
        * ``discs[i]``     – ``True`` when a ``#EXT-X-DISCONTINUITY`` tag
                             immediately precedes the segment
        """
        cur_key_url = None
        cur_iv = None
        cur_disc = False
        key_urls = []
        ivs = []
        discs = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith('#EXT-X-KEY:'):
                method = HLSPlaylist.attr_value('METHOD', stripped)
                if method == 'AES-128':
                    raw_uri = HLSPlaylist.attr_value('URI', stripped)
                    cur_key_url = urljoin(base_url, raw_uri) if raw_uri else None
                    iv_str = HLSPlaylist.attr_value('IV', stripped)
                    cur_iv = (bytes.fromhex(iv_str[2:].zfill(32))
                              if iv_str and iv_str.startswith('0x') else None)
                elif method == 'NONE':
                    cur_key_url = None
                    cur_iv = None
            elif stripped == '#EXT-X-DISCONTINUITY':
                cur_disc = True
            elif stripped and not stripped.startswith('#'):
                key_urls.append(cur_key_url)
                ivs.append(cur_iv)
                discs.append(cur_disc)
                cur_disc = False
        return key_urls, ivs, discs
