---
title: Deploy paperless 2.11.6 to apphost (2026-02-08)
slug: deploy-paperless-2-11-6-2026-02-08
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- paperless
- apphost
grounding: ok
observed_at: '2026-02-08'
volatility: volatile
---
Bumped `ghcr.io/paperless-ngx/paperless-ngx:2.11.6` in /srv/compose/paperless/compose.yaml, then `docker compose pull && docker compose up -d`. Health check green after 53 s; database migration ran on start.
