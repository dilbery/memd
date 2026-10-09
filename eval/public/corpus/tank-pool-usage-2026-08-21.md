---
title: tank pool usage 2026-08-21
slug: tank-pool-usage-2026-08-21
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- zfs
- storage
- capacity
grounding: ok
observed_at: '2026-08-21'
volatility: state
---
`zpool list tank`: 12.8 TiB allocated of 14.5 TiB usable (88%), fragmentation 9%. Largest datasets: tank/media, tank/immich, tank/backup. Above the 80% line where ZFS slows down; plan the next disk purchase.
