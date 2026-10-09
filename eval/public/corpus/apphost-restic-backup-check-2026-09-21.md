---
title: apphost restic backup check 2026-09-21
slug: apphost-restic-backup-check-2026-09-21
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- backup
- restic
- apphost
grounding: ok
description: restic repo 610 GiB, last snapshot 3b900e4e
observed_at: '2026-09-21'
volatility: state
---
Backup check for apphost.

- Last snapshot: `3b900e4e` at 2026-09-21 03:29
- Repository size: 610 GiB on the NAS share `/mnt/backup/restic`
- Snapshots kept: 29 (keep-daily 7, keep-weekly 4, keep-monthly 12)

`restic check --read-data-subset=5%` found no errors. The nightly `restic-backup.service` run by `restic-backup.timer` finished without warnings.
