---
title: apphost /dev/sdc reallocated sectors rising
slug: apphost-sdc-reallocated-sectors
profile: amber
host: apphost
importance: 4
superseded_by: null
tags:
- disk
- smart
- zfs
- hardware
grounding: ok
description: 'smartd warned about `/dev/sdc` (one of the three `tank` disks): Reallocated_Sector_Ct
  went from 8 to 24 in two weeks, Cu'
---
smartd warned about `/dev/sdc` (one of the three `tank` disks): Reallocated_Sector_Ct went from 8 to 24 in two weeks, Current_Pending_Sector 2. The pool is still ONLINE with no read errors. Replacement 8 TB disk ordered; swap with `zpool replace tank <old-id> <new-id>` and let it resilver.
