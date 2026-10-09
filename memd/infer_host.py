"""Backfill the host: field by inferring scope from note content.

Precedence: vmhost > lxc > lapbox > gpuhost > any. A note matches a host when
it mentions the host's placeholder role, its MEMD_HOST_NAMES name, or one of its
MEMD_HOST_SIGNALS phrases, e.g. '{"vmhost": ["10.10.1.11"], "gpuhost": ["igpu"]}'.
An already-set, non-'any' host on the note is respected (not overwritten).
"""
from __future__ import annotations

import json
import logging
import os

from memd.config import host_name

log = logging.getLogger(__name__)

_PRECEDENCE = ("vmhost", "lxc", "lapbox", "gpuhost")


def _extra_signals() -> dict[str, list[str]]:
    raw = os.environ.get("MEMD_HOST_SIGNALS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("MEMD_HOST_SIGNALS is not valid JSON; ignoring it")
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(role).casefold(): [str(s).casefold() for s in phrases if str(s).strip()]
            for role, phrases in data.items() if isinstance(phrases, list)}


def infer_host(note) -> str:
    if getattr(note, "host", "any") not in ("any", "", None):
        return note.host

    hay = f"{note.title}\n{note.body}".lower()
    extra = _extra_signals()
    for role in _PRECEDENCE:
        needles = (role, host_name(role), *extra.get(role, ()))
        if any(n in hay for n in needles):
            return host_name(role)
    return "any"
