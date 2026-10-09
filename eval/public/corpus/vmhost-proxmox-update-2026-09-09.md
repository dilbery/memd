---
title: vmhost Proxmox update 2026-09-09
slug: vmhost-proxmox-update-2026-09-09
profile: amber
host: vmhost
importance: 2
superseded_by: null
tags:
- proxmox
- vmhost
- updates
grounding: ok
observed_at: '2026-09-09'
volatility: state
---
Updated vmhost with `apt full-upgrade` from the no-subscription repo.

- pve-manager 8.4.1
- running kernel 6.8.12-9-pve
- 62 packages upgraded, reboot needed

Guests 110 (ci-runner), 120 (sandbox) and 130 (windows-test) came back with their autostart order unchanged.
