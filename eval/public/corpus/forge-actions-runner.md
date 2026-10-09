---
title: CI runner registration
slug: forge-actions-runner
profile: amber
host: vmhost
importance: 3
superseded_by: null
tags:
- ci
- forge
- runner
grounding: ok
description: The forge actions runner lives in VM 110 (`ci-runner`) on vmhost, registered
  with labels `docker:docker://node:20-bookwo
---
The forge actions runner lives in VM 110 (`ci-runner`) on vmhost, registered with labels `docker:docker://node:20-bookworm` and `ubuntu-latest:docker://node:20-bookworm`. If jobs sit in *queued*, the runner usually lost its token after a forge upgrade: re-register with `forgejo-runner register` using a new token from site administration.
