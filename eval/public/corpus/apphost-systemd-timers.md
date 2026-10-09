---
title: Scheduled jobs on apphost are systemd timers
slug: apphost-systemd-timers
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- systemd
- scheduling
grounding: ok
description: 'All scheduled jobs on apphost moved from root''s crontab to systemd
  timers in February: restic-backup, pg-dump, rclone-of'
---
All scheduled jobs on apphost moved from root's crontab to systemd timers in February: restic-backup, pg-dump, rclone-offsite, zfs scrub and docker image prune. List them with `systemctl list-timers`; units live in /etc/systemd/system and are deployed by ansible. The crontab is empty on purpose.
