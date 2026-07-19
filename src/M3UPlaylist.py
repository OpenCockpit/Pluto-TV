# Copyright (C) 2026 by xcentaurix

"""Shared Extended-M3U provider channel-list writer for FAST-channel TV Cockpit
plugins (Pluto TV, Rakuten TV, Samsung TV Plus, ...).

Written alongside each plugin's own bouquet, in the format iptv-org's
playlists use (https://github.com/iptv-org/iptv), for tools other than
Enigma2 to consume - including StreamCockpit's own channellist*.m3u8
Providers integration, which globs for exactly this file naming convention.
"""

import os


def sanitizeM3UAttr(value):
    """Make *value* safe to embed inside an M3U attribute or field."""
    return str(value).replace('"', "'").replace("\r", "").replace("\n", " ")


def writeM3UPlaylist(path, playlist_name, categories, m3u_entries):
    """Write an Extended-M3U playlist to *path*.

    *categories* is the plugin's ordered list of group/category names.
    *m3u_entries* is {group: [(chid, name, logo, stream_url), ...], ...} -
    the same shape TVDownloadBase.updateprogress() populates in
    self.m3uEntries.

    #PLAYLIST: is the de-facto convention several IPTV players (TiviMate,
    IPTV Smarters, Perfect Player, ...) recognize for the playlist's
    display name.
    """
    lines = ["#EXTM3U", f"#PLAYLIST:{sanitizeM3UAttr(playlist_name)}"]
    for group in categories:
        for chid, name, logo, stream_url in m3u_entries.get(group, []):
            attrs = f'tvg-id="{sanitizeM3UAttr(chid)}" tvg-name="{sanitizeM3UAttr(name)}"'
            if logo:
                attrs += f' tvg-logo="{sanitizeM3UAttr(logo)}"'
            attrs += f' group-title="{sanitizeM3UAttr(group)}"'
            lines.append(f"#EXTINF:-1 {attrs},{sanitizeM3UAttr(name)}")
            lines.append(stream_url)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
