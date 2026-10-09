---
title: Docker log rotation on apphost
slug: docker-log-rotation
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- docker
- logging
- disk
grounding: ok
description: /var/lib/docker filled the root filesystem in March because containers
  logged with the unbounded json-file driver
---
/var/lib/docker filled the root filesystem in March because containers logged with the unbounded json-file driver. `/etc/docker/daemon.json` now sets `"log-driver": "local"` with `"max-size": "20m"` and `"max-file": "5"`. Existing containers only pick it up when recreated (`docker compose up -d --force-recreate`).
