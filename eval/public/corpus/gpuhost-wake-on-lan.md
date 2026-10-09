---
title: Wake gpuhost with wake-on-lan
slug: gpuhost-wake-on-lan
profile: amber
host: gpuhost
importance: 3
superseded_by: null
tags:
- gpuhost
- wol
- power
grounding: ok
description: gpuhost sleeps when idle
---
gpuhost sleeps when idle. Wake it from apphost with `wakeonlan 02:00:00:00:00:20` (onboard NIC, not the 10GbE card). Needs "Power On By PCI-E" enabled in firmware and `ethtool -s eno1 wol g`, persisted by a systemd-networkd `.link` file.
