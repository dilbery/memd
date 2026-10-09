---
title: Immich machine learning runs on gpuhost
slug: immich-ml-on-gpuhost
profile: amber
host: gpuhost
importance: 3
superseded_by: null
tags:
- immich
- gpu
- photos
grounding: ok
description: 'The immich server stays on apphost, but the machine-learning container
  (face detection, CLIP search) runs on gpuhost to '
---
The immich server stays on apphost, but the machine-learning container (face detection, CLIP search) runs on gpuhost to use its GPU: `IMMICH_MACHINE_LEARNING_URL=http://10.10.1.20:3003` in the server's .env. When gpuhost sleeps, smart search falls back to slow CPU or times out; upgrade both containers together.
