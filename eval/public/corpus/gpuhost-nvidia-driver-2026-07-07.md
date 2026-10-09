---
title: gpuhost NVIDIA driver status 2026-07-07
slug: gpuhost-nvidia-driver-2026-07-07
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
description: gpuhost runs NVIDIA 555.42.02 with CUDA 12.5
observed_at: '2026-07-07'
volatility: state
---
Checked the GPU stack on gpuhost after an llama-server crash.

- Driver: NVIDIA 555.42.02 (open kernel modules, DKMS)
- CUDA runtime: 12.5
- Kernel: 6.9.7
- `nvidia-smi` shows the card idling at 26 W, 44 C.

The open kernel module is now the default for this card, no flag needed. llama-server and the immich ML container both came back up without a rebuild. Next check after the next driver release.
