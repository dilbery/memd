---
title: Zigbee coordinator device path
slug: zigbee-coordinator-path
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- zigbee
- iot
- usb
grounding: ok
description: The Zigbee USB coordinator is plugged into apphost (on a USB extension
  cable, away from the USB3 ports' interference)
---
The Zigbee USB coordinator is plugged into apphost (on a USB extension cable, away from the USB3 ports' interference). zigbee2mqtt uses the stable path `/dev/serial/by-id/usb-zigbee-coordinator-if00-port0`, never /dev/ttyUSB0 which changes between boots.
