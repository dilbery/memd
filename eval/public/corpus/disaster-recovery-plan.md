---
title: Disaster recovery plan
slug: disaster-recovery-plan
profile: amber
host: any
importance: 5
superseded_by: null
tags:
- backup
- disaster-recovery
- restic
grounding: ok
volatility: durable
---
Disaster recovery plan for the lab.

## Scope

Losing apphost (disk failure, fire, theft) is the scenario; the other hosts are rebuildable from ansible and hold nothing unique.

## What is backed up

restic nightly: /srv/data, /srv/compose, /srv/backup/pg, /etc. Not backed up: /srv/media (re-rippable), prometheus TSDB.

## Where

Primary repo on the NAS share `/mnt/backup/restic`; weekly offsite copy by rclone. See the offsite note for the bucket.

## Hardware spares

A cold-spare 8 TB disk for tank (once the replacement arrives), a spare 1 TB SSD for the OS, and the previous mini PC in the cupboard that can run the core stacks temporarily with 16 GB of RAM.

## Accounts needed

The DNS provider account (for the wildcard certificate), the object storage account (offsite copy), the tailscale admin console, and the domain registrar. Recovery codes for all four are in the password manager and printed in the fire safe.

## Rebuild time

Measured estimate: 2 hours for OS, pool and base stacks from ansible; restores are limited by the NAS read speed (about 180 MB/s), so a full restore of /srv/data is roughly 1.5 hours more.

## Secrets

The restic repository password is in `/root/.config/restic/pw` on apphost (mode 400) and on paper in the fire safe. Without it the backups are useless; the ansible vault has a third copy.

## Restore order

1. Base OS and ZFS pool via ansible. 2. `db` and restore the latest pg dump. 3. AdGuard, so names resolve. 4. caddy. 5. Everything else, most important first: vaultwarden, paperless, home-assistant.

## Restore test

Quarterly: restore one stack into vmhost's sandbox VM and click through it. Record the date in this note.

## Last test

Restored paperless into VM 120 on 2026-07-12: 38 minutes end to end, documents and tags intact.

## Changelog

- 2025-11-30: plan written after the borg USB disk died
- 2026-01-01: offsite copy added
- 2026-02-02: restore order revised: DNS before caddy
- 2026-08-10: first quarterly restore test
- 2026-08-12: paper copy of the repository password added
- 2026-09-05: mini PC kept as a spare

