"""Map loose caller payloads onto memd's exact note shape.

memd's MCP/REST surface advertises `{title, body}`, but the models calling it do
not reliably produce those names. Observed in production, every one of these
was rejected and the note lost for good:
`{"slug":..., "content":...}`, `{"content":...}`, `{"slug":..., "text":...}`,
and `{"body":...}` with no title at all. The rejection happened in the CLIENT's
schema validator, so the payload never reached the server and nothing was logged
anywhere -- the durable fact simply evaporated.

The rule this module enforces: a note is never lost to a field-name guess. Any
payload carrying text under a recognisable alias becomes a valid note, a missing
title is derived from the body rather than being fatal, and only a payload with
no usable text at all raises -- with a message naming the keys it actually got,
so the caller can correct itself instead of retrying the same shape.
"""
from __future__ import annotations

import re
from typing import Any

from memd.staleness import normalize_volatility
from memd.slug import validate_slug

_TITLE_RE = re.compile(r"^#{1,6}\s+")
_LIST_RE = re.compile(r"^[-*+]\s+")
_BOLD_ITALIC_LEAD = re.compile(r"^[*_]+")
_BOLD_ITALIC_TRAIL = re.compile(r"[*_]+$")


class NormalizeError(ValueError):
    """No usable content, or an unsafe/ambiguous mutation control was supplied."""


TITLE_KEYS: tuple[str, ...] = ("title", "name", "heading", "subject", "slug")
BODY_KEYS: tuple[str, ...] = ("body", "content", "text", "fact", "note", "value")
QUERY_KEYS: tuple[str, ...] = ("query", "q", "question", "topic", "search", "text")

# slug is last in TITLE_KEYS so a real title wins when both are present.
# The two key sets are disjoint by construction, so no key can be claimed as both title and body.


def _first_string(keys: tuple[str, ...], payload: dict) -> str | None:
    """Return the first stripped string value found in `payload` under any of `keys`.

    Walks keys in order. If none yield a non-empty stripped string, returns None.
    Ensures consistent handling of whitespace across loose input formats.
    """
    for key in keys:
        if key not in payload:
            continue
        val = payload[key]
        if isinstance(val, str):
            stripped = val.strip()
            if stripped:
                return stripped
    return None


def derive_title(body: str) -> str:
    """Derive a clean title from note body text.

    Handles markdown formatting, list markers, wiki links, and excessive length.
    Never raises; always returns a non-empty string to prevent empty titles in storage.
    """
    if not body or not body.strip():
        return "untitled note"

    lines = body.splitlines()
    first_line: str | None = None
    for line in lines:
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break

    if first_line is None:
        return "untitled note"

    result = _TITLE_RE.sub("", first_line)
    result = _LIST_RE.sub("", result)
    result = _BOLD_ITALIC_LEAD.sub("", result)
    result = _BOLD_ITALIC_TRAIL.sub("", result)
    result = result.strip()

    if result.startswith("[[") and result.endswith("]]"):
        inner = result[2:-2]
        result = inner.strip()

    if len(result) > 80:
        head = result[:80]
        space_idx = head.rfind(" ")
        if space_idx == -1:
            result = head
        else:
            result = head[:space_idx]

    while result and result[-1] in ".,;:-":
        result = result[:-1]

    if not result:
        return "untitled note"

    return result


