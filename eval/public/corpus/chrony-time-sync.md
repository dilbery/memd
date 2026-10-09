---
title: 'Time sync: apphost is the LAN NTP server'
slug: chrony-time-sync
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- ntp
- chrony
- time
grounding: ok
description: All hosts run chrony
---
All hosts run chrony. apphost syncs from public pool servers and serves NTP to 192.168.0.0/16 (`allow 192.168.0.0/16` in chrony.conf); the others use only `server 10.10.1.10 iburst`. The IoT VLAN gets it via DHCP option 42.
