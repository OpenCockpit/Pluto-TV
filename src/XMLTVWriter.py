# Copyright (C) 2026 by xcentaurix

"""Shared XMLTV guide writer for FAST-channel TV Cockpit plugins (Pluto TV,
Rakuten TV, Samsung TV Plus, ...).

Written alongside each plugin's own bouquet/M3U export, from the same guide
data a plugin already builds for its own eEPGCache.importEvents() call - this
persists that data as a standard XMLTV document, for tools other than
Enigma2's own EPG cache to consume.
"""

import os
import time
from xml.sax.saxutils import escape


def xmltvTime(epoch_seconds):
    """Format a Unix timestamp as an XMLTV date (UTC, per the XMLTV spec)."""
    return time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(epoch_seconds))


def writeXMLTVFile(path, channels, programmes):
    """Write an XMLTV guide document to *path*.

    *channels* is [(channel_id, display_name), ...].
    *programmes* is [(channel_id, start_epoch, stop_epoch, title, desc), ...] -
    start/stop are Unix timestamps, desc may be empty/None.
    """
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<tv>"]
    for channel_id, display_name in channels:
        lines.append(f'  <channel id="{escape(str(channel_id))}">')
        lines.append(f"    <display-name>{escape(str(display_name))}</display-name>")
        lines.append("  </channel>")
    for channel_id, start, stop, title, desc in programmes:
        lines.append(f'  <programme start="{xmltvTime(start)}" stop="{xmltvTime(stop)}"'
                     f' channel="{escape(str(channel_id))}">')
        lines.append(f"    <title>{escape(str(title))}</title>")
        if desc:
            lines.append(f"    <desc>{escape(str(desc))}</desc>")
        lines.append("  </programme>")
    lines.append("</tv>")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
