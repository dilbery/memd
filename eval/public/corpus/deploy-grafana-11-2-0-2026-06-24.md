---
title: Deploy grafana 11.2.0 to apphost (2026-06-24)
slug: deploy-grafana-11-2-0-2026-06-24
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- grafana
- apphost
grounding: ok
observed_at: '2026-06-24'
volatility: volatile
---
Bumped `grafana/grafana:11.2.0` in /srv/compose/grafana/compose.yaml (was 11.1.4), then `docker compose pull && docker compose up -d`. Health check green after 53 s; release notes had no breaking changes.
