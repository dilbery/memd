---
title: gpuhost NVIDIA driver status 2026-07-24
slug: gpuhost-nvidia-driver-2026-07-24
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
description: gpuhost runs NVIDIA 555.58.02 with CUDA 12.5
observed_at: '2026-07-24'
volatility: state
---
Checked the GPU stack on gpuhost after a driver release note.

- Driver: NVIDIA 555.58.02 (open kernel modules, DKMS)
- CUDA runtime: 12.5
- Kernel: 6.9.9
- `nvidia-smi` shows the card idling at 30 W, 47 C.

Nothing else changed. llama-server and the immich ML container both came back up without a rebuild. Next check after the next driver release.
