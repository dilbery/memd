---
title: apphost restic backup check 2026-03-17
slug: apphost-restic-backup-check-2026-03-17
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- backup
- restic
- apphost
grounding: ok
description: restic repo 474 GiB, last snapshot a3535a25
observed_at: '2026-03-17'
volatility: state
---
Backup check for apphost.

- Last snapshot: `a3535a25` at 2026-03-17 03:21
- Repository size: 474 GiB on the NAS share `/mnt/backup/restic`
- Snapshots kept: 29 (keep-daily 7, keep-weekly 4, keep-monthly 12)

`restic check --read-data-subset=5%` found no errors. The nightly `restic-backup.service` run by `restic-backup.timer` finished without warnings.
