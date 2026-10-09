---
title: Syncthing folders between lapbox and gpuhost
slug: syncthing-folders
profile: amber
host: lapbox
importance: 2
superseded_by: null
tags:
- syncthing
- sync
grounding: ok
description: Syncthing syncs `~/notes`, `~/Documents` and `~/src/scratch` between
  lapbox and gpuhost
---
Syncthing syncs `~/notes`, `~/Documents` and `~/src/scratch` between lapbox and gpuhost. `.stignore` excludes `.venv`, `node_modules` and `*.gguf`. Model files are copied by hand because they are too large.
