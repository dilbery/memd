---
title: gpuhost llama-server model 2026-08-18
slug: gpuhost-llama-server-model-2026-08-18
profile: amber
host: gpuhost
importance: 3
superseded_by: null
tags:
- llm
- llama-server
- gpuhost
grounding: ok
observed_at: '2026-08-18'
volatility: state
---
llama-server on gpuhost (port 8080) now serves `chat-12b-q5_k_m.gguf` with `--ctx-size 32768` and `-ngl 99`. Started by the user unit `llama-server.service`; about 68 tokens/s generation on a short prompt.
