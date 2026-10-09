---
title: Deploy jellyfin 10.10.3 to apphost (2026-07-09)
slug: deploy-jellyfin-10-10-3-2026-07-09
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- jellyfin
- apphost
grounding: ok
observed_at: '2026-07-09'
volatility: volatile
---
Bumped `jellyfin/jellyfin:10.10.3` in /srv/compose/jellyfin/compose.yaml (was 10.10.1), then `docker compose pull && docker compose up -d`. Health check green after 55 s; database migration ran on start.
