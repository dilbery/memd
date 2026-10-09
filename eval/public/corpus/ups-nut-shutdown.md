---
title: UPS and NUT shutdown sequence
slug: ups-nut-shutdown
profile: amber
host: apphost
importance: 4
superseded_by: null
tags:
- ups
- power
- nut
grounding: ok
description: The UPS is attached to apphost by USB and monitored by NUT (`upsmon`
  as primary)
---
The UPS is attached to apphost by USB and monitored by NUT (`upsmon` as primary). vmhost and gpuhost run `upsmon` as secondaries against apphost on port 3493. When battery.charge drops below 30% the secondaries shut down first, apphost last. All three have "restore on AC power loss" set to *last state* in firmware; gpuhost stays off and is woken later with wake-on-lan.
