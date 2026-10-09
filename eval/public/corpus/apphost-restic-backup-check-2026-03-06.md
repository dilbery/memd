---
title: apphost restic backup check 2026-03-06
slug: apphost-restic-backup-check-2026-03-06
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- backup
- restic
- apphost
grounding: ok
description: restic repo 452 GiB, last snapshot 2ba5422b
observed_at: '2026-03-06'
volatility: state
---
Backup check for apphost.

- Last snapshot: `2ba5422b` at 2026-03-06 03:21
- Repository size: 452 GiB on the NAS share `/mnt/backup/restic`
- Snapshots kept: 25 (keep-daily 7, keep-weekly 4, keep-monthly 12)

`restic check --read-data-subset=5%` found no errors. The nightly `restic-backup.service` run by `restic-backup.timer` finished without warnings.
