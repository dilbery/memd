---
name: project-aur-supply-chain-defense
description: AUR supply-chain defense on gpuhost — pkgaudit + paru hardening; there is NO parux
type: project
metadata:
  node_type: memory
  source: hermes
---
# AUR supply-chain defense (gpuhost)

On gpuhost the AUR install path is hardened. The audit tool is `pkgaudit` at
~/.local/bin/pkgaudit, and AUR builds go through `paru`. NOTE: there is NO `parux`
binary — that name is a hallucination; the real tools are `pkgaudit` and `paru`.
Run `pkgaudit` before every `paru` build to diff the PKGBUILD against the known
hash and catch malicious upstream changes on this Arch box.
