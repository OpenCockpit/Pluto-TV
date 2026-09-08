# Copyright (C) 2026 by xcentaurix

"""Shared VoD browse-list Source for the FAST-channel TV Cockpit plugins
(Pluto TV, Rakuten TV, Samsung TV Plus, ...).

Instantiate directly - no per-plugin subclass needed:

    self["feedlist"] = PRSList([])                                             # Rakuten/Samsung: menu icon only
    self["feedlist"] = PRSList([], icons=("menu.png", "series.png", "cine.png",
                                          "cine_half.png", "cine_end.png"),
                                          resume_points=resumePointsInstance)  # Pluto: full VoD icon set

*icons* names the plugin's own bundled skin/images/<file> files

*resume_points* (a ResumePoints instance, see PlutoUtils/RakutenTVUtils/
SamsungTVUtils), if given, enables picking between "cine"/"cine_half"/
"cine_end" for movie/episode entries based on playback progress.

The actual per-row layout (icon + text position/size) is skin-defined, in
Common/src/skin/screenpart_PRSList.ymlinc - included as the "feedlist"
widget's own COCTemplatedMultiContentEx convert (see
screenpart_PRSPluginBody.ymlinc). listentry() below only supplies the raw
values that template's "value: N" field indices read: index 0 is the
identity tuple (name, data, _id, epid) callers already read back via
getCurrent()[0] - unchanged so PlutoTVCockpit.py/RakutenTVCockpit.py/
SamsungTVCockpit.py's existing getSelection()-style code needed no changes -
index 1 is the icon pixmap, index 2 the display text.
"""

import os

from Components.Sources.List import List
from Tools.Directories import fileExists, resolveFilename, SCOPE_CURRENT_SKIN
from Tools.LoadPixmap import LoadPixmap
from .Version import PLUGIN


class PRSList(List):
    def __init__(self, entries, icons=("menu.png",), resume_points=None):
        icon_dir = f"/usr/lib/enigma2/python/Plugins/Extensions/{PLUGIN}/skin/images"
        self._resume_points = resume_points
        self._pixmaps = {}

        for icon_file in icons:
            icon_name = os.path.splitext(icon_file)[0]
            fallback = os.path.join(icon_dir, icon_file)
            resolved = x if fileExists(x := resolveFilename(SCOPE_CURRENT_SKIN, f"icons/{PLUGIN}/{icon_file}")) else fallback
            self._pixmaps[icon_name] = LoadPixmap(resolved) if fileExists(resolved) else None

        List.__init__(self)
        self.setList(entries)

    def getSelectionIndex(self):
        return self.index

    def moveToIndex(self, index):
        self.index = index

    def listentry(self, name, data, _id, epid=0):
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

        return ((name, data, _id, epid), png, name)
