# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0

import re
import time
import uuid

from Components.config import config
from enigma import eServiceReference
import requests

from .Debug import logger
from .PlutoTVConfig import pickForwardIP, TSIDS
from .Variables import STREAM_POOL_SIZE, USER_AGENT
from . import LiveProxy


class _PlutoSlot:
    """One virtual device with its own clientID, HTTP session and boot cache."""

    def __init__(self, index, stitcher_fallback):
        self.index = index
        self._stitcher_fallback = stitcher_fallback
        self.session = requests.Session()
        self.client_id = str(uuid.uuid4())
        self.bootCache = {}

    @staticmethod
    def _tokenExpiry(token):
        try:
            from json import loads
            from base64 import urlsafe_b64decode
            payload = token.split(".")[1]
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += "=" * padding
            return loads(urlsafe_b64decode(payload)).get("exp", 0)
        except Exception:
            return 0

    def boot(self, country=None, allow_stale=False):
        country = country or config.plugins.plutotv.country.value
        now = time.time()

        if country in self.bootCache:
            if now < self.bootCache[country]["exp"] - 60:
                return self.bootCache[country]["response"]
            if allow_stale:
                return self.bootCache[country]["response"]
        elif allow_stale:
            return {}

        headers = {
            'authority': 'boot.pluto.tv',
            'accept': '*/*',
            'accept-language': 'en-US,en;q=0.9',
            'origin': 'https://pluto.tv',
            'referer': 'https://pluto.tv/',
            'sec-ch-ua': '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Linux"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'user-agent': USER_AGENT,
        }

        params = {
            'appName': 'web',
            'appVersion': '8.0.0-111b2b9dc00bd0bea9030b30662159ed9e7c8bc6',
            'deviceVersion': '122.0.0',
            'deviceModel': 'web',
            'deviceMake': 'chrome',
            'deviceType': 'web',
            'clientID': self.client_id,
            'clientModelNumber': '1.0.0',
            'serverSideAds': 'false',
            'deviceDNT': 'false',
            'drmCapabilities': 'widevine:L3',
            'blockingMode': '',
        }

        if ip := pickForwardIP(country):
            headers['X-Forwarded-For'] = ip

        try:
            response = self.session.get(PlutoTVRequest.BOOT_URL, headers=headers, params=params, timeout=3)
            response.raise_for_status()
            resp = response.json()
            self.bootCache[country] = {
                "response": resp,
                "exp": self._tokenExpiry(resp.get("sessionToken", "")),
                "stitcherUrl": resp.get("servers", {}).get("stitcher", self._stitcher_fallback),
                "stitcherParams": resp.get("stitcherParams", ""),
            }
            logger.debug("Slot %s new token for %s, stitcher=%s",
                         self.index, country, self.bootCache[country]['stitcherUrl'])
            return resp
        except Exception as e:
            logger.debug("Slot %s boot error: %s", self.index, e)
            if country in self.bootCache:
                return self.bootCache[country]["response"]
            return {}


