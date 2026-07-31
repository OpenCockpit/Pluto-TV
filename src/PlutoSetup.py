# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0

import os
import re

from Components.ActionMap import HelpableActionMap
from Components.config import config
from Components.Sources.StaticText import StaticText
from Screens.Setup import Setup

from . import _
from .PlutoTVDownload import PlutoTVDownload, Silent
from .PiconFetcher import PiconFetcher
from .Variables import BOUQUET_FILE, NUMBER_OF_LIVETV_BOUQUETS
from .Version import VERSION


class PlutoSetup(Setup):
    def __init__(self, session):
        Setup.__init__(self, session, setup="PlutoTV")
        if "key_yellow" not in self:
            self["key_yellow"] = StaticText()
            self["key_yellowActions"] = HelpableActionMap(self, ["ColorActions"], {
                "yellow": (self.yellow, _("Remove picons")),
            }, prio=1, description=_("PlutoTV Setup Actions"))
        if "key_blue" not in self:
            self["key_blue"] = StaticText()
            self["key_blueActions"] = HelpableActionMap(self, ["ColorActions"], {
                "blue": (self.blue, _("Remove Live-TV Bouquet")),
            }, prio=1, description=_("PlutoTV Setup Actions"))
        self.updateYellowButton()
        self.updateBlueButton()
        self.setTitle(_("PlutoTV Setup") + f" ({VERSION})")

    def createSetup(self):
        configList = []
        configList.append((_("VoD country"), config.plugins.plutotv.country, _("Select the country that the VoD list will be created for.")))
        configList.append(("---",))
        for n in range(1, NUMBER_OF_LIVETV_BOUQUETS + 1):
            if n == 1 or getattr(config.plugins.plutotv, "live_tv_country" + str(n - 1)).value:
                configList.append((_("Live-TV bouquet %s") % n, getattr(config.plugins.plutotv, "live_tv_country" + str(n)), _("Country for which Live-TV bouquet %s will be created.") % n))
        configList.append(("---",))
        configList.append((_('Live TV mode'), config.plugins.plutotv.live_tv_mode, _('Select the stream provider. Stitcher uses the native Pluto server with JWT auth (resolved at playback). JMP2 uses the jmp2.uk proxy. i.mjh.nz uses Matt Huisman\'s community playlist. Requires bouquet update to take effect.')))
        configList.append((_("Picon type"), config.plugins.plutotv.picons, _("Using service name picons means they will continue to work even if the service reference changes. Also, they can be shared between channels of the same name that don't have the same service references.")))
        configList.append((_("Automatic update check"), config.plugins.plutotv.auto_update_check, _("Automatically check for a newer package update when the plugin GUI is opened.")))
        configList.append((_("Data location"), config.plugins.plutotv.config_folder, _("Location the config data are stored in.")))
        self["config"].list = configList

    def _locationConfigChanged(self):
        if config.plugins.plutotv.country.isChanged():
            return True
        if config.plugins.plutotv.live_tv_mode.isChanged():
            return True
        return any(getattr(config.plugins.plutotv, "live_tv_country" + str(n)).isChanged() for n in range(1, NUMBER_OF_LIVETV_BOUQUETS + 1))

    def keySave(self):
        if self._locationConfigChanged():
            self.session.openWithCallback(lambda *_: Setup.keySave(self), PlutoTVDownload)
        else:
            Setup.keySave(self)

    def updateYellowButton(self):
        if os.path.isdir(PiconFetcher(config.plugins.plutotv.picons).pluginPiconDir):
            self["key_yellow"].text = _("Remove picons")
        else:
            self["key_yellow"].text = ""

    def updateBlueButton(self):
        with open("/etc/enigma2/bouquets.tv", "r", encoding="utf-8") as f:
            bouquets = f.read()
        if "plutotvcockpit" in bouquets:
            self["key_blue"].text = _("Remove Live-TV Bouquet")
        else:
            self["key_blue"].text = ""

    def yellow(self):
        if self["key_yellow"].text:
            PiconFetcher(config.plugins.plutotv.picons).removeall()
            self.updateYellowButton()

    def blue(self):
        if self["key_blue"].text:
            Silent.stop()
            from enigma import eDVBDB
            eDVBDB.getInstance().removeBouquet(re.escape(BOUQUET_FILE) % ".*")
            self.updateBlueButton()
