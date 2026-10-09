---
title: lapbox system update 2026-09-18
slug: lapbox-system-update-2026-09-18
profile: amber
host: lapbox
importance: 2
superseded_by: null
tags:
- lapbox
- updates
- pacman
grounding: ok
observed_at: '2026-09-18'
volatility: volatile
---
`pacman -Syu` on lapbox: 80 packages upgraded, linux-lts 6.12.44. python rebuild broke two AUR packages; rebuilt with paru. `sbctl verify` clean afterwards.
