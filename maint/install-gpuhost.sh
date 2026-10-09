#!/bin/sh
# Install/refresh the memd-maint units on this machine (user scope).
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.config/systemd/user"
cp "$HERE/systemd/memd-maint.service" "$HERE/systemd/memd-maint.timer" \
   "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now memd-maint.timer
systemctl --user list-timers memd-maint.timer --no-pager
