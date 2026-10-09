---
title: apphost restic backup check 2026-09-15
slug: apphost-restic-backup-check-2026-09-15
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- backup
- restic
- apphost
grounding: ok
description: restic repo 596 GiB, last snapshot c5e928c9
observed_at: '2026-09-15'
volatility: state
---
Backup check for apphost.

- Last snapshot: `c5e928c9` at 2026-09-15 03:17
- Repository size: 596 GiB on the NAS share `/mnt/backup/restic`
- Snapshots kept: 24 (keep-daily 7, keep-weekly 4, keep-monthly 12)

`restic check --read-data-subset=5%` found no errors. The nightly `restic-backup.service` run by `restic-backup.timer` finished without warnings.
