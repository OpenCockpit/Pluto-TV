# Copyright (C) 2026 by xcentaurix
# Copyright (C) 2021 by Team OpenSPA
# License: GNU General Public License v3.0

from Components.Console import Console
from Components.config import config
from Plugins.Plugin import PluginDescriptor
from Screens.MessageBox import MessageBox
from Screens.Standby import QUIT_RESTART, TryQuitMainloop
from skin import findSkinScreen

from .PluginUtils import checkPluginUpdate
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
    if config.plugins.plutotv.auto_update_check.value != "yes":
        session.open(PlutoTVCockpit)
        return

    update = checkPluginUpdate("enigma2-plugin-extensions-plutotvcockpit")
    if update:
        def _installUpdate(answer):
            if answer:
                def _showRestartPrompt(_data, _retval, _extra_args):
                    session.openWithCallback(
                        lambda restart: session.open(PlutoTVCockpit) if not restart else session.open(TryQuitMainloop, QUIT_RESTART),
                        MessageBox,
                        _("The package has been installed. Restart Enigma2 now to load the update?"),
                        type=MessageBox.TYPE_YESNO,
                        default=True
                    )

                install_cmd = f"opkg install {update['path'] or update['package']}"
                Console().ePopen(install_cmd, _showRestartPrompt)
                return

            session.open(PlutoTVCockpit)

        session.openWithCallback(
            _installUpdate,
            MessageBox,
            _("A newer PlutoTVCockpit package (%s) is available. Do you want to install it now?") % update["version"],
            type=MessageBox.TYPE_YESNO,
            default=False
        )
        return

    session.open(PlutoTVCockpit)


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
