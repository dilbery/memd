---
title: gpuhost NVIDIA driver status 2026-03-24
slug: gpuhost-nvidia-driver-2026-03-24
profile: amber
host: gpuhost
importance: 3
superseded_by: null
tags:
- gpu
- nvidia
- gpuhost
- drivers
grounding: ok
description: gpuhost runs NVIDIA 550.54.14 with CUDA 12.4
observed_at: '2026-03-24'
volatility: state
---
Checked the GPU stack on gpuhost after the weekly update.

- Driver: NVIDIA 550.54.14 (open kernel modules, DKMS)
- CUDA runtime: 12.4
- Kernel: 6.8.9
- `nvidia-smi` shows the card idling at 20 W, 35 C.

Persistence mode is still enabled through nvidia-persistenced. llama-server and the immich ML container both came back up without a rebuild. Next check after the next driver release.
