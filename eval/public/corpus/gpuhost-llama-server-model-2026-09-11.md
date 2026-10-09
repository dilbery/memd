---
title: gpuhost llama-server model 2026-09-11
slug: gpuhost-llama-server-model-2026-09-11
profile: amber
host: gpuhost
importance: 3
superseded_by: null
tags:
- llm
- llama-server
- gpuhost
grounding: ok
observed_at: '2026-09-11'
volatility: state
---
llama-server on gpuhost (port 8080) now serves `coder-30b-a3b-q4_k_m.gguf` with `--ctx-size 65536` and `-ngl 99`. Started by the user unit `llama-server.service`; about 48 tokens/s generation on a short prompt.
