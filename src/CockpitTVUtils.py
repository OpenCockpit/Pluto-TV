# Copyright (C) 2026 by xcentaurix

import os
from pickle import load as pickle_load, dump as pickle_dump
from time import time

from Components.config import ConfigSelection
from Components.Harddisk import harddiskmanager
from Tools.Directories import fileExists
from twisted.internet.reactor import callFromThread
import requests


class MountedDataFolder:
    """Tracks a plugin's data folder on whichever removable/mounted partition the
    user picked, offering a `config.plugins.<plugin>.datalocation` ConfigSelection
    and recreating `<mountpoint>/<folder_name>` whenever the choice or the set of
    mounted partitions changes.
    """

    def __init__(self, config_subsection, folder_name):
        self._config_subsection = config_subsection
        self._folder_name = folder_name
        self._data_folder = ""
        choices = self._getMountChoices()
        if not choices:
            choices = [("/tmp", "/tmp")]
        config_subsection.datalocation = ConfigSelection(choices=choices, default=self._getMountDefault(choices))
        harddiskmanager.on_partition_list_change.append(self._onPartitionChange)
        config_subsection.datalocation.addNotifier(self._updateDataFolder, immediate_feedback=False)

    def get(self):
        return self._data_folder

    @staticmethod
    def _getMountChoices():
        choices = []
        for p in harddiskmanager.getMountedPartitions():
            if os.path.exists(p.mountpoint):
                d = os.path.normpath(p.mountpoint)
                if p.mountpoint != "/":
                    choices.append((p.mountpoint, d))
        choices.sort()
        return choices

    @staticmethod
    def _getMountDefault(choices):
        choices = {x[1]: x[0] for x in choices}
        return choices.get("/media/hdd") or choices.get("/media/usb") or ""

    def _onPartitionChange(self, *_args, **_kwargs):
        choices = self._getMountChoices()
        self._config_subsection.datalocation.setChoices(choices=choices, default=self._getMountDefault(choices))
        self._updateDataFolder()

    def _updateDataFolder(self, *_args, **_kwargs):
        self._data_folder = ""
        if v := self._config_subsection.datalocation.value:
            if os.path.exists(v):
                self._data_folder = os.path.join(v, self._folder_name)
                os.makedirs(self._data_folder, exist_ok=True)


class ResumePoints:
    def __init__(self, resume_point_file):
        self.resumePointFile = resume_point_file
        self.resumePointCache = {}
        self.loadResumePoints()
        self.cleanCache()

    def loadResumePoints(self):
        self.resumePointCache.clear()
        if fileExists(self.resumePointFile):
            with open(self.resumePointFile, "rb") as f:
                self.resumePointCache.update(pickle_load(f, encoding="utf8"))

    def saveResumePoints(self):
        os.makedirs(os.path.dirname(self.resumePointFile), exist_ok=True)
        with open(self.resumePointFile, "wb") as f:
            pickle_dump(self.resumePointCache, f, protocol=5)

    def setResumePoint(self, session, sid):
        service = session.nav.getCurrentService()
        if service and session.nav.getCurrentlyPlayingServiceOrGroup():
            if seek := service.seek():
                pos = seek.getPlayPosition()
                if not pos[0]:
                    lru = int(time())
                    duration = sl[1] if (sl := seek.getLength()) else None
                    position = pos[1]
                    self.resumePointCache[sid] = [lru, position, duration]
                    self.saveResumePoints()

    def getResumePoint(self, sid):
        last = None
        length = 0
        if sid and (entry := self.resumePointCache.get(sid)):
            entry[0] = int(time())
            last = entry[1]
            length = entry[2]
        return last, length

    def cleanCache(self):
        changed = False
        now = int(time())
        for sid, v in list(self.resumePointCache.items()):
            if now > v[0] + 30 * 24 * 60 * 60:
                del self.resumePointCache[sid]
                changed = True
        if changed:
            self.saveResumePoints()


def downloadPoster(poster_session, data_folder, url, name, callback):
    """Download and cache a poster image, verifying cached files' magic bytes
    still match their extension (a CDN's content-type header can be wrong).
    """
    if not name or not data_folder:
        return
    base = os.path.join(data_folder, name)
    for ext in ('.png', '.jpg'):
        filename = base + ext
        if fileExists(filename):
            try:
                with open(filename, "rb") as f:
                    header = f.read(2)
                is_png = header == b'\x89P'
                if (ext == '.png') == is_png:
                    callFromThread(callback, filename, name)
                    return
                os.remove(filename)
            except OSError:
                pass
    try:
        response = poster_session.get(url, timeout=5)
        response.raise_for_status()
        rc = response.content
        if len(rc) > 2:
            ext = '.png' if rc[:2] == b'\x89P' else '.jpg'
            filename = base + ext
            with open(filename, "wb") as f:
                f.write(rc)
            callFromThread(callback, filename, name)
            return
    except requests.exceptions.RequestException:
        pass
    callFromThread(callback, "", name)


def pickBestImage(imgs):
    """Pick poster and best available image from a covers list."""
    poster = ""
    image = ""
    if len(imgs) > 2:
        image = imgs[2].get("url", "")
    if len(imgs) > 1 and not image:
        image = imgs[1].get("url", "")
    if len(imgs) > 0:
        poster = imgs[0].get("url", "")
    return poster, image
