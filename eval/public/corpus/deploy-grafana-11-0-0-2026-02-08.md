---
title: Deploy grafana 11.0.0 to apphost (2026-02-08)
slug: deploy-grafana-11-0-0-2026-02-08
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- grafana
- apphost
grounding: ok
observed_at: '2026-02-08'
volatility: volatile
---
Bumped `grafana/grafana:11.0.0` in /srv/compose/grafana/compose.yaml, then `docker compose pull && docker compose up -d`. Health check green after 33 s; release notes had no breaking changes.
