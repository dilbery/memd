---
title: ZFS scrub schedule for tank
slug: zfs-scrub-schedule
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- zfs
- storage
- maintenance
grounding: ok
description: Pool `tank` on apphost is raidz1 over three 8 TB disks
---
Pool `tank` on apphost is raidz1 over three 8 TB disks. A scrub runs on the first Sunday of each month via `zfs-scrub-monthly@tank.timer` (from zfsutils). Results land in `zpool status tank`; ZED mails errors to the admin alias. Last scrubs take about 9 hours.
