---
title: 'Git server: SSH on port 2200'
slug: forge-ssh-port
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- forge
- git
- ssh
grounding: ok
description: The forge runs on apphost; web UI at `https://git.home.example.com` (caddy
  -> port 3000), git over SSH on port 2200 beca
---
The forge runs on apphost; web UI at `https://git.home.example.com` (caddy -> port 3000), git over SSH on port 2200 because 22 is the host's own sshd. Clone with `ssh://git@git.home.example.com:2200/<owner>/<repo>.git`, or add a `Host git.home.example.com` block with `Port 2200` to ~/.ssh/config.
