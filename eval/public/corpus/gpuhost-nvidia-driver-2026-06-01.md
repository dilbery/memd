---
title: gpuhost NVIDIA driver status 2026-06-01
slug: gpuhost-nvidia-driver-2026-06-01
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
description: gpuhost runs NVIDIA 550.78 with CUDA 12.4
observed_at: '2026-06-01'
volatility: state
---
Checked the GPU stack on gpuhost after an llama-server crash.

- Driver: NVIDIA 550.78 (open kernel modules, DKMS)
- CUDA runtime: 12.4
- Kernel: 6.8.12
- `nvidia-smi` shows the card idling at 20 W, 45 C.

Had to reboot twice: the first boot came up on the fallback framebuffer. llama-server and the immich ML container both came back up without a rebuild. Next check after the next driver release.
