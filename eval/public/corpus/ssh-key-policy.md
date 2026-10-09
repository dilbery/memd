---
title: SSH key policy for lab hosts
slug: ssh-key-policy
profile: amber
host: any
importance: 4
superseded_by: null
tags:
- ssh
- security
- policy
grounding: ok
description: Only ed25519 keys
volatility: durable
---
Only ed25519 keys. `authorized_keys` on every host is managed by the ansible role `base_ssh`; never edit it by hand, it is overwritten on the next run. `PasswordAuthentication no` and `PermitRootLogin prohibit-password` everywhere. New machines get the role applied before they join the inventory.
