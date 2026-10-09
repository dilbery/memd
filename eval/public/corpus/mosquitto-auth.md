---
title: Mosquitto broker authentication
slug: mosquitto-auth
profile: amber
host: apphost
importance: 3
superseded_by: null
tags:
- mqtt
- mosquitto
- iot
grounding: ok
description: MQTT broker on apphost port 1883
---
MQTT broker on apphost port 1883. `allow_anonymous false`; users in `/srv/compose/mosquitto/config/passwd`, managed with `mosquitto_passwd`. The IoT VLAN may reach only this port. zigbee2mqtt and Home Assistant each have their own user.
