---
title: gpuhost setup notes
slug: gpuhost-setup-notes
profile: amber
host: gpuhost
importance: 4
superseded_by: null
tags:
- gpuhost
- hardware
- setup
grounding: ok
volatility: durable
---
Setup notes for gpuhost, the GPU workstation.

## Hardware

Desktop tower, 16-core CPU, 96 GB RAM, one NVIDIA GPU with 24 GB, onboard 2.5GbE (eno1) plus a 10GbE PCIe card (enp5s0) to the switch's SFP+ port.

- PSU: 1000 W, 80+ Gold, fully modular
- CPU cooler: 360 mm AIO, pump on the CPU_PUMP header at full speed
- Case: mid tower, three front intakes, one rear and two top exhausts
- Spare slots: one x4 PCIe slot free, two DIMM slots free

## Operating system

Debian testing with backports kernels, installed from the netinst image. Unattended upgrades are off: the NVIDIA DKMS module has to rebuild against every kernel, so updates are done by hand with the driver check afterwards. Packages beyond the base: build-essential, cmake, ccache, ffmpeg, nvtop, btop, podman, git-lfs.

## Networking

Static 10.10.1.20 on enp5s0 via systemd-networkd; eno1 stays up without an address only for wake-on-lan. MTU 9000 on the 10GbE link to match apphost for NFS model copies. Hostname resolution via AdGuard.

## Users

One login user plus a system user `llm` that owns /srv/models and runs the model services. The login user is in the `render` and `video` groups.

## Firmware settings

Resizable BAR on, CSM off, Secure Boot off (NVIDIA DKMS), XMP profile 1, Power On By PCI-E enabled for wake-on-lan.

## Disks

2 TB NVMe for the OS (ext4), 4 TB NVMe for models and datasets mounted at /srv/models. No RAID; models are re-downloadable.

## Kernel parameters

`pcie_aspm=off` on the kernel command line: with ASPM enabled the 10GbE card drops its link every few hours (`enp5s0: Link is Down`). `nvidia-drm.modeset=1` for Wayland.

## GPU power limit

A oneshot unit `gpu-power-limit.service` runs `nvidia-smi -pl 280` at boot: 280 W instead of the default 350 W costs ~4% speed and makes the card much quieter.

## Services

llama-server (user unit), immich machine-learning container, syncthing, node_exporter and nvidia_gpu_exporter.

## Sleep

Suspends after 30 minutes idle unless llama-server has an active request (an inhibitor script checks its /slots endpoint).

## Desktop

KDE Plasma on Wayland, two monitors on DisplayPort.

## Changelog

- 2026-01-07: installed the 10GbE card
- 2026-02-23: moved models to the second NVMe
- 2026-04-06: switched to the open kernel modules
- 2026-04-15: added the sleep inhibitor
- 2026-05-02: enabled resizable BAR
- 2026-06-05: replaced the stock cooler with the AIO
- 2026-08-06: added the GPU power limit unit
- 2026-08-18: turned off ASPM

