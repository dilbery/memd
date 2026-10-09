---
title: apphost restic backup check 2026-08-16
slug: apphost-restic-backup-check-2026-08-16
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- backup
- restic
- apphost
grounding: ok
description: restic repo 501 GiB, last snapshot 886706d8
observed_at: '2026-08-16'
volatility: state
---
Backup check for apphost.

- Last snapshot: `886706d8` at 2026-08-16 03:19
- Repository size: 501 GiB on the NAS share `/mnt/backup/restic`
- Snapshots kept: 24 (keep-daily 7, keep-weekly 4, keep-monthly 12)

`restic check` reported `error: pack 0c076c3fdb29 damaged`. Ran `restic repair packs` and `restic repair snapshots --forget`, then a clean check. The nightly `restic-backup.service` run by `restic-backup.timer` finished without warnings.
