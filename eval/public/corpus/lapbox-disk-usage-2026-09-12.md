---
title: lapbox disk usage 2026-09-12
slug: lapbox-disk-usage-2026-09-12
profile: amber
host: lapbox
importance: 2
superseded_by: null
tags:
- lapbox
- disk
- storage
grounding: ok
observed_at: '2026-09-12'
volatility: state
---
Root filesystem on lapbox (1 TB NVMe, btrfs) at 80% used, /home subvolume 349 GB.

Getting tight; biggest items are ~/.cache/huggingface and podman images. Snapper keeps 5 hourly and 7 daily snapshots of root.
