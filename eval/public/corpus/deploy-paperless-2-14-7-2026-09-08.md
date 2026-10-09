---
title: Deploy paperless 2.14.7 to apphost (2026-09-08)
slug: deploy-paperless-2-14-7-2026-09-08
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- paperless
- apphost
grounding: ok
observed_at: '2026-09-08'
volatility: volatile
---
Bumped `ghcr.io/paperless-ngx/paperless-ngx:2.14.7` in /srv/compose/paperless/compose.yaml (was 2.13.5), then `docker compose pull && docker compose up -d`. Health check green after 35 s; one config key renamed, fixed in .env.
