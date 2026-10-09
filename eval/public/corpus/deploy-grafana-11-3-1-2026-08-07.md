---
title: Deploy grafana 11.3.1 to apphost (2026-08-07)
slug: deploy-grafana-11-3-1-2026-08-07
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- deploy
- grafana
- apphost
grounding: ok
observed_at: '2026-08-07'
volatility: volatile
---
Bumped `grafana/grafana:11.3.1` in /srv/compose/grafana/compose.yaml (was 11.2.0), then `docker compose pull && docker compose up -d`. Health check green after 86 s; release notes had no breaking changes.
