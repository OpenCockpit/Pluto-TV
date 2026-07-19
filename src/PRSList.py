# Copyright (C) 2026 by xcentaurix

"""Shared VoD browse-list MenuList for the FAST-channel TV Cockpit plugins
(Pluto TV, Rakuten TV, Samsung TV Plus, ...).

Instantiate directly - no per-plugin subclass needed:

    self["feedlist"] = PRSList([])                                            # Rakuten/Samsung: menu icon only
    self["feedlist"] = PRSList([], icons=("menu", "series", "cine",
                                           "cine_half", "cine_end"),
                                resume_points=resumePointsInstance)            # Pluto: full VoD icon set

*icons* names the plugin's own bundled skin/images/<name>.png files (also
the optional skin-override name, looked up as icons/<name>.png in the
active skin first). The plugin's own install directory is derived from
PLUGIN in its Version.py, so a plugin need not pass its own path in.

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
    def __init__(self, entries, icons=("menu",), resume_points=None):
        icon_dir = f"/usr/lib/enigma2/python/Plugins/Extensions/{PLUGIN}/skin/images"
        self._resume_points = resume_points

        for icon_name in icons:
            fallback = os.path.join(icon_dir, f"{icon_name}.png")
            resolved = x if fileExists(x := resolveFilename(SCOPE_CURRENT_SKIN, f"icons/{icon_name}.png")) else fallback
            setattr(self, f"{icon_name}_png", LoadPixmap(resolved) if fileExists(resolved) else None)

        MenuList.__init__(self, entries, content=eListboxPythonMultiContent)
        font = fonts.get(PLUGIN, applySkinFactor("Regular", 19, 35))
        self.l.setFont(0, gFont(font[0], font[1]))
        self.l.setItemHeight(font[2])

    def listentry(self, name, data, _id, epid=0):
        res = [(name, data, _id, epid)]

        png = None
        if data == "menu":
            png = getattr(self, "menu_png", None)
        elif data in {"series", "seasons"}:
            png = getattr(self, "series_png", None)
        elif data in {"movie", "episode"}:
            png = getattr(self, "cine_png", None)
            if self._resume_points is not None:
                sid = epid if data == "episode" else _id
                last, length = self._resume_points.getResumePoint(sid)
                if last:
                    cine_half_png = getattr(self, "cine_half_png", None)
                    cine_end_png = getattr(self, "cine_end_png", None)
                    if cine_half_png and (last > 900000) and (not length or (last < length - 900000)):
                        png = cine_half_png
                    elif cine_end_png and last >= length - 900000:
                        png = cine_end_png

        res.append(MultiContentEntryText(pos=applySkinFactor(45, 7), size=applySkinFactor(533, 35), font=0, text=name))
        if png:
            res.append(MultiContentEntryPixmapAlphaBlend(pos=applySkinFactor(7, 9), size=applySkinFactor(20, 20), png=png, flags=BT_SCALE | BT_KEEP_ASPECT_RATIO))
        return res
