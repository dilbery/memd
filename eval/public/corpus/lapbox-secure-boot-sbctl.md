---
title: Secure Boot on lapbox with sbctl
slug: lapbox-secure-boot-sbctl
profile: amber
host: lapbox
importance: 3
superseded_by: null
tags:
- lapbox
- secureboot
grounding: ok
description: lapbox boots with Secure Boot using our own keys enrolled by sbctl
---
lapbox boots with Secure Boot using our own keys enrolled by sbctl. The pacman hook re-signs kernels, but after any bootloader or kernel change run `sbctl verify`; an unsigned file means the next boot stops at the firmware.
