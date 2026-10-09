---
title: Caddy is built with a DNS provider module
slug: caddy-dns-provider-module
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- caddy
- tls
- build
grounding: ok
description: The stock caddy image cannot do DNS-01, so apphost builds its own with
  `xcaddy build --with github.com/caddy-dns/<provid
---
The stock caddy image cannot do DNS-01, so apphost builds its own with `xcaddy build --with github.com/caddy-dns/<provider>` in /srv/compose/caddy/Dockerfile. The DNS provider API token is in /srv/compose/caddy/.env and expires yearly.
