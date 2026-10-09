---
title: gpuhost fan curve and noise
slug: gpuhost-fan-curve
profile: amber
host: gpuhost
importance: 2
superseded_by: null
tags:
- gpuhost
- fans
- noise
grounding: ok
description: gpuhost was loud under sustained GPU load
---
gpuhost was loud under sustained GPU load. Case fans follow `fancontrol` (/etc/fancontrol, hwmon of the motherboard's nct6799 chip) with a flat curve up to 60 C. GPU fans are left on the vendor curve; the power limit does more for noise than any curve.
