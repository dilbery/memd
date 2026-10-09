"""Deterministic, stable slugify — the dedup + superseded_by key."""
import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SAFE_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,1023}\Z")


def validate_slug(value: str) -> str:
    """Validate an explicit identity without silently changing its target."""
    if not isinstance(value, str) or not _SAFE_SLUG.fullmatch(value):
        raise ValueError("slug must be 1–1024 lowercase letters, digits, hyphens or underscores")
    return value


def slugify(title: str) -> str:
    """lowercase; runs of non-alphanumerics -> single hyphen; strip edge hyphens.

    Stable across case/punctuation/whitespace edits so it is a reliable
    identity key for dedup/upsert and superseded_by.
    """
    lowered = title.lower()
    hyphenated = _NON_ALNUM.sub("-", lowered)
    return hyphenated.strip("-")
