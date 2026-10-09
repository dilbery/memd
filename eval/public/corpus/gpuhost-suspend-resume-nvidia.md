---
title: gpuhost black screen after resume from suspend
slug: gpuhost-suspend-resume-nvidia
profile: amber
host: gpuhost
importance: 3
superseded_by: null
tags:
- gpuhost
- nvidia
- suspend
grounding: ok
description: Resuming gpuhost from suspend gave a black screen with the NVIDIA card
---
Resuming gpuhost from suspend gave a black screen with the NVIDIA card. Fix: `options nvidia NVreg_PreserveVideoMemoryAllocations=1` in /etc/modprobe.d/nvidia-power.conf, and enable `nvidia-suspend.service`, `nvidia-resume.service` and `nvidia-hibernate.service`. Rebuild the initramfs afterwards.