class PlutoTVRequest:
    BASE_API = "https://api.pluto.tv"
    BOOT_URL = "https://boot.pluto.tv/v4/start"
    CHANNELS_URL = "https://service-channels.clusters.pluto.tv/v2/guide/channels"
    CATEGORIES_URL = "https://service-channels.clusters.pluto.tv/v2/guide/categories"
    TIMELINES_URL = "https://service-channels.clusters.pluto.tv/v2/guide/timelines"
    STITCHER_FALLBACK = "https://cfd-v4-service-channel-stitcher-use1-1.prd.pluto.tv"
    BASE_VOD = BASE_API + "/v3/vod/categories?includeItems=true&deviceType=web"
    SEASON_VOD = BASE_API + "/v3/vod/series/%s/seasons?includeItems=true&deviceType=web"

    LEGACY_CHANNELS_URL = BASE_API + "/v2/channels.json"
    LEGACY_GUIDE_URL = BASE_API + "/v2/channels"

    PLUTO_SCHEMA = "pluto%3a//"

    JMP2_URL_TEMPLATE = "https://jmp2.uk/plu-%s.m3u8"

    MJH_PLAYLIST_URL = "https://i.mjh.nz/PlutoTV/%s.m3u8"

    def __init__(self):
        self._pool = [_PlutoSlot(i, self.STITCHER_FALLBACK) for i in range(STREAM_POOL_SIZE)]
        self._stream_index = 0
        self.requestCache = {}
        self._sid = str(uuid.uuid1().hex)
        self._deviceId = str(uuid.uuid4().hex)

    @property
    def session(self):
        return self._pool[0].session

    @property
    def bootCache(self):
        return self._pool[0].bootCache

    @bootCache.setter
    def bootCache(self, value):
        self._pool[0].bootCache = value

    def _nextStreamSlot(self):
        """Round-robin through pool slots for stream URLs."""
        slot = self._pool[self._stream_index % STREAM_POOL_SIZE]
        self._stream_index += 1
        return slot

    def boot(self, country=None):
        """Boot the primary (metadata) slot."""
        return self._pool[0].boot(country)

    def _authHeaders(self, country=None):
        """Build authorization headers for service-channels API."""
        country = country or config.plugins.plutotv.country.value
        token = self.boot(country).get('sessionToken', '')
        headers = {
            'authority': 'service-channels.clusters.pluto.tv',
            'accept': '*/*',
            'accept-language': 'en-US,en;q=0.9',
            'authorization': f'Bearer {token}',
            'origin': 'https://pluto.tv',
            'referer': 'https://pluto.tv/',
        }
        if ip := pickForwardIP(country):
            headers['X-Forwarded-For'] = ip
        return headers

    def buildStreamURL(self, channel_id, country=None):
        """Build authenticated stitcher stream URL.

        Uses a pool slot so each concurrent stream gets its own clientID
        device identity, preventing Pluto from killing concurrent streams.

        Uses allow_stale=True so this never blocks the main thread waiting
        for an HTTP response.  A background refresh is scheduled if the
        cached token was stale.

        If *channel_id* already has an active LiveProxy session (e.g. the
        live view is already watching it when instant-record resolves its
        own sref for the same channel a few seconds later), reuse that
        session's proxy URL as-is instead of minting a new pool slot.
        register_channel()'s idempotent-reuse path unconditionally
        overwrites the already-running channel's master_url/url_refresher
        with whatever this call passes in - so calling through with a new
        slot here would silently repoint an already-live channel at a
        *different* device identity's session mid-stream, with the
        prefetch threads that were already running under the old slot
        left going. Two Pluto sessions for "the same channel" aren't
        guaranteed to share an absolute PTS timeline or ad-stitching
        state, which is exactly what was corrupting instant recordings of
        an already-live channel (audio DTS glitches / occasional decoder
        frame corruption right around the slot swap, with no genuine
        #EXT-X-DISCONTINUITY tag involved).
        """
        existing_url = LiveProxy.active_channel_url(channel_id)
        if existing_url is not None:
            return existing_url
        country = country or config.plugins.plutotv.country.value
        slot = self._nextStreamSlot()
        slot.boot(country, allow_stale=True)
        cache = slot.bootCache.get(country, {})
        now = time.time()
        if not cache or now >= cache.get("exp", 0) - 60:
            self._scheduleBackgroundRefresh(slot, country)
        token = cache.get('response', {}).get('sessionToken', '')
        stitcherUrl = cache.get('stitcherUrl', self.STITCHER_FALLBACK)
        stitcherParams = cache.get('stitcherParams', '')
        url = (
            f"{stitcherUrl}/v2/stitch/hls/channel/{channel_id}/master.m3u8"
            f"?jwt={token}&masterJWTPassthrough=true"
        )
        if stitcherParams:
            url += f"&{stitcherParams}"
        logger.debug("buildStreamURL slot %s for %s", slot.index, channel_id)
        LiveProxy.start()

        def _url_refresher(_slot=slot, _country=country, _ch=channel_id):
            _slot.boot(_country)
            _c = _slot.bootCache.get(_country, {})
            _token = _c.get('response', {}).get('sessionToken', '')
            _su = _c.get('stitcherUrl', self.STITCHER_FALLBACK)
            _sp = _c.get('stitcherParams', '')
            _u = f"{_su}/v2/stitch/hls/channel/{_ch}/master.m3u8?jwt={_token}&masterJWTPassthrough=true"
            if _sp:
                _u += f"&{_sp}"
            return _u
        return LiveProxy.register_channel(channel_id, url, url_refresher=_url_refresher)

    @staticmethod
    def _scheduleBackgroundRefresh(slot, country):
        """Refresh *slot*'s boot token for *country* in a background thread."""
        try:
            from twisted.internet import threads
            threads.deferToThread(slot.boot, country)
        except Exception:
            pass

    def _apiHeaders(self, country=None):
        """Build authorization headers for api.pluto.tv endpoints (VOD)."""
        country = country or config.plugins.plutotv.country.value
        token = self.boot(country).get('sessionToken', '')
        headers = {
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'authorization': f'Bearer {token}',
            'origin': 'https://pluto.tv',
            'referer': 'https://pluto.tv/',
            'user-agent': USER_AGENT,
        }
        if ip := pickForwardIP(country):
            headers['X-Forwarded-For'] = ip
        return headers

    def _legacyHeaders(self, country=None):
        """Build headers for legacy api.pluto.tv endpoints (no Bearer token needed)."""
        headers = {
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'host': 'api.pluto.tv',
            'connection': 'keep-alive',
            'referer': 'http://pluto.tv/',
            'origin': 'http://pluto.tv',
            'user-agent': USER_AGENT,
        }
        if ip := pickForwardIP(country or config.plugins.plutotv.country.value):
            headers['X-Forwarded-For'] = ip
        return headers

    def getMjhStreams(self, country=None):
        """Fetch and parse the i.mjh.nz PlutoTV m3u8 playlist.

        Returns a dict mapping channel_id -> stream_url.
        Results are cached for 4 hours.
        """
        country = country or config.plugins.plutotv.country.value
        now = time.time()
        if country not in self.requestCache:
            self.requestCache[country] = {}
        cache_key = "_mjh_streams"
        if cache_key in self.requestCache[country] and self.requestCache[country][cache_key][1] > (now - 4 * 3600):
            return self.requestCache[country][cache_key][0]

        url = self.MJH_PLAYLIST_URL % country
        streams = {}
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            channel_id = None
            for line in response.text.splitlines():
                line = line.strip()
                if line.startswith("#EXTINF:"):
                    if not (match := re.search(r'channel-id="([^"]+)"', line)):
                        if (match := re.search(r'tvg-id="([^"]+)"', line)):
                            channel_id = match.group(1).split(".")[0]
                        else:
                            channel_id = None
                    else:
                        channel_id = match.group(1)
                elif line and not line.startswith("#") and channel_id:
                    streams[channel_id] = line
                    channel_id = None
            self.requestCache[country][cache_key] = (streams, now)
            logger.debug("getMjhStreams: %s channels for %s", len(streams), country)
        except Exception as e:
            logger.debug("getMjhStreams error for %s: %s", country, e)
        return streams

    def getURL(self, url, param=None, header=None, life=60 * 15, country=None):
        if header is None:
            header = {"User-agent": USER_AGENT}
        if param is None:
            param = {}
        now = time.time()
        if (country := country or config.plugins.plutotv.country.value) not in self.requestCache:
            self.requestCache[country] = {}
        if url in self.requestCache[country] and self.requestCache[country][url][1] > (now - life):
            return self.requestCache[country][url][0]
        try:
            import threading
            if threading.current_thread() is threading.main_thread():
                import traceback
                logger.debug('WARNING: getURL(%r) called on main thread!', url)
                traceback.print_stack()
            req = requests.get(url, param, headers=header, timeout=10)
            req.raise_for_status()
            response = req.json()
            req.close()
            self.requestCache[country][url] = (response, now)
            return response
        except Exception:
            return {}

    def buildVodStreamURL(self, vod_url, country=None):
        """Rewrite a VOD stitched URL to use the correct stitcher host + JWT auth,
        then register it with LiveProxy and return the local proxy URL.

        Uses a pool slot so each concurrent stream gets its own clientID
        device identity, preventing Pluto from killing concurrent streams.
        """
        country = country or config.plugins.plutotv.country.value
        slot = self._nextStreamSlot()
        slot.boot(country)
        cache = slot.bootCache.get(country, {})
        token = cache.get('response', {}).get('sessionToken', '')
        stitcherUrl = cache.get('stitcherUrl', self.STITCHER_FALLBACK)
        stitcherParams = cache.get('stitcherParams', '')

        path = vod_url.split('?')[0]
        path = re.sub(r'^https?://[^/]+', '', path)

        if path.startswith('/stitch/'):
            path = '/v2' + path

        url = (
            f"{stitcherUrl}{path}"
            f"?jwt={token}&masterJWTPassthrough=true"
        )
        if stitcherParams:
            url += f"&{stitcherParams}"

        match = re.search(r'/([^/]+)/master\.m3u8', path)
        prefix = f'vod_{match.group(1)}' if match else 'vod'
        channel_id = f'{prefix}_{uuid.uuid4().hex[:8]}'
        LiveProxy.start()
        return LiveProxy.register_channel(channel_id, url)

    def getVOD(self, epid, country=None):
        country = country or config.plugins.plutotv.country.value
        return self.getURL(self.SEASON_VOD % epid, header=self._apiHeaders(country), life=60 * 60, country=country)

    def getOndemand(self, country=None):
        country = country or config.plugins.plutotv.country.value
        return self.getURL(self.BASE_VOD, header=self._apiHeaders(country), life=60 * 60, country=country)

    def getChannels(self, country=None):
        """Fetch channels via v2/guide/channels + categories, returned in legacy format.

        Falls back to the legacy api.pluto.tv endpoint if the new API returns
        no data (some countries like Finland are not on the new API).
        """
        country = country or config.plugins.plutotv.country.value
        headers = self._authHeaders(country)
        params = {'channelIds': '', 'offset': '0', 'limit': '1000', 'sort': 'number:asc'}

        try:
            response = self.session.get(self.CHANNELS_URL, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            channel_list = response.json().get("data", [])
        except Exception as e:
            logger.debug("getChannels new API error for %s: %s", country, e)
            channel_list = []

        if not channel_list:
            logger.debug("getChannels: new API returned no channels for %s, trying legacy API", country)
            return self._getChannelsLegacy(country)

        try:
            response = self.session.get(self.CATEGORIES_URL, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            cat_data = response.json().get("data", [])
        except Exception:
            cat_data = []

        categories = {}
        for elem in cat_data:
            cat_name = elem.get('name', '')
            for ch_id in elem.get('channelIDs', []):
                categories[ch_id] = cat_name

        result = []
        for ch in channel_list:
            ch_id = ch.get('id', '')
            logo_url = next(
                (img["url"] for img in ch.get("images", []) if img.get("type") == "colorLogoPNG"),
                None
            )
            result.append({
                '_id': ch_id,
                'name': ch.get('name', ''),
                'slug': ch.get('slug', ''),
                'number': ch.get('number', 0),
                'category': categories.get(ch_id, ''),
                'colorLogoPNG': {'path': logo_url},
            })

        return result

    def _getChannelsLegacy(self, country):
        """Fetch channels via the legacy api.pluto.tv/v2/channels.json endpoint."""
        params = {'sid': self._sid, 'deviceId': self._deviceId}
        headers = self._legacyHeaders(country)
        try:
            response = requests.get(self.LEGACY_CHANNELS_URL, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            channels = response.json()
            if isinstance(channels, list):
                logger.debug("getChannels legacy API returned %s channels for %s", len(channels), country)
                return channels
            logger.debug("getChannels legacy API unexpected response for %s", country)
            return []
        except Exception as e:
            logger.debug("getChannels legacy API error for %s: %s", country, e)
            return []

    def getBaseGuide(self, start, stop, country=None):
        """Fetch guide data via v2/guide/timelines, returned in legacy format.

        Falls back to the legacy api.pluto.tv endpoint if the new API returns
        no data (some countries like Finland are not on the new API).
        """
        country = country or config.plugins.plutotv.country.value
        headers = self._authHeaders(country)

        if not (channels := self.getChannels(country)):
            return []

        channel_ids = [ch['_id'] for ch in channels]
        channel_lookup = {ch['_id']: ch for ch in channels}

        all_entries = []
        group_size = 100
        for i in range(0, len(channel_ids), group_size):
            group = channel_ids[i:i + group_size]
            params = {
                'start': start,
                'channelIds': ','.join(group),
                'duration': '1440',
            }
            try:
                response = self.session.get(self.TIMELINES_URL, params=params, headers=headers, timeout=10)
                response.raise_for_status()
                data = response.json().get("data", [])
                for entry in data:
                    ch_id = entry.get('channelId', '')
                    ch_data = channel_lookup.get(ch_id, {})
                    all_entries.append({
                        '_id': ch_id,
                        'number': ch_data.get('number', 0),
                        'name': ch_data.get('name', ''),
                        'timelines': entry.get('timelines', []),
                    })
            except Exception as e:
                logger.debug("getBaseGuide new API error for %s: %s", country, e)

        if not all_entries:
            logger.debug("getBaseGuide: new API returned no data for %s, trying legacy API", country)
            return self._getBaseGuideLegacy(start, stop, country)

        return all_entries

    def _getBaseGuideLegacy(self, start, stop, country):
        """Fetch guide data via the legacy api.pluto.tv/v2/channels endpoint."""
        params = {'start': start, 'stop': stop, 'sid': self._sid, 'deviceId': self._deviceId}
        headers = self._legacyHeaders(country)
        try:
            response = requests.get(self.LEGACY_GUIDE_URL, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            guide = response.json()
            if isinstance(guide, list):
                logger.debug("getBaseGuide legacy API returned %s entries for %s", len(guide), country)
                return guide
            logger.debug("getBaseGuide legacy API unexpected response for %s", country)
            return []
        except Exception as e:
            logger.debug("getBaseGuide legacy API error for %s: %s", country, e)
            return []


plutoRequest = PlutoTVRequest()


def startProactiveRefresh():
    """Periodically refresh boot tokens before they expire so that
    synchronous callers (service extensions) never have to block."""
    from twisted.internet import reactor, threads
    now = time.time()
    for slot in plutoRequest._pool:
        for country, cache in list(slot.bootCache.items()):
            if now >= cache.get("exp", 0) - 300:
                threads.deferToThread(slot.boot, country)
    reactor.callLater(120, startProactiveRefresh)


_PROXY_STREAM_RE = re.compile(r'/(?:auto|rec)/([0-9a-f]+)\.(?:m3u8|ts)$', re.IGNORECASE)


def _pluto_channel_id(sref):
    """Return the Pluto channel_id encoded in *sref*, or None.

    Recognizes both the original pluto://<id> scheme and a sref already
    rewritten to this proxy's /auto/<id>.m3u8 or /rec/<id>.ts endpoint.
    The latter matters for instant recording: unlike a timer, it hands
    recordServiceExtension the currently-playing service reference, which
    playServiceExtension has already rewritten to the local /auto/ URL by
    the time it gets here - the pluto:// prefix is long gone, so without
    this fallback recordServiceExtension can't tell it's a Pluto channel
    and never rewrites it to the /rec/ endpoint recording needs (see
    recordServiceExtension's docstring).

    Pure parsing, no side effects - unlike _resolve_pluto_sref, which calls
    buildStreamURL (a stitcher token fetch + LiveProxy registration) as
    part of resolving a sref for playback. Callers that only need to compare
    channel_ids (e.g. _is_recording) must not pay that cost just to answer
    a read-only membership check.
    """
    parts = sref.toString().split(":")
    if len(parts) <= 10:
        return None
    stream = parts[10]
    if stream.lower().startswith(plutoRequest.PLUTO_SCHEMA):
        return stream[len(plutoRequest.PLUTO_SCHEMA):]
    m = _PROXY_STREAM_RE.search(stream)
    return m.group(1) if m else None


def _resolve_pluto_sref(sref):
    """Rewrite *sref* to the local proxy URL if it encodes a Pluto channel.

    Returns (sref, channel_id_or_None) so callers can tell whether anything
    was resolved without re-parsing the reference themselves.
    """
    channel_id = _pluto_channel_id(sref)
    if channel_id is None:
        return sref, None
    parts = sref.toString().split(":")
    cc = {v: k for k, v in TSIDS.items()}.get(parts[4], None)
    stream_url = plutoRequest.buildStreamURL(channel_id, cc)
    parts[10] = stream_url.replace(":", "%3a")
    sref = eServiceReference(":".join(parts))
    return sref, channel_id


_live_channel_id = None

_nav_instance = None

_recording_bases = {}


def _is_recording(nav, channel_id):
    """True if a currently-running, non-justplay record timer targets *channel_id*.

    Used to veto closing a channel that the live slot is leaving but that a
    background recording still depends on - see playServiceExtension.
    """
    for timer in nav.RecordTimer.timer_list:
        if timer.isRunning() and not timer.justplay:
            if _pluto_channel_id(timer.service_ref.ref) == channel_id:
                return True
    return False


def playServiceExtension(_nav, sref, *_args, **_kwargs):
    """Called by Enigma2 for every playService() call, of any service type.

    Besides resolving the stream URL, this is the only per-zap signal we get
    that the live slot has moved on: zapping to a non-Pluto channel never
    touches buildStreamURL at all, so without closing the previous channel
    here, its pre-fetch threads would keep polling the CDN indefinitely.
    Skipped when a record timer is still actively recording that channel
    (see _is_recording): closing it then would kill its pre-fetch threads
    mid-recording, degrading it to silent/high-latency segments exactly the
    way register_channel's docstring describes a different code path having
    once done to a recording in production.
    """
    global _live_channel_id
    sref, channel_id = _resolve_pluto_sref(sref)
    if (_live_channel_id and _live_channel_id != channel_id
            and not _is_recording(_nav, _live_channel_id)):
        LiveProxy.close_channel(_live_channel_id)
    _live_channel_id = channel_id
    return sref, False


def recordServiceExtension(_nav, sref, *_args, **_kwargs):
    """Rewrites a recording's sref to the RecordingProxy endpoint.

    _resolve_pluto_sref() still runs first so the channel is registered and
    its pre-fetch threads are running exactly as for live playback. The
    result is then repointed from the segment-chunked HLS URL
    (/auto/{channel_id}.m3u8) live playback uses to RecordingProxy's
    continuous, container-harmonized re-mux endpoint
    (/rec/{channel_id}.ts) - see RecordingProxy.py for why a recorded file
    needs different framing than a live HLS pipeline.

    Also captures _nav into the module-level _nav_instance: this runs
    (inside nav.recordService()) after RecordTimerEntry.Filename is already
    computed but before record_service.prepare() is called, so it's the
    earliest point at which the destination filename ApSc needs is both
    resolvable and stable - see recording_filename_for.
    """
    global _nav_instance
    _nav_instance = _nav
    sref, channel_id = _resolve_pluto_sref(sref)
    if channel_id is not None:
        rec_url = f'http://{LiveProxy.PROXY_HOST}:{LiveProxy.PROXY_PORT}/rec/{channel_id}.ts'
        parts = sref.toString().split(":")
        parts[10] = rec_url.replace(":", "%3a")
        sref = eServiceReference(":".join(parts))
    return sref


def recording_filename_for(channel_id):
    """Return the .ts path *channel_id*'s active, non-justplay timer is
    currently writing to - the file RecordingProxy's ApScWriter should
    mirror .ap/.sc entries against - or None if no such timer is found.

    eServiceMP3Record has a patched EOS-recovery path
    (restartRecordingFromEos in servicemp3record.cpp) that, entirely inside
    GStreamer/C++ with no Python-visible signal, renames the output file
    mid-recording by appending "_001.ts" and reopens a fresh pipeline
    against the same /rec/{channel_id}.ts URL when the HTTP stream sees a
    spurious EOS. That reopens triggers a brand new GET to RecordingProxy,
    i.e. a fresh call here for the same still-running timer. Comparing
    against _recording_bases lets this tell that apart from a genuinely new
    recording and reproduce the same rename, so ApSc's sidecar files follow
    the recording across the restart instead of going stale on the old
    filename.
    """
    if _nav_instance is None:
        return None
    base = None
    for timer in _nav_instance.RecordTimer.timer_list:
        if timer.justplay or not timer.Filename:
            continue
        if _pluto_channel_id(timer.service_ref.ref) == channel_id:
            base = timer.Filename + '.ts'
            break
    if base is None:
        _recording_bases.pop(channel_id, None)
        return None
    prev_base, prev_filename = _recording_bases.get(channel_id, (None, None))
    if prev_base == base:
        filename = prev_filename.replace('.ts', '_001.ts', 1)
    else:
        filename = base
    _recording_bases[channel_id] = (base, filename)
    return filename
