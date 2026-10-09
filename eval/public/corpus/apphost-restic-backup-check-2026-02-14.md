---
title: apphost restic backup check 2026-02-14
slug: apphost-restic-backup-check-2026-02-14
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- backup
- restic
- apphost
grounding: ok
description: restic repo 410 GiB, last snapshot 0b1770fd
observed_at: '2026-02-14'
volatility: state
---
Backup check for apphost.

- Last snapshot: `0b1770fd` at 2026-02-14 03:23
- Repository size: 410 GiB on the NAS share `/mnt/backup/restic`
- Snapshots kept: 24 (keep-daily 7, keep-weekly 4, keep-monthly 12)

`restic check --read-data-subset=5%` found no errors. The nightly `restic-backup.service` run by `restic-backup.timer` finished without warnings.
