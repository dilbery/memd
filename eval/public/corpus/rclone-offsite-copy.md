---
title: Offsite copy of the restic repository
slug: rclone-offsite-copy
profile: amber
host: apphost
importance: 4
superseded_by: null
tags:
- backup
- rclone
- offsite
grounding: ok
description: Every Sunday night `rclone-offsite.timer` runs `rclone sync /mnt/backup/restic
  offsite:homelab-offsite --bwlimit 4M --fa
---
Every Sunday night `rclone-offsite.timer` runs `rclone sync /mnt/backup/restic offsite:homelab-offsite --bwlimit 4M --fast-list` to object storage. The bucket has versioning and a 30-day object lock, so a ransomware sync cannot destroy older copies.
