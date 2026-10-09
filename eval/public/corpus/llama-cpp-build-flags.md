---
title: Building llama.cpp on gpuhost
slug: llama-cpp-build-flags
profile: amber
host: gpuhost
importance: 3
superseded_by: null
tags:
- llm
- llama.cpp
- cuda
grounding: ok
description: '`cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89 -DGGML_NATIVE=ON
  && cmake --build build -j` in ~/src/llama.'
---
`cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89 -DGGML_NATIVE=ON && cmake --build build -j` in ~/src/llama.cpp. The CUDA toolkit must match the driver's CUDA version or the build links fine but fails at runtime with `CUDA driver version is insufficient`.
