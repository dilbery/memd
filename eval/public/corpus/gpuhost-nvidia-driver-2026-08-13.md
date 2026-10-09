---
title: gpuhost NVIDIA driver status 2026-08-13
slug: gpuhost-nvidia-driver-2026-08-13
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
description: gpuhost runs NVIDIA 560.35.03 with CUDA 12.6
observed_at: '2026-08-13'
volatility: state
---
Checked the GPU stack on gpuhost after the weekly update.

- Driver: NVIDIA 560.35.03 (open kernel modules, DKMS)
- CUDA runtime: 12.6
- Kernel: 6.10.10
- `nvidia-smi` shows the card idling at 21 W, 41 C.

The open kernel module is now the default for this card, no flag needed. llama-server and the immich ML container both came back up without a rebuild. Next check after the next driver release.
