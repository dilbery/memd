---
title: Deploy paperless 2.13.5 to apphost (2026-09-01)
slug: deploy-paperless-2-13-5-2026-09-01
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- paperless
- apphost
grounding: ok
observed_at: '2026-09-01'
volatility: volatile
---
Bumped `ghcr.io/paperless-ngx/paperless-ngx:2.13.5` in /srv/compose/paperless/compose.yaml (was 2.12.1), then `docker compose pull && docker compose up -d`. Health check green after 66 s; dashboards and users intact.
