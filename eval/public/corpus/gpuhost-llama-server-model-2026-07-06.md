---
title: gpuhost llama-server model 2026-07-06
slug: gpuhost-llama-server-model-2026-07-06
profile: amber
host: gpuhost
importance: 3
superseded_by: null
tags:
- llm
- llama-server
- gpuhost
grounding: ok
observed_at: '2026-07-06'
volatility: state
---
llama-server on gpuhost (port 8080) now serves `coder-14b-q4_k_m.gguf` with `--ctx-size 16384` and `-ngl 99`. Started by the user unit `llama-server.service`; about 108 tokens/s generation on a short prompt.
