---
title: Reset the Grafana admin password
slug: grafana-admin-password-reset
profile: amber
host: apphost
importance: 2
superseded_by: null
tags:
- grafana
- howto
grounding: ok
description: '`docker exec -it grafana grafana cli admin reset-admin-password <new>`
  inside the running container'
---
`docker exec -it grafana grafana cli admin reset-admin-password <new>` inside the running container. The login is `admin`; the password is not in the .env because the provisioning only sets it on first start.
