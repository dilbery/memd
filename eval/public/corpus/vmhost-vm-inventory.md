---
title: vmhost VM inventory
slug: vmhost-vm-inventory
profile: amber
host: vmhost
importance: 3
superseded_by: null
tags:
- proxmox
- vm
- vmhost
grounding: ok
description: 'Proxmox guests on vmhost: 110 `ci-runner` (Debian 12, 4 vCPU, 8 GB,
  runs the forge actions runner), 120 `sandbox` (throw'
---
Proxmox guests on vmhost: 110 `ci-runner` (Debian 12, 4 vCPU, 8 GB, runs the forge actions runner), 120 `sandbox` (throwaway experiments, snapshots before every change), 130 `windows-test` (off unless needed). Storage is local-lvm; backups via vzdump to the NAS weekly.
