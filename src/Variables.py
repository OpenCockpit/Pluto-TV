# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0

from os import path
from Tools.Directories import resolveFilename, SCOPE_CONFIG
from .Version import PLUGIN


TIMER_FILE = path.join(path.realpath(resolveFilename(SCOPE_CONFIG)), PLUGIN, PLUGIN + ".timer")
NODATA_FILE = path.join(path.realpath(resolveFilename(SCOPE_CONFIG)), PLUGIN, PLUGIN + ".nodata")
RESUMEPOINTS_FILE = path.join(path.realpath(resolveFilename(SCOPE_CONFIG)), PLUGIN, "resumepoints.pkl")
PLUGIN_FOLDER = path.dirname(path.realpath(__file__))
PLUGIN_ICON = "plugin.png"
BOUQUET_FILE = "userbouquet.plutotvcockpit_%s.tv"
BOUQUET_NAME = "Pluto TV Cockpit (%s)"
CHANNELLIST_FILE = "channellist.plutotvcockpit_%s.m3u8"
XMLTV_FILE = "xmltv.plutotvcockpit_%s.xml"
NUMBER_OF_LIVETV_BOUQUETS = 5
STREAM_POOL_SIZE = 4
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
