---
title: tank pool usage 2026-08-29
slug: tank-pool-usage-2026-08-29
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- zfs
- storage
- capacity
grounding: ok
observed_at: '2026-08-29'
volatility: state
---
`zpool list tank`: 13.5 TiB allocated of 14.5 TiB usable (93%), fragmentation 7%. Largest datasets: tank/media, tank/immich, tank/backup. Above the 80% line where ZFS slows down; plan the next disk purchase.
