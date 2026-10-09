---
title: apphost restic backup check 2026-03-14
slug: apphost-restic-backup-check-2026-03-14
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- backup
- restic
- apphost
grounding: ok
description: restic repo 471 GiB, last snapshot 260843e1
observed_at: '2026-03-14'
volatility: state
---
Backup check for apphost.

- Last snapshot: `260843e1` at 2026-03-14 03:37
- Repository size: 471 GiB on the NAS share `/mnt/backup/restic`
- Snapshots kept: 26 (keep-daily 7, keep-weekly 4, keep-monthly 12)

`restic check --read-data-subset=5%` found no errors. The nightly `restic-backup.service` run by `restic-backup.timer` finished without warnings.