def _optional_bool(value):
    """Parse callers' booleans without treating the string 'false' as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "1", "on"):
            return True
        if text in ("false", "no", "0", "off"):
            return False
    return None


def normalize_fact(payload: dict) -> dict:
    """Normalize a loose payload into the strict memd fact shape.

    Maps various field names for title and body, derives titles from content when missing,
    validates optional fields (host, profile, conflict, importance, tags), and drops unknowns.
    Prevents data loss by accepting flexible input formats while enforcing internal consistency.
    """
    body = _first_string(BODY_KEYS, payload)
    title = _first_string(TITLE_KEYS, payload)

    if body is not None and title is not None:
        pass  # use as-is
    elif body is not None:
        title = derive_title(body)
    elif title is not None:
        body = title
    else:
        keys_str = ", ".join(sorted(payload)) or "(none)"
        raise NormalizeError(
            "no note content: expected a body under one of body/content/text "
            f"(and optionally a title); got keys: {keys_str}"
        )

    out: dict[str, Any] = {"title": title, "body": body}

    for opt_key in ("host", "profile", "source", "observed_at", "verified_at",
                    "description", "expected_revision"):
        if opt_key in payload and isinstance(payload[opt_key], str):
            stripped_val = payload[opt_key].strip()
            if stripped_val:
                out[opt_key] = stripped_val

    # An unrecognised volatility is dropped, never an error: no verdict is
    # better than a wrong one.
    volatility = normalize_volatility(payload.get("volatility"))
    if volatility:
        out["volatility"] = volatility

    # Probes are declared, never guessed: an invalid one is an error the caller
    # can correct, not something to drop silently. null or [] clears them.
    if "verify" in payload:
        from memd.verify import ProbeError, canonical
        try:
            out["verify"] = canonical(payload["verify"])
        except ProbeError as exc:
            raise NormalizeError(f"verify: {exc}") from exc

    for key in ("slug", "supersedes"):
        if key in payload:
            try:
                out[key] = validate_slug(payload[key].strip() if isinstance(payload[key], str)
                                         else payload[key])
            except ValueError as exc:
                raise NormalizeError(f"{key}: {exc}") from exc

    if "expected_revision" in payload and "expected_revision" not in out:
        raise NormalizeError("expected_revision must be a nonempty revision string")

    for key in ("conflict", "pinned"):
        if key in payload:
            value = _optional_bool(payload[key])
            if value is None:
                raise NormalizeError(f"{key} must be true or false")
            out[key] = value

    if "importance" in payload:
        try:
            n = int(float(payload["importance"]))
        except (TypeError, ValueError, OverflowError):
            pass  # omit key
        else:
            clamped = max(1, min(5, n))
            out["importance"] = clamped

    if "tags" in payload:
        raw_tags = payload["tags"]
        tag_list: list[str] | None = None
        if isinstance(raw_tags, (list, tuple)):
            tag_list = [str(t).strip() for t in raw_tags if str(t).strip()]
        elif isinstance(raw_tags, str):
            parts = [p.strip() for p in raw_tags.split(",")]
            tag_list = [p for p in parts if p]

        if tag_list is not None:
            out["tags"] = tag_list

    return out


def normalize_recall_args(payload: dict) -> dict:
    """Normalize arguments for a recall/search operation.

    Extracts query from multiple possible field names, resolves k (count), and optionally profile.
    Never raises; returns a valid search config even with minimal or missing input data.
    Ensures that fumbled recalls still return core memory rather than failing.
    """
    query = _first_string(QUERY_KEYS, payload) or ""

    if "k" in payload:
        try:
            k_val = int(float(payload["k"]))
        except (TypeError, ValueError, OverflowError):
            k_val = 8
    else:
        k_val = 8

    clamped_k = max(1, min(50, k_val))

    out: dict[str, Any] = {"query": query, "k": clamped_k}

    if "include_core" in payload:
        value = _optional_bool(payload["include_core"])
        if value is not None:
            out["include_core"] = value
    if "include_archived" in payload:
        if _optional_bool(payload["include_archived"]):
            out["include_archived"] = True
    for key, default, minimum, maximum in (("max_chars", 14000, 256, 100000),
                                           ("core_limit", 8, 0, 50)):
        if key in payload:
            try:
                value = int(float(payload[key]))
            except (TypeError, ValueError, OverflowError):
                value = default
            out[key] = max(minimum, min(maximum, value))
    if isinstance(payload.get("host"), str) and payload["host"].strip():
        out["host"] = payload["host"].strip()
    tags = payload.get("tags")
    if isinstance(tags, str):
        tags = tags.split(",")
    if isinstance(tags, (list, tuple)):
        out["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()]

    if "profile" in payload and isinstance(payload["profile"], str):
        stripped_profile = payload["profile"].strip()
        if stripped_profile:
            out["profile"] = stripped_profile

    return out
