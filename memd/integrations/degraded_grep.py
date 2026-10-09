"""Fallback recall when memd is unreachable: keyword grep over a SEPARATE
read-only checkout.

Hard rail: this must NEVER read memd's dedicated write clone (MEMD_CLONE).
A memd crash mid-commit could leave that tree half-written or mid-rebase;
fallback readers must use the agent's own local clone or the shared sync clone.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from memd.store import CARVED_INDEX_FILES

_WORD = re.compile(r"[A-Za-z0-9_./:@-]+")


class MemdCloneForbidden(RuntimeError):
    """Raised if a caller tries to grep memd's dedicated write clone."""


def _split_body(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return text.strip()


def grep_recall(query: str, checkout_path: str, top_n: int = 4) -> list[dict]:
    checkout = Path(checkout_path).expanduser().resolve()

    memd_clone = os.environ.get("MEMD_CLONE")
    if memd_clone:
        forbidden = Path(memd_clone).expanduser().resolve()
        if checkout == forbidden or forbidden in checkout.parents or checkout in forbidden.parents:
            raise MemdCloneForbidden(
                f"degraded grep must not read memd's write clone: {checkout}"
            )

    if not checkout.is_dir():
        return []

    terms = [t.lower() for t in _WORD.findall(query) if t]
    if not terms:
        return []

    scored: list[tuple[int, str, str]] = []
    for f in sorted(checkout.glob("*.md")):
        if f.name in CARVED_INDEX_FILES:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        body = _split_body(text)
        hay = text.lower()
        score = sum(hay.count(t) for t in terms)
        if score > 0:
            scored.append((score, f.stem, body))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"slug": slug, "body": body, "score": score} for score, slug, body in scored[:top_n]]
