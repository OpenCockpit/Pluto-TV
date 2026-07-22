# Copyright (C) 2026 by xcentaurix

import os
import re
import time
from io import BytesIO

from Screens.MessageBox import MessageBox
from Tools.Directories import fileExists
from enigma import eDVBDB, eEPGCache
from twisted.internet import threads, reactor

from .PiconFetcher import PiconFetcher
from .M3UPlaylist import writeM3UPlaylist
from .Debug import logger


class TVDownloadBase:
    """Shared bouquet/EPG download workflow for FAST-channel TV Cockpit plugins
    (Pluto TV, Rakuten TV, Samsung TV Plus, ...).

    A concrete plugin subclasses this (redeclaring ``downloadActive`` on its own
    class so the flag is shared between that plugin's interactive and silent
    download instances, but independent from other plugins) and provides:

    Required class attributes:
        downloadActive (bool)   -- must be redeclared, not inherited, per plugin
        LOG_PREFIX (str)        -- tag used in internal (untranslated) log prints
        TIMER_FILE (str)        -- from the plugin's own Variables.py
        BOUQUET_FILE (str)      -- from the plugin's own Variables.py
        TSIDS (dict)            -- from the plugin's own Config.py
        SILENT_IN_PROGRESS_TEXT, PICONS_LABEL, FETCHING_PICONS_TEXT,
        UPDATE_COMPLETED_TEXT, PROCESSING_TEXT, WAITING_FOR_CHANNEL_TEXT
                                 -- translated (via the plugin's own `_`) strings
        FINALIZE_DELAY (float)  -- optional pause after writing a bouquet, before
                                    the next country/region starts; defaults to 0
        EPGIMPORT_MISSING_TEXT (str) -- shown once at completion (interactive) or
                                    logged (silent) if a plugin's _importGuide sets
                                    self.epgimport_missing = True; defaults to a
                                    plain, untranslated message - override with a
                                    translated (via the plugin's own `_`) string

    Required hook methods:
        _selectedLocations(self) -> list[str]
        _defaultLocation(self) -> str
        _picons_config(self) -> ConfigElement
        _configFolder(self) -> str -- the plugin's configured export folder
                                    (config.plugins.<plugin>.config_folder.value),
                                    used for the default _afterBouquetWritten's
                                    M3U export and by plugins' own _importGuide
                                    for the XMLTV export
        _fetchChannels(self, cc) -> list[dict]
        buildM3U(self, channel)
        _bouquetName(self, cc) -> str
        _buildBouquetEntry(self, key, chitem) -> (ref, stream_url, ch_name, ch_logourl)

    Optional hooks:
        _importGuide(self, cc)   -- called once per country/region before the
                                     per-channel loop; no-op by default
        _clearPluginState(self)  -- called whenever channelsList/categories are
                                     reset; no-op by default
        _afterBouquetWritten(self, cc, bouquet_name) -- called once per
                                     country/region right after its bouquet
                                     file is written. Default: if
                                     CHANNELLIST_FILE is set, writes an
                                     Extended-M3U export to
                                     <_configFolder()>/<CHANNELLIST_FILE % cc>
                                     from self.m3uEntries, reusing each
                                     channel's already-resolved bouquet
                                     stream_url (no extra network calls).
                                     Override this entirely (as PlutoTVCockpit
                                     does) when the bouquet's own stream_url
                                     isn't suitable for external consumption
                                     (e.g. a lazy-resolved custom scheme) and
                                     a different, independently-built URL is
                                     needed for the export instead.

    Optional class attribute:
        CHANNELLIST_FILE (str)  -- printf-style filename template (e.g.
                                    "channellist.rakutentvcockpit_%s.m3u8"),
                                    formatted with cc, for the default
                                    _afterBouquetWritten's M3U export. None
                                    (the default) disables the export.
    """

    downloadActive = False
    FINALIZE_DELAY = 0
    EPGIMPORT_MISSING_TEXT = "EPGImport plugin not installed - no EPG data was imported."
    CHANNELLIST_FILE = None

    def __init__(self, silent=False):
        self.channelsList = {}
        self.categories = []
        self.m3uEntries = {}
        self.state = 1
        self.silent = silent
        self.epgcache = eEPGCache.getInstance()
        self.epgimport_missing = False
        self._setDownloadActive(False)

    def _downloadActiveOwner(self):
        for klass in type(self).__mro__:
            if "downloadActive" in klass.__dict__:
                return klass
        return TVDownloadBase

    def _isDownloadActive(self):
        return self._downloadActiveOwner().downloadActive

    def _setDownloadActive(self, value):
        self._downloadActiveOwner().downloadActive = value

    def _importGuide(self, cc):
        pass

    def _clearPluginState(self):
        pass

    def _afterBouquetWritten(self, cc, bouquet_name):
        if self.CHANNELLIST_FILE:
            path = os.path.join(self._configFolder(), self.CHANNELLIST_FILE % cc)
            writeM3UPlaylist(path, bouquet_name, self.categories, self.m3uEntries)

    def _existingBouquets(self):
        """Yield (cc, filename) for every userbouquet file on disk matching
        this plugin's BOUQUET_FILE template, by reversing the %s placeholder
        back into a capturing group."""
        cc_re = re.compile(re.escape(self.BOUQUET_FILE) % "(.+)")
        try:
            filenames = os.listdir("/etc/enigma2/")
        except OSError:
            return
        for filename in filenames:
            m = cc_re.fullmatch(filename)
            if m:
                yield m.group(1), filename

    def _clearBouquet(self, cc, filename):
        """Empty an existing bouquet's content in place instead of deleting
        it via eDVBDB.removeBouquet(), so its file and its position in
        bouquets.tv survive - only the per-channel content is cleared here,
        to be completely rewritten by addOrUpdateBouquet() later for
        locations that get re-downloaded in this same run."""
        eDVBDB.getInstance().addOrUpdateBouquet(self._bouquetName(cc), filename, [], False)

    def cc(self):
        locations = [x for x in self._selectedLocations() if x] or [self._defaultLocation()]
        for cc, filename in self._existingBouquets():
            if cc not in locations:
                self._clearBouquet(cc, filename)
        yield from locations

    def download(self):
        if self._isDownloadActive():
            if not self.silent:
                self.session.openWithCallback(self.close, MessageBox, self.SILENT_IN_PROGRESS_TEXT, MessageBox.TYPE_INFO, timeout=30)
            logger.info("A silent download is in progress.")
            return
        self.ccGenerator = self.cc()
        self.piconFetcher = PiconFetcher(self._picons_config(), self)
        threads.deferToThread(self._downloadThread)

    def _downloadThread(self):
        """Run the entire download workflow in a background thread."""
        try:
            self._managerThread()
        except Exception as e:
            logger.error("Error in download thread: %s", e)
            self._setDownloadActive(False)

    def _managerThread(self):
        self._setDownloadActive(True)
        if cc := next(self.ccGenerator, None):
            self._downloadBouquetThread(cc)
        else:
            self.channelsList.clear()
            self.categories.clear()
            self._clearPluginState()
            self._setDownloadActive(False)
            self.ccGenerator = None
            if self.piconFetcher.piconList:
                self.total = len(self.piconFetcher.piconList)
                reactor.callFromThread(self.updateProgressBar, 0)
                reactor.callFromThread(self.updateAction, self.PICONS_LABEL)
                reactor.callFromThread(self.updateStatus, self.FETCHING_PICONS_TEXT)
                self.piconFetcher.fetchPicons()
                reactor.callFromThread(self.updateProgressBar, self.total)
            self.piconFetcher = None
            if self.epgimport_missing:
                reactor.callFromThread(self.updateStatus, self.EPGIMPORT_MISSING_TEXT)
                time.sleep(5)
            reactor.callFromThread(self.updateStatus, self.UPDATE_COMPLETED_TEXT)
            time.sleep(3)
            reactor.callFromThread(self.exitOk)
            reactor.callFromThread(self.start)

    def manager(self):
        """Legacy entry point - redirects to threaded version."""
        threads.deferToThread(self._managerThread)

    def _downloadBouquetThread(self, cc):
        self.bouquet = []
        self.resolved_count = 0
        self.bouquetCC = cc
        self.tsid = self.TSIDS.get(cc, "0")
        self.usedServiceIds = set()
        reactor.callFromThread(self.stop)
        self.channelsList.clear()
        self.categories.clear()
        self.m3uEntries.clear()
        self._clearPluginState()
        reactor.callFromThread(self.updateAction, cc)
        reactor.callFromThread(self.updateProgressBar, 0)
        reactor.callFromThread(self.updateStatus, self.PROCESSING_TEXT)

        try:
            self._buildBouquet(cc)
        except Exception as e:
            logger.error("%s: failed to update, skipping to the next location: %s", cc, e)
            self._managerThread()

    def _buildBouquet(self, cc):
        channels = sorted(self._fetchChannels(cc), key=lambda x: x["number"])
        for channel in channels:
            self.buildM3U(channel)

        self.categories.sort(key=str.casefold)
        for _group, channels_in_group in self.channelsList.items():
            channels_in_group.sort(key=lambda ch: ch[2].casefold())

        self.total = sum(len(v) for v in self.channelsList.values())

        if len(self.categories) == 0:
            reactor.callFromThread(self.noCategories)
            self._managerThread()
        else:
            if self.categories[0] in self.channelsList:
                self.subtotal = len(self.channelsList[self.categories[0]])
            else:
                self.subtotal = 0
            self.key = 0
            self.chitem = 0

            self._importGuide(cc)

            for i in range(self.total + 1):
                self.updateprogress(param=i)

    def updateprogress(self, param):
        if hasattr(self, "state") and self.state == 1:
            reactor.callFromThread(self.updateProgressBar, param)
            if param < self.total:
                key = self.categories[self.key]
                if self.chitem == self.subtotal:
                    self.chitem = 0
                    found = False
                    while not found:
                        self.key += 1
                        key = self.categories[self.key]
                        found = key in self.channelsList
                    self.subtotal = len(self.channelsList[key])

                if self.chitem == 0:
                    self.bouquet.append(f"1:64:{self.key}:0:0:0:0:0:0:0::{self.categories[self.key]}")

                chid = self.channelsList[key][self.chitem][1]
                ref, stream_url, ch_name, ch_logourl = self._buildBouquetEntry(key, self.chitem)
                if stream_url:
                    self.resolved_count += 1
                    self.bouquet.append(f"{ref}:{stream_url}:{ch_name}")
                    self.m3uEntries.setdefault(key, []).append((chid, ch_name, ch_logourl, stream_url.replace("%3a", ":")))
                    self.piconFetcher.addPicon(ref, ch_name, ch_logourl, self.silent)
                else:
                    logger.debug("%s: no resolvable stream URL, skipping from bouquet", ch_name)
                self.chitem += 1
                reactor.callFromThread(self.updateStatus, self.WAITING_FOR_CHANNEL_TEXT + ch_name)
            elif self.total > 0 and self.resolved_count == 0:
                logger.error("%s: no channel resolved a stream URL, leaving existing bouquet unchanged", self.bouquetCC)
                reactor.callFromThread(self.noCategories)
                self._managerThread()
            else:
                bouquet_name = self._bouquetName(self.bouquetCC)
                bouquet_file = self.BOUQUET_FILE % self.bouquetCC
                reactor.callFromThread(eDVBDB.getInstance().addOrUpdateBouquet, bouquet_name, bouquet_file, self.bouquet, False)
                bouquet_path = "/etc/enigma2/" + bouquet_file
                if os.path.isfile(bouquet_path):
                    with open(bouquet_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    if lines and lines[0].startswith("#NAME"):
                        lines[0] = f"#NAME {bouquet_name}\r\n"
                        with open(bouquet_path, "w", encoding="utf-8") as f:
                            f.writelines(lines)
                self._afterBouquetWritten(self.bouquetCC, bouquet_name)
                os.makedirs(os.path.dirname(self.TIMER_FILE), exist_ok=True)
                with open(self.TIMER_FILE, "w", encoding="utf-8") as f:
                    f.write(str(time.time()))
                time.sleep(self.FINALIZE_DELAY)
                self._managerThread()

    def start(self):
        pass

    def stop(self):
        pass

    def exitOk(self, answer=None):
        pass

    def updateProgressBar(self, param):
        pass

    def updateStatus(self, name):
        pass

    def updateAction(self, cc=""):
        pass

    def noCategories(self):
        pass


class TVDownloadScreenMixin:
    """Shared UI plumbing for the interactive download-progress Screen.

    The host class (which must also inherit its plugin's TVDownloadBase
    subclass and Screen, in that order) still owns __init__ (skinName, title
    and widget creation need the plugin's own translated strings), and must
    define EXIT_CONFIRM_TEXT plus a _restartSilentTimer() hook.
    """

    def init(self):
        self["progress"].setValue(0)
        threads.deferToThread(self.download)

    def exit(self):
        self.session.openWithCallback(self.cleanup, MessageBox, self.EXIT_CONFIRM_TEXT, MessageBox.TYPE_YESNO, timeout=30)

    def cleanup(self, answer=None):
        if answer:
            self._setDownloadActive(False)
            self.exitOk(answer)

    def exitOk(self, answer=True):
        if answer:
            self._restartSilentTimer()
            self.close(True)

    def updateProgressBar(self, param):
        try:
            progress = min(((param + 1) * 100) // self.total, 100)
        except Exception:
            progress = 0
        logger.debug("progress: %s", progress)
        self["progress"].setValue(progress)
        self["wait"].text = str(progress) + " %"

    def updateStatus(self, name):
        self["status"].text = name


class TVDownloadSilentMixin:
    """Shared background/silent-timer plumbing for FAST-channel TV Cockpit plugins.

    The host class still owns __init__ (it must call the plugin's
    TVDownloadBase.__init__(self, silent=True) and create self.timer), and
    must define BOUQUET_MARKER, FRIENDLY_NAME and LOCATION_WORD.
    """

    def init(self, session):
        self.session = session
        with open("/etc/enigma2/bouquets.tv", "r", encoding="utf-8") as f:
            bouquets = f.read()
        if self.BOUQUET_MARKER in bouquets:
            self.start(True)

    def start(self, fromSessionStart=False):
        self.stop()
        minutes = 60 * 5
        if fileExists(self.TIMER_FILE):
            with open(self.TIMER_FILE, "r", encoding="utf-8") as f:
                last = float(f.read().strip())
            minutes -= int((time.time() - last) / 60)
            if minutes < 0:
                minutes = 1
        self.timer.startLongTimer(minutes * 60)
        if not fromSessionStart:
            self.afterUpdateCallbacks()

    def stop(self):
        self.timer.stop()

    def afterUpdateCallbacks(self):
        for f in self.afterUpdate:
            if callable(f):
                f()

    def noCategories(self):
        logger.debug("There is no data, it is possible that %s is not available in your %s.", self.FRIENDLY_NAME, self.LOCATION_WORD)


def importXMLTVGuide(epgcache, log_prefix, path, xmltv_bytes, channels_map):
    """Import EPG events from an XMLTV byte blob into eEPGCache via EPGImport's
    converter, using a pre-built {channel_id: service_ref} mapping.

    *xmltv_bytes* is also written to *path*, a supplementary artifact for
    tools other than Enigma2's own EPG cache to consume, from the same guide
    data already fetched here. Best-effort: a failure to write it doesn't
    affect the return value or abort the actual eEPGCache import below.

    Returns False if the EPGImport plugin itself isn't installed (the import
    was skipped entirely, not just unsuccessful) so callers can surface this
    to the user instead of it only ever showing up as a debug-log print
    nobody reads - this is a hard requirement (there is no fallback path),
    so silently no-op'ing left users with bouquets/channels updating fine
    and no indication at all of why the guide stayed empty. Returns True
    otherwise, regardless of how many events actually matched a channel
    (that part already gets its own "N EPG events imported" print).
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fp:
            fp.write(xmltv_bytes)
    except OSError as e:
        logger.error("%s: failed to write XMLTV guide to %s: %s", log_prefix, path, e)

    try:
        from Plugins.Extensions.EPGImport.xmltvconverter import XMLTVConverter
    except ImportError:
        logger.error("%s: EPGImport not available, skipping EPG import", log_prefix)
        return False

    try:
        xmltv_parser = XMLTVConverter(channels_map, {})
        evt_cnt = 0
        for item in xmltv_parser.enumFile(BytesIO(xmltv_bytes)):
            if not item:
                continue
            sref, event = item
            evt_cnt += 1
            reactor.callFromThread(epgcache.importEvents, sref, [event])
        logger.debug("%s: %s EPG events imported", log_prefix, evt_cnt)
    except Exception as e:
        logger.error("%s: EPG import error: %s", log_prefix, e)
    return True
