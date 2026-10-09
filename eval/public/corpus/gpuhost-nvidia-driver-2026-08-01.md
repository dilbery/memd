---
title: gpuhost NVIDIA driver status 2026-08-01
slug: gpuhost-nvidia-driver-2026-08-01
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
description: gpuhost runs NVIDIA 560.28.03 with CUDA 12.6
observed_at: '2026-08-01'
volatility: state
---
Checked the GPU stack on gpuhost after the weekly update.

- Driver: NVIDIA 560.28.03 (open kernel modules, DKMS)
- CUDA runtime: 12.6
- Kernel: 6.10.4
- `nvidia-smi` shows the card idling at 20 W, 38 C.

The open kernel module is now the default for this card, no flag needed. llama-server and the immich ML container both came back up without a rebuild. Next check after the next driver release.
