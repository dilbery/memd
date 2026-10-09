---
title: DNS resolver config 2026-04-08
slug: dns-resolver-config-2026-04-08
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- dns
- adguard
- network
grounding: ok
observed_at: '2026-04-08'
volatility: state
---
AdGuard Home on apphost (10.10.1.10:53) after latency tests.

Upstream DNS servers (parallel requests):
- `tls://dns1.example.net`
- `tls://dns2.example.net`

DNSSEC validation: disabled. Rewrites for `*.home.example.com` still point at 10.10.1.10; DHCP on the router hands out 10.10.1.10 as the only resolver.
