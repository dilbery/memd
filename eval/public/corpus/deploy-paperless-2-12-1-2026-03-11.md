---
title: Deploy paperless 2.12.1 to apphost (2026-03-11)
slug: deploy-paperless-2-12-1-2026-03-11
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- paperless
- apphost
grounding: ok
observed_at: '2026-03-11'
volatility: volatile
---
Bumped `ghcr.io/paperless-ngx/paperless-ngx:2.12.1` in /srv/compose/paperless/compose.yaml (was 2.11.6), then `docker compose pull && docker compose up -d`. Health check green after 78 s; database migration ran on start.
