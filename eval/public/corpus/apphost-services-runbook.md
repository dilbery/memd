---
title: apphost services runbook
slug: apphost-services-runbook
profile: amber
host: apphost
importance: 4
superseded_by: null
tags:
- apphost
- runbook
- docker
- services
grounding: ok
description: 'Every container stack on apphost: paths, ports, quirks'
volatility: durable
---
Runbook for every container stack on apphost (10.10.1.10). Each stack follows the compose layout convention. Start/stop order matters only for `db` (first) and `caddy` (last). This note is the umbrella; the per-incident notes hold history.

## caddy

- Compose: `/srv/compose/caddy/compose.yaml`, data `/srv/data/caddy`
- Port: 443
- Restart policy: `unless-stopped`; image pinned by tag, updated by hand
- Backup: included in the nightly restic run

Reverse proxy for every web service; config in `Caddyfile`, reload with `docker exec caddy caddy reload --config /etc/caddy/Caddyfile`.

## grafana

- Compose: `/srv/compose/grafana/compose.yaml`, data `/srv/data/grafana`
- Port: 3000, public name `grafana.home.example.com`
- Restart policy: `unless-stopped`; image pinned by tag, updated by hand
- Backup: included in the nightly restic run

Dashboards are provisioned from `/srv/data/grafana/provisioning`; edits in the UI are lost on restart unless exported.

## prometheus

- Compose: `/srv/compose/prometheus/compose.yaml`, data `/srv/data/prometheus`
- Port: 9090, public name `prom.home.example.com`
- Restart policy: `unless-stopped`; image pinned by tag, updated by hand
- Backup: excluded

Scrape config is templated by ansible; see the monitoring reference for jobs and retention.

## immich

- Compose: `/srv/compose/immich/compose.yaml`, data `/srv/data/immich`
- Port: 2283, public name `photos.home.example.com`
- Restart policy: `unless-stopped`; image pinned by tag, updated by hand
- Backup: included in the nightly restic run

Uploads land in `/srv/data/immich/library`; the database is its own `immich-db` container, not the shared postgres.

## paperless

- Compose: `/srv/compose/paperless/compose.yaml`, data `/srv/data/paperless`
- Port: 8000, public name `docs.home.example.com`
- Restart policy: `unless-stopped`; image pinned by tag, updated by hand
- Backup: included in the nightly restic run

OCR languages are deu+eng (`PAPERLESS_OCR_LANGUAGE=deu+eng`); the consume folder is `/srv/scan/inbox`, fed by the scanner's SMB share.

## jellyfin

- Compose: `/srv/compose/jellyfin/compose.yaml`, data `/srv/data/jellyfin`
- Port: 8096, public name `media.home.example.com`
- Restart policy: `unless-stopped`; image pinned by tag, updated by hand
- Backup: included in the nightly restic run

Hardware transcoding is disabled: apphost exposes no `/dev/dri` to the container, so 4K files are pre-transcoded on gpuhost with ffmpeg.

## vaultwarden

- Compose: `/srv/compose/vaultwarden/compose.yaml`, data `/srv/data/vaultwarden`
- Port: 8081, public name `vault.home.example.com`
- Restart policy: `unless-stopped`; image pinned by tag, updated by hand
- Backup: included in the nightly restic run

WebSocket notifications need the `/notifications/hub` route in the Caddyfile, otherwise clients only sync every few minutes.

## forge

- Compose: `/srv/compose/forge/compose.yaml`, data `/srv/data/forge`
- Port: 3001, public name `git.home.example.com`
- Restart policy: `unless-stopped`; image pinned by tag, updated by hand
- Backup: included in the nightly restic run

LFS objects live in `/srv/forge/lfs`, outside the data volume, and are excluded from restic because they can be re-fetched.

## home-assistant

- Compose: `/srv/compose/home-assistant/compose.yaml`, data `/srv/data/home-assistant`
- Port: 8123, public name `home.home.example.com`
- Restart policy: `unless-stopped`; image pinned by tag, updated by hand
- Backup: included in the nightly restic run

Runs with `network_mode: host` for mDNS discovery; its port is therefore not published through compose.

## adguard

- Compose: `/srv/compose/adguard/compose.yaml`, data `/srv/data/adguard`
- Port: 53, public name `dns.home.example.com`
- Restart policy: `unless-stopped`; image pinned by tag, updated by hand
- Backup: included in the nightly restic run

Admin UI on port 3080 behind caddy; the DNS port binds to 10.10.1.10 only, not 0.0.0.0, so systemd-resolved can keep 127.0.0.53.

## mosquitto

- Compose: `/srv/compose/mosquitto/compose.yaml`, data `/srv/data/mosquitto`
- Port: 1883
- Restart policy: `unless-stopped`; image pinned by tag, updated by hand
- Backup: included in the nightly restic run

Plain MQTT on the LAN and IoT VLAN; no TLS listener yet.

## uptime-kuma

- Compose: `/srv/compose/uptime-kuma/compose.yaml`, data `/srv/data/uptime-kuma`
- Port: 3002, public name `status.home.example.com`
- Restart policy: `unless-stopped`; image pinned by tag, updated by hand
- Backup: included in the nightly restic run

Notifications go to the same ntfy topic as alertmanager.

## ntfy

- Compose: `/srv/compose/ntfy/compose.yaml`, data `/srv/data/ntfy`
- Port: 8090, public name `ntfy.home.example.com`
- Restart policy: `unless-stopped`; image pinned by tag, updated by hand
- Backup: excluded

Topics are protected with access tokens; the phone app subscribes over the tailscale route.

## db

- Compose: `/srv/compose/db/compose.yaml`, data `/srv/data/db`
- Port: 5432
- Restart policy: `unless-stopped`; image pinned by tag, updated by hand
- Backup: included in the nightly restic run

Shared postgres:16 for paperless and the forge; see the dumps note.

