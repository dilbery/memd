---
title: Printing from lapbox
slug: lapbox-printing
profile: amber
host: lapbox
importance: 1
superseded_by: null
tags:
- printer
- cups
grounding: ok
description: 'The laser printer is at 10.10.1.40 and speaks IPP Everywhere, so CUPS
  needs no driver: `lpadmin -p laser -E -v ipp://10.'
---
The laser printer is at 10.10.1.40 and speaks IPP Everywhere, so CUPS needs no driver: `lpadmin -p laser -E -v ipp://10.10.1.40/ipp/print -m everywhere`. Duplex default set in the web UI on localhost:631.
