---
title: SSH to vmhost guests through a jump host
slug: ssh-proxyjump-config
profile: amber
host: lapbox
importance: 2
superseded_by: null
tags:
- ssh
- lapbox
grounding: ok
description: The Proxmox guests are on an internal bridge
---
The Proxmox guests are on an internal bridge. From lapbox, `~/.ssh/config` has `Host ci-runner sandbox` with `ProxyJump vmhost`, so `ssh sandbox` works from anywhere on the LAN or tailscale.
