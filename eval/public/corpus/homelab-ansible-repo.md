---
title: Ansible repo and how to run it
slug: homelab-ansible-repo
profile: amber
host: lapbox
importance: 4
superseded_by: null
tags:
- ansible
- automation
grounding: ok
description: The playbooks live in `~/src/homelab-ansible` on lapbox
---
The playbooks live in `~/src/homelab-ansible` on lapbox. Inventory `hosts.ini` has groups `[docker]` (apphost), `[gpu]` (gpuhost), `[pve]` (vmhost) and `[workstation]` (lapbox). Always run `ansible-playbook site.yml -l <host> --check --diff` first and read the diff; the `docker` role restarts the daemon when daemon.json changes, which bounces every container.
