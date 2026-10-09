---
title: Deploy jellyfin 10.9.11 to apphost (2026-01-11)
slug: deploy-jellyfin-10-9-11-2026-01-11
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- jellyfin
- apphost
grounding: ok
observed_at: '2026-01-11'
volatility: volatile
---
Bumped `jellyfin/jellyfin:10.9.11` in /srv/compose/jellyfin/compose.yaml, then `docker compose pull && docker compose up -d`. Health check green after 90 s; dashboards and users intact.
