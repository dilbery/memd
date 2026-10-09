---
title: caddy TLS renewal check 2026-05-16
slug: caddy-tls-renewal-check-2026-05-16
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- caddy
- tls
- certificates
grounding: ok
observed_at: '2026-05-16'
volatility: state
---
Caddy on apphost holds the wildcard certificate for `*.home.example.com` (DNS-01 challenge). Current certificate expires 2026-08-08.

Renewal succeeded on the first attempt. Checked with `curl -vI https://grafana.home.example.com`.
