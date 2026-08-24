# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0

import os
import re
from time import strftime, gmtime, localtime
from urllib.parse import quote
from twisted.internet import threads

from Components.ActionMap import HelpableActionMap
from Components.config import config
from Components.Button import Button
from Components.Label import Label
from Components.ScrollLabel import ScrollLabel
from Components.Sources.StaticText import StaticText
from Components.Pixmap import Pixmap
from Screens.ChoiceBox import ChoiceBox
from Screens.HelpMenu import HelpableScreen
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from Tools.Directories import fileExists, isPluginInstalled
from Tools.LoadPixmap import LoadPixmap
from enigma import BT_KEEP_ASPECT_RATIO, BT_SCALE, BT_HALIGN_CENTER, BT_VALIGN_CENTER, eServiceReference, eTimer
from skin import parameters

from . import _, __
from .Debug import logger
from .ConfigInit import COUNTRY_NAMES, getselectedcountries
from .PlutoTVRequest import plutoRequest
from .PlutoTVDownload import PlutoTVDownload, Silent
from .PRSUtils import PRSUtils
from .CockpitTVDownload import loadNoDataLocations
from .CockpitTVUtils import pickBestImage
from .Variables import TIMER_FILE, NODATA_FILE, BOUQUET_FILE
from .PRSList import PRSList
from .PlutoSetup import PlutoSetup
from .PRSPlayer import PRSPlayer

_utils = PRSUtils(config.plugins.plutotv)
downloadPoster = _utils.downloadPoster
resumePointsInstance = _utils.resumePoints


