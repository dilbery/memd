---
title: Stale file handle on vmhost /mnt/media after apphost reboot
slug: vmhost-nfs-stale-file-handle
profile: amber
host: vmhost
importance: 3
superseded_by: null
tags:
- nfs
- storage
grounding: ok
description: vmhost mounts `apphost:/srv/media` over NFSv4 at /mnt/media
---
vmhost mounts `apphost:/srv/media` over NFSv4 at /mnt/media. After apphost reboots, every access fails with `Stale file handle`. Fix: the fstab entry now uses `soft,timeo=150,x-systemd.automount,x-systemd.idle-timeout=600` so the automount unit remounts on next access. Manual recovery: `umount -l /mnt/media && mount /mnt/media`.
