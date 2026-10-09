---
title: Deploy home-assistant 2026.4.2 to apphost (2026-04-30)
slug: deploy-home-assistant-2026-4-2-2026-04-30
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- home-assistant
- apphost
grounding: ok
observed_at: '2026-04-30'
volatility: volatile
---
Bumped `ghcr.io/home-assistant/home-assistant:2026.4.2` in /srv/compose/home-assistant/compose.yaml (was 2026.3.4), then `docker compose pull && docker compose up -d`. Health check green after 83 s; dashboards and users intact.
