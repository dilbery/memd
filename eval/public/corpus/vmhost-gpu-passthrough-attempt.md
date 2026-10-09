---
title: Tried GPU passthrough on vmhost
slug: vmhost-gpu-passthrough-attempt
profile: amber
host: any
importance: 3
superseded_by: null
tags:
- proxmox
- gpu
grounding: ok
---
Tried passing an old GPU into VM 120 for CUDA tests. IOMMU groups were not clean on this board; gave up and use gpuhost instead.
