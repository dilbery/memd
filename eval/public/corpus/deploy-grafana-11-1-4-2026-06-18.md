---
title: Deploy grafana 11.1.4 to apphost (2026-06-18)
slug: deploy-grafana-11-1-4-2026-06-18
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- grafana
- apphost
grounding: ok
observed_at: '2026-06-18'
volatility: volatile
---
Bumped `grafana/grafana:11.1.4` in /srv/compose/grafana/compose.yaml (was 11.1.0), then `docker compose pull && docker compose up -d`. Health check green after 91 s; database migration ran on start.
