---
title: Shared Postgres and nightly dumps
slug: postgres-shared-dumps
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- postgres
- backup
- database
grounding: ok
description: One `postgres:16` container named `db` on apphost serves paperless and
  the forge
---
One `postgres:16` container named `db` on apphost serves paperless and the forge. `pg-dump.timer` runs `pg_dumpall` at 02:30 into /srv/backup/pg (keep 7), which restic picks up at 03:00. Restic never reads the live data directory.
