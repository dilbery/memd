---
title: apphost restic backup check 2026-08-27
slug: apphost-restic-backup-check-2026-08-27
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- backup
- restic
- apphost
grounding: ok
description: restic repo 518 GiB, last snapshot 4fdccd3c
observed_at: '2026-08-27'
volatility: state
---
Backup check for apphost.

- Last snapshot: `4fdccd3c` at 2026-08-27 03:39
- Repository size: 518 GiB on the NAS share `/mnt/backup/restic`
- Snapshots kept: 24 (keep-daily 7, keep-weekly 4, keep-monthly 12)

`restic check --read-data-subset=5%` found no errors. The nightly `restic-backup.service` run by `restic-backup.timer` finished without warnings.
