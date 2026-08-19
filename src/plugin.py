# Copyright (C) 2026 by xcentaurix
# Copyright (C) 2021 by Team OpenSPA
# License: GNU General Public License v3.0

from Components.config import config
from Plugins.Plugin import PluginDescriptor
from skin import findSkinScreen

from .PluginUpgrade import checkPluginUpdateAndOpen
from . import ConfigInit  # noqa: F401, pylint: disable=unused-import
from . import _
from .PlutoTVRequest import playServiceExtension, recordServiceExtension, startProactiveRefresh
from .PlutoTVDownload import PlutoTVDownload, Silent
from .PlutoTVCockpit import PlutoTVCockpit
from .Variables import PLUGIN_ICON
from .SkinUtils import loadPluginSkin
from .Version import VERSION
from .Debug import logger


if findSkinScreen("PlutoTVCockpit") is None:
    loadPluginSkin()


def sessionstart(reason, session, **kwargs):  # pylint: disable=unused-argument
    logger.info("+++ Version: %s starts...", VERSION)
    if hasattr(session.nav, "playServiceExtensions") and playServiceExtension not in session.nav.playServiceExtensions:
        session.nav.playServiceExtensions.append(playServiceExtension)
    if hasattr(session.nav, "recordServiceExtensions") and recordServiceExtension not in session.nav.recordServiceExtensions:
        session.nav.recordServiceExtensions.append(recordServiceExtension)
    Silent.init(session)
    from twisted.internet import reactor
    reactor.callLater(30, startProactiveRefresh)


def Download_PlutoTV(session, **_kwargs):
    session.open(PlutoTVDownload)


def system(session, **_kwargs):
    checkPluginUpdateAndOpen(
        session, "enigma2-plugin-extensions-plutotvcockpit", "PlutoTVCockpit",
        PlutoTVCockpit, config.plugins.plutotv.auto_update_check)


def Plugins(**_kwargs):
    return [
        PluginDescriptor(
            name=_("PlutoTVCockpit"),
            where=PluginDescriptor.WHERE_PLUGINMENU,
            icon=PLUGIN_ICON,
            description=_("View video on demand and download a bouquet of live tv channels"),
            fnc=system,
            needsRestart=True
        ),
        PluginDescriptor(
            name=_("Download PlutoTV bouquets, picons and EPG"),
            where=PluginDescriptor.WHERE_EXTENSIONSMENU,
            fnc=Download_PlutoTV,
            needsRestart=True
        ),
        PluginDescriptor(
            name=_("Silently download PlutoTV"),
            where=PluginDescriptor.WHERE_SESSIONSTART,
            fnc=sessionstart
        ),
    ]
