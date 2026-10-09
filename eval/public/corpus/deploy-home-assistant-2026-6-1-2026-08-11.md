---
title: Deploy home-assistant 2026.6.1 to apphost (2026-08-11)
slug: deploy-home-assistant-2026-6-1-2026-08-11
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- home-assistant
- apphost
grounding: ok
observed_at: '2026-08-11'
volatility: volatile
---
Bumped `ghcr.io/home-assistant/home-assistant:2026.6.1` in /srv/compose/home-assistant/compose.yaml (was 2026.5.3), then `docker compose pull && docker compose up -d`. Health check green after 94 s; database migration ran on start.
