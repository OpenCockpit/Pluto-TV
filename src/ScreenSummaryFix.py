# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


from enigma import eWindow
from Screens.Screen import Screen
from skin import applyAllAttributes


def _applySkin(self):
    bounds = (self.desktop.size().width(), self.desktop.size().height())
    resolution = bounds
    zPosition = 0
    for (key, value) in self.skinAttributes:
        if key in {"resolution", "baseResolution"}:
            resolution = tuple(int(x.strip()) for x in value.split(","))
        elif key == "zPosition":
            zPosition = int(value)
    if not self.instance:
        self.instance = eWindow(self.desktop, zPosition)
    if "title" not in self.skinAttributes and self.screenTitle:
        self.skinAttributes.append(("title", self.screenTitle))
    else:
        for attribute in self.skinAttributes:
            if attribute[0] == "title":
                self.setTitle(_(attribute[1]))  # noqa: F821, pylint: disable=undefined-variable
    self.scale = ((bounds[0], resolution[0]), (bounds[1], resolution[1]))
    self.skinAttributes.sort(key=lambda a: {"position": 1}.get(a[0], 0))
    applyAllAttributes(self.instance, self.desktop, self.skinAttributes, self.scale)
    self.createGUIScreen(self.instance, self.desktop)


def patchScreenApplySkin():
    if Screen.applySkin is not _applySkin:
        Screen.applySkin = _applySkin
