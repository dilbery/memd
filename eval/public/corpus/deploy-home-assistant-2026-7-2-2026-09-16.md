---
title: Deploy home-assistant 2026.7.2 to apphost (2026-09-16)
slug: deploy-home-assistant-2026-7-2-2026-09-16
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- home-assistant
- apphost
grounding: ok
observed_at: '2026-09-16'
volatility: volatile
---
Bumped `ghcr.io/home-assistant/home-assistant:2026.7.2` in /srv/compose/home-assistant/compose.yaml (was 2026.6.1), then `docker compose pull && docker compose up -d`. Health check green after 83 s; database migration ran on start.
