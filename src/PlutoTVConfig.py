# Copyright (C) 2026 by xcentaurix
# Copyright (C) 2021 by Team OpenSPA
# License: GNU General Public License v3.0

import ipaddress
import random

from Components.config import ConfigDirectory, ConfigSelection, ConfigSubsection, config

from . import _
from .CountryCodes import ISO3166
from .Variables import NUMBER_OF_LIVETV_BOUQUETS
from .CockpitTVConfig import setupLocationSlots


X_FORWARD_NETS = {
    "us": "185.236.200.0/24",
    "gb": "185.199.220.0/24",
    "de": "85.214.132.0/24",
    "es": "88.26.241.0/24",
    "ca": "192.206.151.0/24",
    "br": "177.47.27.0/24",
    "mx": "200.68.128.0/24",
    "fr": "176.31.84.0/24",
    "at": "2.18.68.0/24",
    "ch": "5.144.31.0/24",
    "it": "5.133.48.0/24",
    "ar": "104.103.238.0/24",
    "co": "181.204.4.0/24",
    "cr": "138.122.24.0/24",
    "pe": "190.42.0.0/24",
    "ve": "103.83.193.0/24",
    "cl": "161.238.0.0/24",
    "bo": "186.27.64.0/24",
    "sv": "190.53.128.0/24",
    "gt": "190.115.2.0/24",
    "hn": "181.115.0.0/24",
    "ni": "186.76.0.0/24",
    "pa": "168.77.0.0/24",
    "uy": "179.24.0.0/24",
    "ec": "181.196.0.0/24",
    "py": "177.250.0.0/24",
    "do": "152.166.0.0/24",
    "se": "185.39.146.0/24",
    "dk": "80.63.84.0/24",
    "no": "84.214.150.0/24",
    "au": "144.48.37.0/24",
    "fi": "85.194.236.0/24",
}

_forwardIPCache = {}


def pickForwardIP(country):
    """Return an X-Forwarded-For address for *country*, or None if unmapped.

    Picked once per country and cached for the life of the process (not
    re-rolled per call), so a single boot/API session doesn't see its
    apparent origin IP change mid-flight. Restarting the plugin re-rolls
    it, spreading requests across the subnet over time.
    """
    net = X_FORWARD_NETS.get(country)
    if net is None:
        return None
    if country not in _forwardIPCache:
        _forwardIPCache[country] = str(random.choice(list(ipaddress.ip_network(net).hosts())))
    return _forwardIPCache[country]


COUNTRY_NAMES = {cc: country[0].split("(")[0].strip() for country in sorted(ISO3166) if (cc := country[1].lower()) in X_FORWARD_NETS}

TSIDS = {cc: f"{i:X}" for i, cc in enumerate(COUNTRY_NAMES, 1)}


config.plugins.plutotv = ConfigSubsection()
config.plugins.plutotv.country = ConfigSelection(default="local", choices=[("local", _("Local"))] + list(COUNTRY_NAMES.items()))
config.plugins.plutotv.picons = ConfigSelection(default="snp", choices=[("snp", _("service name")), ("srp", _("service reference")), ("", _("None"))])
config.plugins.plutotv.live_tv_mode = ConfigSelection(default="jmp2", choices=[("stitcher", _("Stitcher")), ("jmp2", _("JMP2 proxy")), ("mjh", _("i.mjh.nz"))])
config.plugins.plutotv.config_folder = ConfigDirectory(default="/etc/enigma2")


getselectedcountries = setupLocationSlots(config.plugins.plutotv, "live_tv_country", COUNTRY_NAMES, NUMBER_OF_LIVETV_BOUQUETS, _("None"))
