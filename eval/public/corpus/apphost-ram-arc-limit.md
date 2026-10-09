---
title: apphost RAM upgrade and ZFS ARC limit
slug: apphost-ram-arc-limit
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- zfs
- memory
- apphost
grounding: ok
description: apphost went from 32 GB to 64 GB of RAM in May
---
apphost went from 32 GB to 64 GB of RAM in May. The ZFS ARC is capped at 16 GB with `options zfs zfs_arc_max=17179869184` in /etc/modprobe.d/zfs.conf so containers keep headroom; `arc_summary` shows the hit rate stays above 95%.
