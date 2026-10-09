"""Host-aware advisory grounding: extract candidate commands and (Task 6) check them."""
from __future__ import annotations

import re

# backticked single tokens: `rg`, `jq`, `git`
_BACKTICK = re.compile(r"`([a-zA-Z][\w.-]*)`")
# contents of a fenced shell block (very common doc shape)
_FENCE = re.compile(r"```(?:bash|sh|shell|console)?\n(.*?)```", re.DOTALL)

# words that look like commands but are never local executables we should check.
# `ssh` is here because "ssh svcuser@host ..." is a remote-access verb, not a
# local-tool claim — checking it would false-flag every remote note.
_STOPWORDS = {"http", "https", "ssh", "cd", "the", "and", "see", "note"}


def extract_commands(body: str) -> list[str]:
    """Best-effort list of bare command names referenced in a note body.

    Conservative: backticked single tokens + the first token of fenced shell
    lines. URLs, paths, IPs, and obvious noise are excluded.
    """
    found: list[str] = []

    def _add(tok: str) -> None:
        tok = tok.strip()
        if not tok or tok in _STOPWORDS:
            return
        if "/" in tok or ":" in tok or tok.replace(".", "").isdigit():
            return
        if "." in tok:  # path-ish like state.db
            return
        if tok not in found:
            found.append(tok)

    for m in _BACKTICK.finditer(body):
        _add(m.group(1))

    for block in _FENCE.findall(body):
        for line in block.splitlines():
            line = line.strip().lstrip("$ ").strip()
            if not line or line.startswith("#"):
                continue
            _add(line.split()[0])

    return found


import os
import shutil
import socket

from memd.config import host_name


def local_host_checker(kind: str, target: str) -> bool:
    """Real local existence check. kind in {'command','path','tcp'}."""
    if kind == "command":
        return shutil.which(target) is not None
    if kind == "path":
        return os.path.exists(target)
    if kind == "tcp":
        host, _, port = target.partition(":")
        try:
            with socket.create_connection((host, int(port)), timeout=0.5):
                return True
        except OSError:
            return False
    return False


def ground(note, *, host_checker=local_host_checker) -> str:
    """Host-aware ADVISORY grounding. Returns a `grounding` enum value; never raises.

    host == gpuhost -> check referenced commands locally:
        all present -> 'ok'; any missing -> 'unverified-local'
    host != gpuhost -> 'unverified-remote' (NO local check is consulted at all)
    """
    if note.host != host_name("gpuhost"):
        return "unverified-remote"

    commands = extract_commands(note.body)
    if not commands:
        return "ok"
    for cmd in commands:
        if not host_checker("command", cmd):
            return "unverified-local"
    return "ok"
