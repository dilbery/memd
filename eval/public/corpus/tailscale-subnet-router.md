---
title: Remote access through the tailscale subnet router
slug: tailscale-subnet-router
profile: amber
host: apphost
importance: 4
superseded_by: null
tags:
- tailscale
- vpn
- remote
grounding: ok
description: 'apphost is the tailscale subnet router: `tailscale up --advertise-routes=10.10.1.0/24
  --accept-dns=false`, route approve'
---
apphost is the tailscale subnet router: `tailscale up --advertise-routes=10.10.1.0/24 --accept-dns=false`, route approved in the admin console. From outside the house every 10.10.1.x address works directly; MagicDNS names are disabled so `*.home.example.com` still resolves through AdGuard over the tunnel.
