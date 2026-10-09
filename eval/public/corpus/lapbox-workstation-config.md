---
title: lapbox workstation config
slug: lapbox-workstation-config
profile: amber
host: lapbox
importance: 3
superseded_by: null
tags:
- lapbox
- dotfiles
- terminal
grounding: ok
volatility: durable
---
How lapbox (the Arch laptop) is set up for daily work.

## Shell

zsh with a small hand-written prompt, no framework. History shared across sessions, 50k lines.

## Packages

Base plus: base-devel, paru for the AUR, podman, uv, neovim, kitty, tmux, ripgrep, fd, bat, jq, sbctl, tlp, syncthing, restic (for the home directory backup to apphost), wireguard-tools, tailscale.

## Power

TLP with the battery charge limit at 80%; `powertop --auto-tune` is not used because it breaks the USB dock's ethernet. Lid close suspends, hibernate is not configured (zram swap only).

## Backups

The home directory goes to the restic repository on apphost's NAS share every evening via a user timer when on the LAN; excludes ~/.cache, ~/Downloads and every .venv.

## tmux

Prefix is `C-a` (not the default C-b). Splits on `|` and `-`, mouse on, `tmux-resurrect` restores sessions after reboot.

## Terminal

kitty. The `ssh` kitten breaks on vmhost's guests because they lack the xterm-kitty terminfo; there, use plain ssh with `TERM=xterm-256color` (an alias `sshx` does this).

## Editor

neovim with lazy.nvim; LSPs via mason: pyright, ruff, gopls, yamlls.

## Git

Commits signed with the ssh key (`gpg.format ssh`), `pull.rebase true`, `rerere.enabled true`.

## Fonts

A monospace nerd font at 11pt; UI font system default.

## Browser

Firefox with a separate profile for the lab admin UIs.

## Dotfiles

Managed with a bare git repo in ~/.dotfiles, pushed to the forge.

## Changelog

- 2025-11-13: moved from bash to zsh
- 2025-11-24: tmux prefix changed to C-a
- 2026-02-16: kitty replaced alacritty
- 2026-04-05: neovim config rewritten for lazy.nvim
- 2026-04-15: commit signing with ssh keys
- 2026-05-08: added the sshx alias
- 2026-08-17: charge limit set to 80%
- 2026-09-11: dotfiles moved to a bare repo

