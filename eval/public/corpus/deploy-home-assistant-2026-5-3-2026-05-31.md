---
title: Deploy home-assistant 2026.5.3 to apphost (2026-05-31)
slug: deploy-home-assistant-2026-5-3-2026-05-31
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- home-assistant
- apphost
grounding: ok
observed_at: '2026-05-31'
volatility: volatile
---
Bumped `ghcr.io/home-assistant/home-assistant:2026.5.3` in /srv/compose/home-assistant/compose.yaml (was 2026.4.2), then `docker compose pull && docker compose up -d`. Health check green after 84 s; dashboards and users intact.
