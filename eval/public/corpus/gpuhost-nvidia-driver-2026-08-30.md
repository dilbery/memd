---
title: gpuhost NVIDIA driver status 2026-08-30
slug: gpuhost-nvidia-driver-2026-08-30
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
description: gpuhost runs NVIDIA 565.57.01 with CUDA 12.7
observed_at: '2026-08-30'
volatility: state
---
Checked the GPU stack on gpuhost after an llama-server crash.

- Driver: NVIDIA 565.57.01 (open kernel modules, DKMS)
- CUDA runtime: 12.7
- Kernel: 6.11.2
- `nvidia-smi` shows the card idling at 24 W, 36 C.

The open kernel module is now the default for this card, no flag needed. llama-server and the immich ML container both came back up without a rebuild. Next check after the next driver release.
