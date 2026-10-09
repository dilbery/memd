---
title: Vaultwarden admin token rotation
slug: vaultwarden-admin-token
profile: amber
host: apphost
importance: 4
superseded_by: null
tags:
- vaultwarden
- security
grounding: ok
description: The Vaultwarden admin page token is stored as an argon2 PHC hash in `/srv/compose/vaultwarden/.env`
  as `ADMIN_TOKEN`
---
The Vaultwarden admin page token is stored as an argon2 PHC hash in `/srv/compose/vaultwarden/.env` as `ADMIN_TOKEN`. To rotate: run `docker exec -it vaultwarden /vaultwarden hash`, paste the output into the .env (single-quote it, the `$` signs break compose interpolation otherwise) and `docker compose up -d`. `SIGNUPS_ALLOWED=false`; new family accounts are invited from the admin page.
