---
title: Deploy jellyfin 10.10.1 to apphost (2026-06-08)
slug: deploy-jellyfin-10-10-1-2026-06-08
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- jellyfin
- apphost
grounding: ok
observed_at: '2026-06-08'
volatility: volatile
---
Bumped `jellyfin/jellyfin:10.10.1` in /srv/compose/jellyfin/compose.yaml (was 10.9.11), then `docker compose pull && docker compose up -d`. Health check green after 28 s; dashboards and users intact.
