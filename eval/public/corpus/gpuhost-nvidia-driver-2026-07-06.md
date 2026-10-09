---
title: gpuhost NVIDIA driver status 2026-07-06
slug: gpuhost-nvidia-driver-2026-07-06
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
description: gpuhost runs NVIDIA 550.90.07 with CUDA 12.4
observed_at: '2026-07-06'
volatility: state
---
Checked the GPU stack on gpuhost after a DKMS build warning.

- Driver: NVIDIA 550.90.07 (open kernel modules, DKMS)
- CUDA runtime: 12.4
- Kernel: 6.9.3
- `nvidia-smi` shows the card idling at 15 W, 41 C.

Nothing else changed. llama-server and the immich ML container both came back up without a rebuild. Next check after the next driver release.
