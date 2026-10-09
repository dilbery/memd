---
title: Docker publishes ports past the firewall
slug: docker-firewall-docker-user
profile: amber
host: apphost
importance: 4
superseded_by: null
tags:
- firewall
- docker
- nftables
grounding: ok
description: 'Published container ports on apphost were reachable from the guest VLAN
  although the nftables input chain drops them: do'
---
Published container ports on apphost were reachable from the guest VLAN although the nftables input chain drops them: docker inserts its own forward rules. Filter in the `DOCKER-USER` chain instead (allow 10.10.1.0/24 and the tailscale range, drop the rest), or publish on 127.0.0.1 and let caddy proxy.
