---
title: lapbox zram swap
slug: lapbox-zram-swap
profile: amber
host: lapbox
importance: 1
superseded_by: null
tags:
- lapbox
- memory
- swap
grounding: ok
description: 'No swap partition on lapbox; `zram-generator` provides 8 GB of zstd-compressed
  swap (`/etc/systemd/zram-generator.conf`:'
---
No swap partition on lapbox; `zram-generator` provides 8 GB of zstd-compressed swap (`/etc/systemd/zram-generator.conf`: `zram-size = min(ram / 2, 8192)`).
