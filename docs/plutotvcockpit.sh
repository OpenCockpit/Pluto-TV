#!/bin/sh
echo src/gz cockpit-all https://xcentaurix.github.io/Cockpit-Feed/packages/all > /etc/opkg/cockpit-feed-all.conf
opkg update
opkg install enigma2-plugin-extensions-plutotvcockpit
init 4
init 3
