---
title: DNS resolver config 2026-07-19
slug: dns-resolver-config-2026-07-19
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- dns
- adguard
- network
grounding: ok
observed_at: '2026-07-19'
volatility: state
---
AdGuard Home on apphost (10.10.1.10:53) after timeouts on the IoT VLAN.

Upstream DNS servers (parallel requests):
- `quic://dns3.example.net`
- `tls://dns2.example.net`

DNSSEC validation: enabled. Rewrites for `*.home.example.com` still point at 10.10.1.10; DHCP on the router hands out 10.10.1.10 as the only resolver.
