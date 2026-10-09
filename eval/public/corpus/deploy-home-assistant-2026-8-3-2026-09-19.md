---
title: Deploy home-assistant 2026.8.3 to apphost (2026-09-19)
slug: deploy-home-assistant-2026-8-3-2026-09-19
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- home-assistant
- apphost
grounding: ok
observed_at: '2026-09-19'
volatility: volatile
---
Bumped `ghcr.io/home-assistant/home-assistant:2026.8.3` in /srv/compose/home-assistant/compose.yaml (was 2026.7.2), then `docker compose pull && docker compose up -d`. Health check green after 90 s; one config key renamed, fixed in .env.
