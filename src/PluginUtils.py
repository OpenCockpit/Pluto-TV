# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


import os
import re
import subprocess

from Components.PluginComponent import plugins
from Tools.Directories import fileReadLines


COCKPIT_FEED_NAME = "cockpit-all"


WHERE_SEARCH = -99
WHERE_TMDB_SEARCH = -98
WHERE_TMDB_MOVIELIST = -97
WHERE_MEDIATHEK_SEARCH = -96
WHERE_TVMAGAZINE_SEARCH = -95
WHERE_COVER_DOWNLOAD = -94
WHERE_JOBCOCKPIT = -93


def getPlugin(where):
    plugin = None
    plugins_list = plugins.getPlugins(where=where)
    if plugins_list:
        plugin = plugins_list[0]
    return plugin


def _version_sort_key(version):
    version = str(version or "")
    key = []
    for part in re.split(r"([0-9]+)", version):
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return tuple(key)


def _isVersionNewer(candidate_version, current_version):
    if not candidate_version or not current_version:
        return False
    if candidate_version == current_version:
        return False
    return _version_sort_key(candidate_version) > _version_sort_key(current_version)


def _opkgListsDir():
    for line in fileReadLines("/etc/opkg/opkg.conf") or []:
        parts = line.split()
        if len(parts) >= 2 and parts[-2] == "lists_dir":
            return parts[-1]
    return "/var/lib/opkg/lists"


def getInstalledPackageVersion(package_name):
    control_file = os.path.join("/var/lib/opkg/info", f"{package_name}.control")
    if not os.path.exists(control_file):
        return None

    for line in fileReadLines(control_file) or []:
        if line.startswith("Version:"):
            return line.split("Version:", 1)[1].strip().split("+", 1)[0]

    return None


def getAvailablePackageVersion(package_name):
    opkg = "/usr/bin/opkg"
    if not os.path.exists(opkg):
        return None

    try:
        subprocess.run([opkg, "update"], capture_output=True, text=True, check=False)
    except OSError:
        return None

    latest_version = None
    in_package = False

    for line in fileReadLines(os.path.join(_opkgListsDir(), COCKPIT_FEED_NAME)) or []:
        if line.startswith("Package:"):
            in_package = line.split(":", 1)[1].strip() == package_name
        elif in_package and line.startswith("Version:"):
            latest_version = line.split(":", 1)[1].strip()
            in_package = False

    if latest_version is None:
        return None

    return {
        "path": None,
        "version": latest_version,
    }


def checkPluginUpdate(package_name):
    installed_version = getInstalledPackageVersion(package_name)
    available_info = getAvailablePackageVersion(package_name)
    if not available_info:
        return None

    if _isVersionNewer(available_info["version"], installed_version):
        return {
            "package": package_name,
            "installed": installed_version,
            "version": available_info["version"],
            "path": available_info["path"],
        }

    return None
