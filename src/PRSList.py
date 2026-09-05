# Copyright (C) 2026 by xcentaurix

"""Shared VoD browse-list MenuList for the FAST-channel TV Cockpit plugins
(Pluto TV, Rakuten TV, Samsung TV Plus, ...).

Instantiate directly - no per-plugin subclass needed:

    self["feedlist"] = PRSList([])                                            # Rakuten/Samsung: menu icon only
    self["feedlist"] = PRSList([], icons=("menu.png", "series.png", "cine.png",
                                           "cine_half.png", "cine_end.png"),
                                resume_points=resumePointsInstance)            # Pluto: full VoD icon set

*icons* names the plugin's own bundled skin/images/<file> files (also the
optional skin-override name, looked up as icons/<PLUGIN>/<file> in the
active skin first) - each entry a full filename, e.g. "cine_half.png", not
just "cine_half", so a reference-scanning tool like gitupdatepypics.py can
find it as a literal string. The plugin's own install directory is derived
from PLUGIN in its Version.py, so a plugin need not pass its own path in.

*resume_points* (a ResumePoints instance, see PlutoUtils/RakutenTVUtils/
SamsungTVUtils), if given, enables picking between "cine"/"cine_half"/
"cine_end" for movie/episode entries based on playback progress.
"""

import os

from Components.MenuList import MenuList
from Components.MultiContent import MultiContentEntryText, MultiContentEntryPixmapAlphaBlend
from Tools.Directories import fileExists, resolveFilename, SCOPE_CURRENT_SKIN
from Tools.LoadPixmap import LoadPixmap
from enigma import BT_KEEP_ASPECT_RATIO, BT_SCALE, eListboxPythonMultiContent, gFont
from skin import applySkinFactor, fonts
from .Version import PLUGIN


class PRSList(MenuList):
    def __init__(self, entries, icons=("menu.png",), resume_points=None):
        icon_dir = f"/usr/lib/enigma2/python/Plugins/Extensions/{PLUGIN}/skin/images"
        self._resume_points = resume_points
        self._pixmaps = {}

        for icon_file in icons:
            icon_name = os.path.splitext(icon_file)[0]
            fallback = os.path.join(icon_dir, icon_file)
            resolved = x if fileExists(x := resolveFilename(SCOPE_CURRENT_SKIN, f"icons/{PLUGIN}/{icon_file}")) else fallback
            self._pixmaps[icon_name] = LoadPixmap(resolved) if fileExists(resolved) else None

        MenuList.__init__(self, entries, content=eListboxPythonMultiContent)
        font = fonts.get(PLUGIN, applySkinFactor("Regular", 19, 35))
        self.l.setFont(0, gFont(font[0], font[1]))
        self.l.setItemHeight(font[2])

    def listentry(self, name, data, _id, epid=0):
        res = [(name, data, _id, epid)]

        png = None
        if data == "menu":
            png = self._pixmaps.get("menu")
        elif data in {"series", "seasons"}:
            png = self._pixmaps.get("series")
        elif data in {"movie", "episode"}:
            png = self._pixmaps.get("cine")
            if self._resume_points is not None:
                sid = epid if data == "episode" else _id
                last, length = self._resume_points.getResumePoint(sid)
                if last:
                    cine_half_png = self._pixmaps.get("cine_half")
                    cine_end_png = self._pixmaps.get("cine_end")
                    if cine_half_png and (last > 900000) and (not length or (last < length - 900000)):
                        png = cine_half_png
                    elif cine_end_png and last >= length - 900000:
                        png = cine_end_png
        else:
            png = self._pixmaps.get("menu")

        res.append(MultiContentEntryText(pos=applySkinFactor(45, 7), size=applySkinFactor(533, 35), font=0, text=name))
        if png:
            res.append(MultiContentEntryPixmapAlphaBlend(pos=applySkinFactor(7, 9), size=applySkinFactor(20, 20), png=png, flags=BT_SCALE | BT_KEEP_ASPECT_RATIO))
        return res
