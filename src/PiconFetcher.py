# Copyright (C) 2026 by xcentaurix

import concurrent.futures
import os
import shutil
import time
from itertools import count as _count

import requests
from twisted.internet import reactor
from Tools.Directories import fileExists, sanitizeFilename
from .Version import PLUGIN
from .Variables import USER_AGENT


_SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
if os.path.isfile(_SYSTEM_CA_BUNDLE) and not os.environ.get("REQUESTS_CA_BUNDLE"):
    os.environ["REQUESTS_CA_BUNDLE"] = _SYSTEM_CA_BUNDLE

_PICON_WIDTH = 220
_PICON_HEIGHT = 132


class PiconFetcher:
    def __init__(self, picons_config, parent=None):
        """
        picons_config  : config element for the picons setting, e.g. config.plugins.rakutentv.picons
        parent         : optional parent widget that receives updateProgressBar(counter) callbacks
        """
        self.plugin_name = PLUGIN
        self._picons_config = picons_config
        self._user_agent = USER_AGENT
        self.parent = parent
        self.piconDir = self.getPiconPath()
        self.pluginPiconDir = os.path.join(self.piconDir, self.plugin_name)
        self.resolutionStr = f"?h={_PICON_HEIGHT}&w={_PICON_WIDTH}"
        self.piconList = []

    def addPicon(self, ref, name, url, silent):
        """Queue a picon for download.  Call before fetchPicons()."""
        if not self._picons_config.value or not url:
            return
        if self._picons_config.value == "snp" and (ch_name := sanitizeFilename(name.lower())):
            piconname = os.path.join(self.piconDir, ch_name + ".png")
        else:
            piconname = os.path.join(self.piconDir, ref.replace(":", "_") + ".png")
        one_week_ago = time.time() - 60 * 60 * 24 * 7
        if not (fileExists(piconname) and (silent or os.path.getmtime(piconname) > one_week_ago)):
            self.piconList.append((url, piconname))

    def fetchPicons(self):
        """Download all queued picons in a thread pool."""
        _PICON_WORKERS = 10
        _PICON_BATCH_TIMEOUT = 60
        self._counter = _count()
        self.createFolders()
        if self.piconList:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_PICON_WORKERS) as executor:
                futures = {executor.submit(self.downloadURL, url, fn): (url, fn)
                           for url, fn in self.piconList}
                done, _ = concurrent.futures.wait(futures, timeout=_PICON_BATCH_TIMEOUT)
            print(f"[{self.plugin_name} PiconFetcher] fetched {len(done)}/{len(futures)} picons")

    def removeall(self):
        """Remove all symlinks and downloaded picons created by this plugin."""
        if os.path.exists(self.piconDir):
            for f in os.listdir(self.piconDir):
                item = os.path.join(self.piconDir, f)
                if os.path.islink(item) and self.pluginPiconDir in os.readlink(item):
                    os.remove(item)
        if os.path.exists(self.pluginPiconDir):
            shutil.rmtree(self.pluginPiconDir)

    def createFolders(self):
        os.makedirs(self.piconDir, exist_ok=True)
        os.makedirs(self.pluginPiconDir, exist_ok=True)

    _DOWNLOAD_TIMEOUT = 8.0

    def downloadURL(self, url, piconname):
        filepath = os.path.join(self.pluginPiconDir, piconname.removeprefix(self.piconDir).removeprefix(os.sep))
        try:
            dl_url = url if "?" in url else f"{url}{self.resolutionStr}"
            response = requests.get(dl_url, timeout=self._DOWNLOAD_TIMEOUT, headers={"User-Agent": self._user_agent})
            response.raise_for_status()
            content_type = response.headers.get('content-type', '')
            if 'image' in content_type.lower() and len(rc := response.content):
                self._convertToPng(rc, filepath, self.plugin_name)
            else:
                print(f"[{self.plugin_name} PiconFetcher] skipped {dl_url}:"
                      f" content-type={content_type!r} len={len(response.content)}")
        except requests.exceptions.RequestException as e:
            print(f"[{self.plugin_name} PiconFetcher] fetch failed {dl_url}: {e}")
        if fileExists(filepath):
            self.makesoftlink(filepath, piconname)
        if self.parent:
            reactor.callFromThread(self.parent.updateProgressBar, next(self._counter))

    @staticmethod
    def _convertToPng(data, filepath, tag="PiconFetcher"):
        """Decode, downscale to picon size, and save as PNG.

        Some sources (e.g. RakutenTV's channel "artwork"/"snapshot"/"poster"
        fields) are full promotional key-art rather than small icons - several
        MB at native resolution, observed as large as 2-3MB each. Enigma2's
        list rendering decodes the picon file fresh on every row repaint
        (Components/Renderer/Picon.py), so serving these at native resolution
        turns ordinary list scrolling into a multi-megabyte-per-frame
        allocation storm. Always downscale locally rather than trust the
        source to already be small or the CDN to have honored the ?h=&w=
        hint above - thumbnail() only shrinks (never enlarges), so an
        already-small image passes through unchanged.
        """
        try:
            from PIL import Image
            import io
            resample = getattr(Image, "Resampling", Image).LANCZOS
            with Image.open(io.BytesIO(data)) as img:
                img.thumbnail((_PICON_WIDTH, _PICON_HEIGHT), resample)
                img.save(filepath, "PNG")
        except Exception as e:
            print(f"[{tag} PiconFetcher] image conversion failed: {e}")

    def makesoftlink(self, filepath, softlinkpath):
        svgpath = softlinkpath.removesuffix(".png") + ".svg"
        if os.path.isfile(svgpath):
            return
        islink = os.path.islink(softlinkpath)
        if islink:
            target = os.readlink(softlinkpath)
            if target == filepath:
                return
            if self.pluginPiconDir not in target and os.path.exists(target):
                return
            os.remove(softlinkpath)
        elif os.path.isfile(softlinkpath):
            return
        os.symlink(filepath, softlinkpath)

    @staticmethod
    def getPiconPath():
        _SYSTEM = "/usr/share/enigma2/picon"
        try:
            from Components.Renderer.Picon import lastPiconPath, searchPaths
        except ImportError:
            try:
                from Components.Renderer.Picon import piconLocator
                lastPiconPath = piconLocator.activePiconPath
                searchPaths = piconLocator.searchPaths
            except ImportError:
                lastPiconPath = None
                searchPaths = None
        for p in (searchPaths or []):
            if p != _SYSTEM and os.path.isdir(p) and os.access(p, os.W_OK):
                return p
        return lastPiconPath or _SYSTEM
