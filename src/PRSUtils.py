# Copyright (C) 2026 by xcentaurix

"""Shared per-plugin utilities for the FAST-channel TV Cockpit plugins
(Pluto TV, Rakuten TV, Samsung TV Plus, ...): data-folder resolution,
resume-point tracking, and poster downloading.

Instantiate once per plugin (module level):

    from Components.config import config
    from .PRSUtils import PRSUtils

    _utils = PRSUtils(config.plugins.rakutentv)
    downloadPoster = _utils.downloadPoster
    resumePointsInstance = _utils.resumePoints

*config_subsection* is the plugin's own config.plugins.<x> ConfigSubsection
(needed for MountedDataFolder's data-location setting). RESUMEPOINTS_FILE,
USER_AGENT and PLUGIN are read from the plugin's own Variables.py/Version.py,
so nothing else needs passing in.
"""

import requests

from .Variables import RESUMEPOINTS_FILE, USER_AGENT
from .Version import PLUGIN
from .CockpitTVUtils import MountedDataFolder, ResumePoints, downloadPoster as _sharedDownloadPoster


class PRSUtils:
    def __init__(self, config_subsection):
        self._dataFolder = MountedDataFolder(config_subsection, PLUGIN)
        self.resumePoints = ResumePoints(RESUMEPOINTS_FILE)

        self._poster_session = requests.Session()
        self._poster_session.headers.update({"User-Agent": USER_AGENT})

    def getDataFolder(self):
        return self._dataFolder.get()

    def downloadPoster(self, url, name, callback):
        _sharedDownloadPoster(self._poster_session, self.getDataFolder(), url, name, callback)
