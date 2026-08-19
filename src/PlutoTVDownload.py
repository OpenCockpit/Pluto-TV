# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0

import datetime
import os
import time
import zlib

from Components.ActionMap import ActionMap
from Components.config import config
from Components.Label import Label
from Components.ProgressBar import ProgressBar
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from enigma import eTimer
from twisted.internet import reactor

from . import _
from .Debug import logger
from .ConfigInit import COUNTRY_NAMES, TSIDS, getselectedcountries
from .LiveProxy import PROXY_HOST, PROXY_PORT
from .PlutoTVRequest import plutoRequest
from .Variables import TIMER_FILE, NODATA_FILE, BOUQUET_FILE, BOUQUET_NAME, CHANNELLIST_FILE, XMLTV_FILE
from .CockpitTVDownload import TVDownloadBase, TVDownloadScreenMixin, TVDownloadSilentMixin
from .M3UPlaylist import writeM3UPlaylist
from .XMLTVWriter import writeXMLTVFile


class PlutoTVDownloadBase(TVDownloadBase):
    downloadActive = False

    TIMER_FILE = TIMER_FILE
    NODATA_FILE = NODATA_FILE
    BOUQUET_FILE = BOUQUET_FILE
    CHANNELLIST_FILE = CHANNELLIST_FILE
    XMLTV_FILE = XMLTV_FILE
    TSIDS = TSIDS

    FINALIZE_DELAY = 3
    SILENT_IN_PROGRESS_TEXT = _("A silent download is in progress.")
    PICONS_LABEL = _("picons")
    FETCHING_PICONS_TEXT = _("Fetching picons...")
    UPDATE_COMPLETED_TEXT = _("Live-TV update completed")
    PROCESSING_TEXT = _("Processing data...")
    WAITING_FOR_CHANNEL_TEXT = _("Waiting for Channel: ")

    def __init__(self, silent=False, locations=None):
        TVDownloadBase.__init__(self, silent, locations)
        self.guideList = {}

    def _clearPluginState(self):
        self.guideList.clear()

    def _selectedLocations(self):
        return getselectedcountries()

    def _defaultLocation(self):
        return config.plugins.plutotv.country.value

    def _picons_config(self):
        return config.plugins.plutotv.picons

    def _configFolder(self):
        return config.plugins.plutotv.config_folder.value

    def _fetchChannels(self, cc):
        return plutoRequest.getChannels(cc)

    def _bouquetName(self, cc):
        return BOUQUET_NAME % COUNTRY_NAMES.get(cc, cc)

    def _afterBouquetWritten(self, cc, bouquet_name):
        entries = {
            group: [(chid, name, logo, f"http://{PROXY_HOST}:{PROXY_PORT}/auto/{chid}.m3u8")
                    for _sid, chid, name, logo, _chid2 in self.channelsList.get(group, [])]
            for group in self.categories
        }
        writeM3UPlaylist(os.path.join(self._configFolder(), self.CHANNELLIST_FILE % cc), bouquet_name, self.categories, entries)

        xmltv_channels = [
            (chid, name)
            for group in self.categories
            for _sid, chid, name, _logo, _chid2 in self.channelsList.get(group, [])
        ]
        xmltv_programmes = [
            (chid, start, start + duration, title, desc)
            for chid, _name in xmltv_channels
            for title, desc, start, duration, _genre, _event_id in self.guideList.get(chid, [])
        ]
        writeXMLTVFile(os.path.join(self._configFolder(), self.XMLTV_FILE % cc), xmltv_channels, xmltv_programmes)

    def _importGuide(self, cc):
        guide = self.getGuidedata(cc)
        for event in guide:
            self.buildGuide(event)
        total_events = sum(len(v) for v in self.guideList.values())
        logger.debug("_importGuide: %s: %d guide entries fetched, %d channels with events, %d events total", cc, len(guide), len(self.guideList), total_events)

    def _buildBouquetEntry(self, key, chitem):
        ch_sid, ch_hash, ch_name, ch_logourl, _id = self.channelsList[key][chitem]

        mode = config.plugins.plutotv.live_tv_mode.value
        if mode == "jmp2":
            stream_url = (plutoRequest.JMP2_URL_TEMPLATE % _id).replace(":", "%3a")
        elif mode == "mjh":
            mjh_streams = plutoRequest.getMjhStreams(self.bouquetCC)
            if not (stream_url := mjh_streams.get(_id, "").replace(":", "%3a")):
                stream_url = plutoRequest.PLUTO_SCHEMA + _id
        else:
            stream_url = plutoRequest.PLUTO_SCHEMA + _id

        ref = f"4097:0:1:{ch_sid}:{self.tsid}:FF:CCCC0000:0:0:0"

        chevents = []
        if ch_hash in self.guideList:
            for evt in self.guideList[ch_hash]:
                title = evt[0]
                summary = evt[1]
                begin = int(round(evt[2]))
                duration = evt[3]
                genre = evt[4]
                event_id = evt[5]

                chevents.append((begin, duration, title, "", summary, genre, event_id))
        if len(chevents) > 0:
            iterator = iter(chevents)
            events_tuple = tuple(iterator)
            reactor.callFromThread(self.epgcache.importEvents, f"{ref}:{stream_url}", events_tuple)

        return ref, stream_url, ch_name, ch_logourl

    def buildGuide(self, event):
        _id = event.get("_id", "")
        if len(_id) == 0:
            return
        self.guideList[_id] = []
        timelines = event.get("timelines", [])
        chplot = (event.get("description", "") or event.get("summary", ""))

        for item in timelines:
            episode = (item.get("episode", {}) or item)
            series = (episode.get("series", {}) or item)
            epdur = int(episode.get("duration", "0") or "0") // 1000
            epgenre = episode.get("genre", "")
            etype = series.get("type", "film")

            genre = self.convertgenre(epgenre)

            offset = datetime.datetime.now() - datetime.datetime.utcnow()
            try:
                starttime = self.strpTime(item["start"]) + offset
            except Exception:
                continue
            start = time.mktime(starttime.timetuple())
            title = (item.get("title", ""))
            tvplot = (series.get("description", "") or series.get("summary", "") or chplot)
            epnumber = episode.get("number", 0)
            epseason = episode.get("season", 0)
            epname = episode.get("name", "")
            epmpaa = episode.get("rating", "")
            epplot = (episode.get("description", "") or tvplot or epname)

            if len(epmpaa) > 0 and "Not Rated" not in epmpaa:
                epplot = f"({epmpaa}). {epplot}"

            noserie = ("live", "film")
            if epseason > 0 and epnumber > 0 and etype not in noserie:
                title = f"{title} (T{epseason})"
                epplot = f"T{epseason} Ep.{epnumber} {epplot}"

            if epdur > 0:
                item_id = str(item.get("_id") or episode.get("_id") or "")
                event_id = (zlib.crc32(item_id.encode("utf-8")) & 0xFFFF) or 1 if item_id else 0
                self.guideList[_id].append((title, epplot, start, epdur, genre, event_id))

    def buildM3U(self, channel):
        logo = (channel.get("colorLogoPNG", {}).get("path", None) or None)
        group = channel.get("category", "")
        _id = channel["_id"]

        if group not in self.channelsList:
            self.channelsList[group] = []
            self.categories.append(group)

        if int(channel["number"]) == 0:
            sid = int(_id[-4:], 16) if len(_id) >= 4 else 0
        else:
            sid = int(channel["number"])

        if sid <= 0:
            sid = (zlib.crc32(_id.encode("utf-8")) & 0xFFFF) or 1

        if sid in self.usedServiceIds:
            sid = (zlib.crc32(_id.encode("utf-8")) & 0xFFFF) or 1
            while sid in self.usedServiceIds:
                sid = (sid + 1) & 0xFFFF
                if sid == 0:
                    sid = 1

        self.usedServiceIds.add(sid)
        number = f"{sid:X}"

        self.channelsList[group].append((str(number), _id, channel["name"], logo, _id))
        return True

    @staticmethod
    def convertgenre(genre):
        genre_id = 0
        if genre in {"Classics", "Romance", "Thrillers", "Horror"} or "Sci-Fi" in genre or "Action" in genre:
            genre_id = 0x10
        elif "News" in genre or "Educational" in genre:
            genre_id = 0x20
        elif genre == "Comedy":
            genre_id = 0x30
        elif "Children" in genre:
            genre_id = 0x50
        elif genre == "Music":
            genre_id = 0x60
        elif genre == "Documentaries":
            genre_id = 0xA0
        return genre_id

    @staticmethod
    def getGuidedata(cc):
        start = (datetime.datetime.fromtimestamp(PlutoTVDownloadBase.getLocalTime()).strftime("%Y-%m-%dT%H:00:00Z"))
        stop = (datetime.datetime.fromtimestamp(PlutoTVDownloadBase.getLocalTime()) + datetime.timedelta(hours=24)).strftime("%Y-%m-%dT%H:00:00Z")
        return sorted(plutoRequest.getBaseGuide(start, stop, cc), key=lambda x: x["number"])

    @staticmethod
    def getLocalTime():
        offset = datetime.datetime.utcnow() - datetime.datetime.now()
        return time.time() + offset.total_seconds()

    @staticmethod
    def strpTime(datestring, fmt="%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.datetime.strptime(datestring, fmt)
        except TypeError:
            return datetime.datetime.fromtimestamp(time.mktime(time.strptime(datestring, fmt)))


class PlutoTVDownload(TVDownloadScreenMixin, PlutoTVDownloadBase, Screen):

    EXIT_CONFIRM_TEXT = _("The download is in progress. Exit now?")

    def __init__(self, session, locations=None):
        self.session = session
        Screen.__init__(self, session)
        self.skinName = "DownloadProgress"
        self.title = _("PlutoTV updating")
        PlutoTVDownloadBase.__init__(self, locations=locations)
        self.total = 0
        self["progress"] = ProgressBar()
        self["action"] = Label()
        self.updateAction()
        self["wait"] = Label()
        self["status"] = Label(_("Please wait..."))
        self["actions"] = ActionMap(["OkCancelActions"], {"cancel": self.exit}, -1)
        self.onFirstExecBegin.append(self.init)

    def updateAction(self, cc=""):
        self["action"].text = _("Updating: Pluto TV %s") % cc.upper()

    def noCategories(self, cc=""):
        self.session.open(MessageBox, _("There is no data for %s. It may be caused by geo-blocking, or Pluto TV may not be available in your country.") % COUNTRY_NAMES.get(cc, cc), type=MessageBox.TYPE_ERROR, timeout=10)

    def _restartSilentTimer(self):
        Silent.stop()
        Silent.start()


class DownloadSilent(TVDownloadSilentMixin, PlutoTVDownloadBase):

    BOUQUET_MARKER = "plutotvcockpit"
    FRIENDLY_NAME = "Pluto TV"
    LOCATION_WORD = "country"

    def __init__(self):
        self.afterUpdate = []
        PlutoTVDownloadBase.__init__(self, silent=True)
        self.timer = eTimer()
        self.timer.timeout.get().append(self.download)


Silent = DownloadSilent()