class PlutoTVCockpit(Screen, HelpableScreen):

    def __init__(self, session):
        self.session = session
        Screen.__init__(self, session)
        self.skinName = "PlutoTVCockpit"
        HelpableScreen.__init__(self)

        self.colors = parameters.get("PlutoTvColors", [])

        self["feedlist"] = PRSList([], icons=("menu", "series", "cine", "cine_half", "cine_end"), resume_points=resumePointsInstance)
        self["playlist"] = StaticText()
        self["loading"] = Label(_("Loading data... Please wait"))
        self["vtitle"] = StaticText()
        self["key_red"] = Button(_("Exit"))
        self["key_yellow"] = Button()
        self.mdb = isPluginInstalled("tmdb") and "tmdb" or isPluginInstalled("IMDb") and "imdb"
        self.yellowLabel = _("TMDb Search") if self.mdb == "tmdb" else (_("IMDb Search") if self.mdb else "")
        self["key_green"] = Button()
        self["updated"] = StaticText()
        self["key_menu"] = Button(_("MENU"))
        self["key_blue"] = Button(_("Change country"))
        self["poster"] = Pixmap()
        self["posterBG"] = Label()
        self["info"] = ScrollLabel()

        self["feedlist"].onSelectionChanged.append(self.update_data)

        self.picname = ""

        self["actions"] = HelpableActionMap(
            self, ["SetupActions", "InfobarChannelSelection", "MenuActions"],
            {
                "ok": (self.action, _("Go forward one level including starting playback")),
                "cancel": (self.exit, _("Go back one level including exiting")),
                "save": (self.green, _("Create or update PlutoTV live bouquets")),
                "historyBack": (self.back, _("Go back one level")),
                "menu": (self.loadSetup, _("Open the plugin configuration screen")),
            }, -1
        )

        self["MDBActions"] = HelpableActionMap(
            self, ["ColorActions"],
            {
                "yellow": (self.MDB, _("Search for information in %s") % (_("The Movie Database") if self.mdb == "tmdb" else _("the Internet Movie Database"))),
            }, -1
        )
        self["MDBActions"].setEnabled(False)

        self["CountryActions"] = HelpableActionMap(
            self,
            ["ColorActions"],
            {
                "blue": (self.switchCountry, _("Load the VoD list of another country")),
            },
            -1
        )

        self["InfoNavigationActions"] = HelpableActionMap(
            self, ["NavigationActions"],
            {
                "pageUp": (self["info"].pageUp, _("Scroll the information field")),
                "pageDown": (self["info"].pageDown, _("Scroll the information field")),
            }, -1
        )

        self.updatebutton()

        if self.updatebutton not in Silent.afterUpdate:
            Silent.afterUpdate.append(self.updatebutton)

        self.updateDataTimer = eTimer()
        self.updateDataTimer.callback.append(self.update_data_delayed)
        self.country = config.plugins.plutotv.country.value
        self.initialise()
        self.onLayoutFinish.append(self.getCategories)

    def initialise(self):
        self.titlemenu = _("VOD Menu") + (" - " + COUNTRY_NAMES[self.country] if self.country in COUNTRY_NAMES else "")
        self.films = []
        self.menu = []
        self.history = []
        self.chapters = {}
        self.numSeasons = 0
        self.vinfo = ""
        self.description = ""
        self.eptitle = ""
        self.epinfo = ""
        self["feedlist"].setList([])
        self["poster"].hide()
        self["posterBG"].hide()
        self["info"].setText("")
        self["vtitle"].setText("")
        self["playlist"].setText(self.titlemenu)
        self["loading"].show()
        self.title = _("PlutoTV") + " - " + self.titlemenu

    def update_data(self):
        self.updateDataTimer.stop()
        if not (selection := self.getSelection()):
            return
        _index, _name, __type, _id = selection
        self["MDBActions"].setEnabled(False)
        self["key_yellow"].text = ""
        if __type == "menu":
            self["poster"].hide()
            self["posterBG"].hide()
            self.updateInfo()
        else:
            self.updateDataTimer.start(500, 1)

    def update_data_delayed(self):
        if not (selection := self.getSelection()):
            return
        index, _name, __type, _id = selection
        if __type in {"movie", "series"}:
            film = self.films[index]
            self.description = film[2].decode("utf-8")
            self["vtitle"].text = film[1].decode("utf-8")
            info = film[4].decode("utf-8") + "       "
            self["MDBActions"].setEnabled(True)
            self["key_yellow"].text = self.yellowLabel

            if __type == "movie":
                info += strftime("%Hh %Mm", gmtime(int(film[5])))
            else:
                info += __("%s Season available", "%s Seasons available", film[10]) % film[10]
                self.numSeasons = film[10]
            self.vinfo = info
            picname = film[0] + ".jpg"
            self.picname = picname
            pic = film[6]
            if len(picname) > 5:
                self["poster"].hide()
                self["posterBG"].hide()
                threads.deferToThread(downloadPoster, pic, picname, self.downloadPosterCallback)

        elif __type == "seasons":
            self.eptitle = ""
            self.epinfo = ""
            if self.numSeasons == 1:
                self.lastActionTimer = eTimer()
                self.lastActionTimer.callback.append(self.lastAction)
                self.lastActionTimer.start(10, 1)

        elif __type == "episode":
            film = self.chapters[_id][index]
            self.eptitle = film[1].decode("utf-8") + "  " + strftime("%Hh %Mm", gmtime(int(film[5])))
            self.epinfo = film[3].decode("utf-8")
            self.updateInfo()

    def updateInfo(self):
        vinfoColored = self.vinfo and self.addColor(self.vinfo)
        eptitleColored = self.eptitle and self.addColor(self.eptitle)
        spacer = "\n" if (vinfoColored or self.description) and (eptitleColored or self.epinfo) else ""
        self["info"].setText("\n".join([x for x in (vinfoColored, self.description, spacer, eptitleColored, self.epinfo) if x]))

    def downloadPosterCallback(self, filename, name):
        if name == self.picname:
            self.updateInfo()
            self.showPoster(filename, name)

    def showPoster(self, filename, name):
        try:
            if name == self.picname and filename and os.path.isfile(filename):
                self["poster"].instance.setPixmapScale(BT_SCALE | BT_KEEP_ASPECT_RATIO | BT_HALIGN_CENTER | BT_VALIGN_CENTER)
                self["poster"].instance.setPixmap(LoadPixmap(filename))
                self["poster"].show()
                self["posterBG"].show()
        except Exception as ex:
            logger.debug("[PlutoScreen] showPoster, ERROR: %s", ex)

    def getCategories(self):
        self.lvod = {}
        threads.deferToThread(plutoRequest.getOndemand, self.country).addCallback(self.getCategoriesCallback)

    def getCategoriesCallback(self, ondemand):
        if not (categories := ondemand.get("categories", [])):
            self.session.open(MessageBox, _("There is no data for %s. It may be caused by geo-blocking, or Pluto TV may not be available in your country.") % COUNTRY_NAMES.get(self.country, self.country), type=MessageBox.TYPE_ERROR, timeout=10)
        else:
            for category in categories:
                self.buildlist(category)
            self.menu.sort(key=lambda x: re.sub(r"^[\W_]+", "", x.decode("utf-8").casefold()))
            for _key, items in self.lvod.items():
                items.sort(key=lambda x: re.sub(r"^[\W_]+", "", x[1].decode("utf-8")).casefold())
            entries = []
            for key in self.menu:
                entries.append(self["feedlist"].listentry(key.decode("utf-8"), "menu", ""))
            self["feedlist"].setList(entries)
        self["loading"].hide()

    def buildlist(self, category):
        name = category["name"].encode("utf-8")
        self.lvod[name] = []

        self.menu.append(name)
        items = category.get("items", [])
        for item in items:
            itemid = item.get("_id", "")
            if not itemid:
                continue
            itemname = item.get("name", "").encode("utf-8")
            itemsummary = item.get("summary", "").encode("utf-8")
            itemgenre = item.get("genre", "").encode("utf-8")
            itemrating = item.get("rating", "").encode("utf-8")
            itemduration = int(item.get("duration", "0") or "0") // 1000
            itemtype = item.get("type", "")
            seasons = len(item.get("seasonsNumbers", []))
            urls = item.get("stitched", {}).get("urls", [])
            url = urls[0].get("url", "") if urls else ""

            itemposter, itemimage = pickBestImage(item.get("covers", []))
            self.lvod[name].append((itemid, itemname, itemsummary, itemgenre, itemrating, itemduration, itemposter, itemimage, itemtype, url, seasons))

    def buildchapters(self, chapters):
        self.chapters.clear()
        items = chapters.get("seasons", [])
        for item in items:
            chs = item.get("episodes", [])
            for ch in chs:
                if (season := str(ch.get("season", 0))) != "0":
                    if season not in self.chapters:
                        self.chapters[season] = []
                    _id = ch.get("_id", "")
                    name = ch.get("name", "").encode("utf-8")
                    number = str(ch.get("number", 0))
                    summary = ch.get("description", "").encode("utf-8")
                    rating = ch.get("rating", "")
                    duration = ch.get("duration", 0) // 1000
                    genre = ch.get("genre", "").encode("utf-8")
                    imgs = ch.get("covers", [])
                    urls = ch.get("stitched", {}).get("urls", [])
                    url = urls[0].get("url", "") if urls else ""

                    itemposter, itemimage = pickBestImage(imgs)
                    self.chapters[season].append((_id, name, number, summary, rating, duration, genre, itemposter, itemimage, url))

    def getSelection(self):
        index = self["feedlist"].getSelectionIndex()
        if current := self["feedlist"].getCurrent():
            data = current[0]
            return index, data[0], data[1], data[2]
        return None

    def action(self):
        if not (selection := self.getSelection()):
            return
        self.lastAction = self.action
        index, name, __type, _id = selection
        menu = []
        menuact = self.titlemenu
        if __type == "menu":
            self.films = self.lvod[self.menu[index]]
            for x in self.films:
                sname = x[1].decode("utf-8")
                stype = x[8]
                sid = x[0]
                menu.append(self["feedlist"].listentry(sname, stype, sid))
            self["feedlist"].moveToIndex(0)
            self["feedlist"].setList(menu)
            self.titlemenu = name
            self["playlist"].text = self.titlemenu
            self.title = _("PlutoTV") + " - " + self.titlemenu
            self.history.append((index, menuact))
        elif __type == "series":
            self["loading"].show()
            self._series_name = name
            self._series_index = index
            self._series_menuact = menuact
            threads.deferToThread(plutoRequest.getVOD, _id, self.country).addCallback(self._getVODCallback)
        elif __type == "seasons":
            for key in self.chapters[_id]:
                sname = key[1].decode("utf-8")
                stype = "episode"
                sid = key[0]
                menu.append(self["feedlist"].listentry(_("Episode") + " " + key[2] + ". " + sname, stype, _id, key[0]))
            self["feedlist"].setList(menu)
            self.titlemenu = menuact.split(" - ")[0] + " - " + name
            self["playlist"].text = self.titlemenu
            self.title = _("PlutoTV") + " - " + self.titlemenu
            self.history.append((index, menuact))
            self["feedlist"].moveToIndex(0)
        elif __type == "movie":
            film = self.films[index]
            sid = film[0]
            name = film[1].decode("utf-8")
            url = film[9]
            self.playVOD(name, sid, url)
        elif __type == "episode":
            film = self.chapters[_id][index]
            sid = film[0]
            name = film[1].decode("utf-8")
            url = film[9]
            self.playVOD(name, sid, url)

    def back(self):
        if not (selection := self.getSelection()):
            return
        self.lastAction = self.back
        _index, _name, __type, _id = selection
        menu = []
        if self.history:
            hist = self.history[-1][0]
            histname = self.history[-1][1]
            if __type in {"movie", "series"}:
                for key in self.menu:
                    menu.append(self["feedlist"].listentry(key.decode("utf-8"), "menu", ""))
                self["vtitle"].text = ""
                self.vinfo = ""
                self.description = ""
            elif __type == "seasons":
                for x in self.films:
                    sname = x[1].decode("utf-8")
                    stype = x[8]
                    sid = x[0]
                    menu.append(self["feedlist"].listentry(sname, stype, sid))
            elif __type == "episode":
                for key in list(self.chapters.keys()):
                    sname = str(key)
                    stype = "seasons"
                    sid = str(key)
                    menu.append(self["feedlist"].listentry(_("Season") + " " + sname, stype, sid))
            self["feedlist"].setList(menu)
            self.history.pop()
            self["feedlist"].moveToIndex(hist)
            self.titlemenu = histname
            self["playlist"].text = self.titlemenu
            self.title = _("PlutoTV") + " - " + self.titlemenu
            if not self.history:
                self["poster"].hide()

    def _getVODCallback(self, chapters):
        self.buildchapters(chapters)
        menu = []
        for key in list(self.chapters.keys()):
            menu.append(self["feedlist"].listentry(_("Season") + " " + key, "seasons", key))
        self["feedlist"].setList(menu)
        self.titlemenu = self._series_name + " - " + _("Seasons")
        self["playlist"].text = self.titlemenu
        self.title = _("PlutoTV") + " - " + self.titlemenu
        self.history.append((self._series_index, self._series_menuact))
        self["feedlist"].moveToIndex(0)
        self["loading"].hide()

    def playVOD(self, name, sid, url=None):
        if url:
            self._play_name = name
            self._play_sid = sid
            threads.deferToThread(plutoRequest.buildVodStreamURL, url, self.country).addCallback(self._playVODCallback)

    def _playVODCallback(self, url):
        if url and self._play_name:
            string = f"4097:0:0:0:0:0:0:0:0:0:{quote(url)}:{quote(self._play_name)}"
            reference = eServiceReference(string)
            if "m3u8" in url.lower() or "127.0.0.1" in url:
                self.session.open(PRSPlayer, service=reference, sid=self._play_sid, resume_points=resumePointsInstance)

    def green(self):
        locations = [x for x in getselectedcountries() if x] or [config.plugins.plutotv.country.value]
        if len(locations) <= 1:
            self.session.openWithCallback(self.endupdateLive, PlutoTVDownload)
            return
        choices = [(_("All"), None)] + [(COUNTRY_NAMES.get(cc, cc), cc) for cc in locations]
        self.session.openWithCallback(
            self.greenChoice,
            ChoiceBox,
            title=_("Select a Live-TV bouquet to update"),
            list=choices,
            keys=[]
        )

    def greenChoice(self, result=None):
        if result is None:
            return
        self.session.openWithCallback(self.endupdateLive, PlutoTVDownload, locations=[result[1]] if result[1] else None)

    def endupdateLive(self, _ret=None):
        if _ret:
            countries = ", ".join(COUNTRY_NAMES.get(cc, cc) for cc in _ret)
            self.session.openWithCallback(self.updatebutton, MessageBox, _("The Pluto TV bouquets for %s in your channel list have been updated.\n\nThey will now be rebuilt automatically every 5 hours.") % countries, type=MessageBox.TYPE_INFO, timeout=10)
        else:
            self.updatebutton()

    def updatebutton(self, _ret=None):
        with open("/etc/enigma2/bouquets.tv", "r", encoding="utf-8") as f:
            bouquets = f.read()
        nodata = loadNoDataLocations(NODATA_FILE)
        if fileExists(TIMER_FILE) and all(((BOUQUET_FILE % cc) in bouquets or cc in nodata) for cc in [x for x in getselectedcountries() if x]):
            with open(TIMER_FILE, "r", encoding="utf-8") as f:
                last = float(f.read().replace("\n", "").replace("\r", ""))
            updated = strftime(" %x %H:%M", localtime(int(last)))
            self["key_green"].text = _("Update Live-TV Bouquet")
            self["updated"].text = _("Live-TV Bouquet last updated:") + updated
        elif "plutotvcockpit" in bouquets:
            self["key_green"].text = _("Update Live-TV Bouquet")
            self["updated"].text = _("Live-TV Bouquet needs updating. Press GREEN.")
        else:
            self["key_green"].text = _("Create Live-TV Bouquet")
            self["updated"].text = ""

    def exit(self, *_args, **_kwargs):
        if self.history:
            self.back()
        else:
            self.close()

    def MDB(self):
        if not (selection := self.getSelection()):
            return
        _index, name, __type, _id = selection
        if __type in {"movie", "series"} and self.mdb:
            if self.mdb == "tmdb":
                from Plugins.Extensions.tmdb.tmdb import tmdbScreen
                self.session.open(tmdbScreen, name, 2)
            else:
                from Plugins.Extensions.IMDb.plugin import IMDB
                self.session.open(IMDB, name, False)

    def loadSetup(self):
        def loadSetupCallback(_result=None):
            if config.plugins.plutotv.country.value != self.country:
                self.country = config.plugins.plutotv.country.value
                self.initialise()
                self.getCategories()
        self.session.openWithCallback(loadSetupCallback, PlutoSetup)

    def switchCountry(self):
        def switchCountryCallback(result=None):
            if result and result[1] != self.country:
                self.country = result[1]
                self.initialise()
                self.getCategories()
        self.session.openWithCallback(
            switchCountryCallback,
            ChoiceBox,
            title=_("Temporarily switch the VoD list to another country"),
            list=list(zip(config.plugins.plutotv.country.description, config.plugins.plutotv.country.choices)),
            selection=config.plugins.plutotv.country.choices.index(self.country),
            keys=[]
        )

    def addColor(self, text, i=1):
        if i < len(self.colors):
            text = rf"\c{self.colors[i]:08x}" + text + rf"\c{self.colors[0]:08x}"  # noqa: W605
        return text

    def close(self, *_args, **_kwargs):
        if self.updatebutton in Silent.afterUpdate:
            Silent.afterUpdate.remove(self.updatebutton)
        Screen.close(self)
