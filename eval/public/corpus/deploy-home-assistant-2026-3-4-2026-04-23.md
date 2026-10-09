---
title: Deploy home-assistant 2026.3.4 to apphost (2026-04-23)
slug: deploy-home-assistant-2026-3-4-2026-04-23
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- home-assistant
- apphost
grounding: ok
observed_at: '2026-04-23'
volatility: volatile
---
Bumped `ghcr.io/home-assistant/home-assistant:2026.3.4` in /srv/compose/home-assistant/compose.yaml, then `docker compose pull && docker compose up -d`. Health check green after 22 s; dashboards and users intact.
