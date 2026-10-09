---
title: lapbox disk usage 2026-07-26
slug: lapbox-disk-usage-2026-07-26
profile: amber
host: lapbox
importance: 2
superseded_by: null
tags:
- lapbox
- disk
- storage
grounding: ok
observed_at: '2026-07-26'
volatility: state
---
Root filesystem on lapbox (1 TB NVMe, btrfs) at 63% used, /home subvolume 377 GB.

Cleared the pacman cache with `paccache -rk1` and old container images; down from 91%. Snapper keeps 5 hourly and 7 daily snapshots of root.
