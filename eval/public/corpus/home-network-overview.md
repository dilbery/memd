---
title: Home network overview
slug: home-network-overview
profile: amber
host: any
importance: 4
superseded_by: null
tags:
- network
- vlan
- wifi
grounding: ok
volatility: durable
---
How the home network is laid out.

## Summary

One router, one 8-port managed switch, two access points. Everything that matters is on VLAN 1; the other VLANs are isolated by the router's firewall.

## VLAN 1 (lab)

10.10.1.0/24, gateway .1. Static: apphost .10, vmhost .11, gpuhost .20, lapbox .30 (DHCP reservation), printer .40. DHCP pool .100-.199.

## VLAN 110 (guests)

10.10.110.0/24, internet only, client isolation on the APs. SSID `hl-guest`, password rotated yearly.

## VLAN 120 (IoT)

10.10.120.0/24. Smart plugs, sensors and the TV. No route to 10.10.1.0/24 except TCP 1883 to apphost (MQTT) and UDP 123 for time. Internet blocked for everything except the TV.

## VLAN 130 (cameras)

10.10.130.0/24, no internet at all. The NVR container pulls RTSP streams from here; cameras cannot initiate connections.

## Switch

Port 1 uplink to router (trunk), 2 apphost, 3 vmhost (trunk for guests), 4 gpuhost, 5-6 APs (trunk), 7 printer, 8 spare. Config backup in the ansible repo.

## Wi-Fi

SSIDs `hl` (VLAN 1), `hl-iot` (VLAN 120, 2.4 GHz only), `hl-guest` (VLAN 110). Roaming works between the two APs with 802.11r off.

## Router

Small x86 router appliance running an open-source firewall distribution. WAN via the ISP's modem in bridge mode, IPv6 prefix delegation /56 with one /64 per VLAN, though only VLAN 1 and 20 get router advertisements. Config backups are exported monthly into the ansible repo.

## Firewall rules

- VLAN 1 -> any: allow
- VLAN 110 -> RFC1918: block, -> internet: allow
- VLAN 120 -> 10.10.1.10:1883/tcp, :123/udp: allow; rest of RFC1918: block
- VLAN 130 -> any: block (the NVR connects in, not out)
- WAN -> any: block except the tailscale UDP port, which is not forwarded because NAT traversal works without it

## DNS and DHCP

The router does DHCP for every VLAN and hands out 10.10.1.10 (AdGuard) as resolver on VLAN 1 and 30; guests get public resolvers.

## Changelog

- 2026-03-14: split IoT devices into VLAN 120
- 2026-04-12: added the camera VLAN
- 2026-06-12: second access point in the garden room
- 2026-06-17: replaced the unmanaged switch
- 2026-07-15: moved DHCP from apphost to the router
- 2026-08-03: 802.11r turned off after roaming problems
- 2026-09-09: guest password rotated
- 2026-09-20: IPv6 on the lab VLAN

