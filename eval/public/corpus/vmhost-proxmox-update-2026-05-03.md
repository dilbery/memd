---
title: vmhost Proxmox update 2026-05-03
slug: vmhost-proxmox-update-2026-05-03
profile: amber
host: vmhost
importance: 2
superseded_by: null
tags:
- proxmox
- vmhost
- updates
grounding: ok
observed_at: '2026-05-03'
volatility: state
---
Updated vmhost with `apt full-upgrade` from the no-subscription repo.

- pve-manager 8.2.2
- running kernel 6.8.4-2-pve
- 88 packages upgraded, reboot done

Guests 110 (ci-runner), 120 (sandbox) and 130 (windows-test) came back with their autostart order unchanged.
