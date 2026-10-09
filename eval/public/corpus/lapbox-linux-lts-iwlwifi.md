---
title: lapbox runs linux-lts because of iwlwifi crashes
slug: lapbox-linux-lts-iwlwifi
profile: amber
host: lapbox
importance: 3
superseded_by: null
tags:
- lapbox
- kernel
- wifi
grounding: ok
description: 'The mainline kernel crashed the Wi-Fi firmware on lapbox (`iwlwifi:
  Failed to run INIT ucode: -110`) after resume'
---
The mainline kernel crashed the Wi-Fi firmware on lapbox (`iwlwifi: Failed to run INIT ucode: -110`) after resume. Switched to linux-lts, which does not. Keep linux-lts as the default boot entry until mainline has been tested again.
