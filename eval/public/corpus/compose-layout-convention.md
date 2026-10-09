---
title: Compose layout convention on apphost
slug: compose-layout-convention
profile: amber
host: apphost
importance: 4
superseded_by: null
tags:
- docker
- compose
- convention
grounding: ok
description: Every stack lives in `/srv/compose/<name>/compose.yaml` with its `.env`
  next to it (mode 600, not in git) and persistent
volatility: durable
---
Every stack lives in `/srv/compose/<name>/compose.yaml` with its `.env` next to it (mode 600, not in git) and persistent data under `/srv/data/<name>`. Update with `docker compose pull && docker compose up -d` from that directory. Add the data path to the restic include list when creating a new stack.
