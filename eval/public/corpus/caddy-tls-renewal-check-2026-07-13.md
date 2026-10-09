---
title: caddy TLS renewal check 2026-07-13
slug: caddy-tls-renewal-check-2026-07-13
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- caddy
- tls
- certificates
grounding: ok
observed_at: '2026-07-13'
volatility: state
---
Caddy on apphost holds the wildcard certificate for `*.home.example.com` (DNS-01 challenge). Current certificate expires 2026-09-28.

Renewal FAILED: `DNS-01 challenge timeout` from the ACME server; the DNS provider API token had expired. Rotated the token in /srv/compose/caddy/.env and restarted caddy, renewal then succeeded. Checked with `curl -vI https://grafana.home.example.com`.
