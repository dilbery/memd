---
title: Monitoring stack reference
slug: monitoring-stack-reference
profile: amber
host: apphost
importance: 4
superseded_by: null
tags:
- monitoring
- prometheus
- alerting
- loki
grounding: ok
volatility: durable
---
Reference for the monitoring stack. Long-lived; update when jobs or retention change.

## Overview

Prometheus, Alertmanager, Loki and Grafana all run on apphost. Exporters run on every host. Dashboards: node, docker, zfs, gpu, blackbox.

## Prometheus

- scrape interval 30s, evaluation interval 30s
- retention: `--storage.tsdb.retention.time=45d` (raised from 15d in June)
- TSDB on `/srv/data/prometheus`, about 18 GB

## Scrape jobs

- `node`: all four hosts on :9100
- `cadvisor`: apphost :8088
- `nvidia`: gpuhost :9835 (nvidia_gpu_exporter)
- `zfs`: apphost :9134
- `blackbox`: HTTPS probes of every `*.home.example.com` name

## Exporters per host

- apphost: node_exporter, cadvisor, zfs_exporter, smartctl_exporter, promtail
- vmhost: node_exporter, pve_exporter (API token `monitoring@pve!prom`), promtail
- gpuhost: node_exporter, nvidia_gpu_exporter, promtail; scraped with `honor_labels` off and a 2m staleness allowance because it sleeps
- lapbox: node_exporter only (see below)

Exporters are installed by the ansible `monitoring_agent` role; versions are pinned in `group_vars/all/monitoring.yml`.

## Grafana

Anonymous access off, one admin plus a read-only viewer account for the wall tablet. Data sources: Prometheus (default), Loki, and the postgres `db` read-only user for paperless statistics. Plugins are baked into the image, never installed at runtime.

## lapbox exporter

node_exporter on lapbox listens only on its tailscale address `100.64.0.30:9100`, not the LAN IP, because the laptop roams onto untrusted networks. Prometheus scrapes it through the tunnel; a LAN-IP target will always be down.

## Alertmanager

Routes everything to the ntfy topic `homelab-alerts` on `ntfy.home.example.com`. Critical alerts repeat every 1h, warnings 12h. Inhibit rule: HostDown suppresses that host's other alerts.

## Loki

Promtail on each host ships journald and docker logs. Retention 14 days (`retention_period: 336h`), compactor enabled.

## Alert rules

- DiskAlmostFull: >85% for 15m
- ZpoolDegraded: immediate
- BackupTooOld: no restic snapshot in 30h
- CertExpiringSoon: < 14 days
- GpuHot: > 83 C for 10m

## Dashboards

Provisioned JSON in the ansible repo under `roles/grafana/files`. Export from the UI and commit, otherwise changes vanish on redeploy.

## Silences

Planned maintenance: `amtool silence add instance=~"vmhost.*" --duration=2h`. Remove silences when done instead of letting them expire.

## Changelog

- 2025-11-09: added the zfs exporter
- 2025-12-12: moved alerting from e-mail to ntfy
- 2026-04-04: raised prometheus retention
- 2026-06-01: added blackbox probes for every name
- 2026-07-24: restricted the lapbox exporter to tailscale
- 2026-07-29: added GpuHot
- 2026-08-08: deleted the unused snmp job
- 2026-09-10: loki compactor enabled

