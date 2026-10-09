---
title: Deploy jellyfin 10.10.6 to apphost (2026-08-22)
slug: deploy-jellyfin-10-10-6-2026-08-22
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- jellyfin
- apphost
grounding: ok
observed_at: '2026-08-22'
volatility: volatile
---
Bumped `jellyfin/jellyfin:10.10.6` in /srv/compose/jellyfin/compose.yaml (was 10.10.3), then `docker compose pull && docker compose up -d`. Health check green after 25 s; one config key renamed, fixed in .env.
