"""Date recalled notes ("as of") and flag changeable-state notes past their freshness window.

Dates come from verified_at, observed_at, or the last ISO date in the title/slug;
volatility (durable/state/volatile) is optional note metadata. No verdict is
better than a wrong one, so unlabelled notes get a date and never a warning.
The exception is evidence: a recent failed re-check by mem-verify
(`verification: {status: failed}`, memd.verify) flags a note whatever its
volatility, until a later verified_at clears it.
"""
from __future__ import annotations

import re
import datetime
from typing import Any

VOLATILITIES = ("durable", "state", "volatile")
STALE_AFTER_DAYS = {"state": 30, "volatile": 7}
FAILED_VERIFICATION_DAYS = 30   # a failed re-check older than this no longer labels

_DATE_RE = re.compile(r"(?<!\d)(20\d\d)-(\d\d)-(\d\d)(?!\d)")

_ALIASES = {
    "stable": "durable",
    "permanent": "durable",
    "fact": "durable",
    "current": "state",
    "status": "state",
    "changing": "state",
    "ephemeral": "volatile",
    "temporary": "volatile",
    "temp": "volatile",
    "transient": "volatile",
}


def normalize_volatility(value: Any) -> str | None:
    """Return a canonical volatility string or None."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if not v:
        return None
    if v in VOLATILITIES:
        return v
    return _ALIASES.get(v, None)


def _parse_date(value: Any) -> datetime.date | None:
    """Best-effort parse of a frontmatter date field."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return datetime.date.fromisoformat(s[:10])
        except (ValueError, IndexError):
            return None
    return None


def as_of(note: dict) -> datetime.date | None:
    """Return the most recent usable date from the note, or None."""
    # Try verified_at, then observed_at
    for key in ("verified_at", "observed_at"):
        d = _parse_date(note.get(key))
        if d is not None:
            return d

    # Try title for last ISO date
    title = note.get("title")
    if isinstance(title, str):
        matches = _DATE_RE.findall(title)
        if matches:
            # LAST match
            y, m, d = matches[-1]
            try:
                return datetime.date(int(y), int(m), int(d))
            except (ValueError, TypeError):
                pass

    # Try slug for last ISO date
    slug = note.get("slug")
    if isinstance(slug, str):
        matches = _DATE_RE.findall(slug)
        if matches:
            y, m, d = matches[-1]
            try:
                return datetime.date(int(y), int(m), int(d))
            except (ValueError, TypeError):
                pass

    return None


def failed_verification(note: dict, today: datetime.date) -> dict | None:
    """A recent failed mem-verify check: {"checked_at": date, "probe": str}, else None.

    The marker lives in unknown frontmatter, so it arrives either top-level or
    under the note's ``metadata``. A verified_at after the check supersedes it.
    """
    marker = note.get("verification")
    if not isinstance(marker, dict):
        metadata = note.get("metadata")
        marker = metadata.get("verification") if isinstance(metadata, dict) else None
    if not isinstance(marker, dict) or str(marker.get("status", "")).lower() != "failed":
        return None
    checked = _parse_date(marker.get("checked_at"))
    if checked is None or (today - checked).days > FAILED_VERIFICATION_DAYS:
        return None
    verified = _parse_date(note.get("verified_at"))
    if verified is not None and verified > checked:
        return None
    failed = marker.get("failed")
    probe = failed[0] if isinstance(failed, list) and failed else failed
    probe = " ".join(str(probe or "a declared probe").split())
    if len(probe) > 120:
        probe = probe[:119].rstrip() + "…"
    return {"checked_at": checked, "probe": probe}


def is_stale(note: dict, today: datetime.date) -> bool:
    """Return True if the note is past its staleness threshold or failed a recent re-check."""
    if failed_verification(note, today):
        return True
    vol = normalize_volatility(note.get("volatility"))
    if vol not in STALE_AFTER_DAYS:
        return False
    d = as_of(note)
    if d is None:
        return False
    return (today - d).days > STALE_AFTER_DAYS[vol]


def label(note: dict, today: datetime.date) -> str:
    """Return a suffix to append to a note heading, or empty string."""
    d = as_of(note)
    failed = failed_verification(note, today)
    if failed:
        when = f"as of {d.isoformat()}; " if d else ""
        return (f"  ({when}verification failed {failed['checked_at'].isoformat()}: {failed['probe']}"
                " — may be stale, verify live state before acting on it)")
    if d is None:
        return ""

    vol = normalize_volatility(note.get("volatility"))

    if is_stale(note, today):
        age = (today - d).days
        return f"  (as of {d.isoformat()}, {age} days ago — {vol}; may be stale, verify live state before acting on it)"

    if vol == "durable":
        return f"  (as of {d.isoformat()}, durable)"

    return f"  (as of {d.isoformat()})"


def stale_banner(count: int) -> str:
    """Return a banner warning if count >= 2, else empty string."""
    if count < 2:
        return ""
    return f"⚠ {count} of these notes describe changeable state and are past their freshness window; check the live system before relying on them.\n\n"
