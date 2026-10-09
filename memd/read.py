"""Read complete, pageable note bodies from the authoritative Git working tree."""
from __future__ import annotations

from pathlib import Path

from memd.config import Config
from memd.profiles import guard_paths
from memd.store import (archived_value, clone_lock, git_head_sha, read_note, assert_readable_tree,
                        StoreUnavailable as ReadUnavailable)

DEFAULT_READ_LIMIT = 8000
MAX_READ_LIMIT = 32000


class ReadRevisionConflict(ValueError):
    """The note changed between pages; restart reading the new revision."""


def bounded_int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(low, min(high, number))


def read(slug: str, *, profile: str, cfg: Config, offset: int = 0,
         limit: int = DEFAULT_READ_LIMIT, revision: str | None = None) -> dict:
    """Return one body page with exact offsets, revision and source metadata.

    Slugs are looked up as identifiers, never joined into paths. This also
    supports historical slugs that predate the stricter write validator.
    """
    if not isinstance(slug, str) or not slug.strip():
        raise ValueError("read requires a note slug from recall or save")
    slug = slug.strip()
    clone, _ = guard_paths(profile, cfg.clone, cfg.db)
    if not (clone / ".git").exists():
        raise FileNotFoundError("memory clone is unavailable")
    offset = bounded_int(offset, 0, 0, 2**63 - 1)
    limit = bounded_int(limit, DEFAULT_READ_LIMIT, 1, MAX_READ_LIMIT)
    with clone_lock(clone):
        assert_readable_tree(clone)
        note = read_note(clone, slug)
        if note is None:
            raise FileNotFoundError(f"no memory note with slug {slug!r}")
        from memd.store import RETRACTED
        if note.superseded_by == RETRACTED:
            raise FileNotFoundError("This memory was retracted by its owner and is unavailable to agents")
        if revision and revision != note.git_blob:
            raise ReadRevisionConflict("note changed since the previous page; restart at offset 0")
        data = note.to_dict()
        total = len(note.body)
        if offset > total:
            raise ValueError(f"offset {offset} exceeds note length {total}")
        end = min(total, offset + limit)
        receipt = {
            key: data.get(key) for key in (
                "slug", "title", "profile", "host", "tags", "importance", "pinned",
                "grounding", "source", "observed_at", "verified_at", "superseded_by",
                "description",
            )
        }
        # A note published from another store names its source (memd.share).
        if isinstance((note.metadata or {}).get("published_from"), dict):
            receipt["published_from"] = dict(note.metadata["published_from"])
        # An archived note (memd.forget) stays readable, clearly marked as history.
        archived = archived_value(note.metadata)
        if archived:
            reason = " ".join(str(note.metadata.get("archived_reason") or "").split())
            receipt["archived"] = archived
            if reason:
                receipt["archived_reason"] = reason
            receipt["notice"] = (f"ARCHIVED {archived} by review"
                                 + (f" ({reason})" if reason else "")
                                 + ": kept for history, excluded from recall and the core index; "
                                 "it may no longer be true. `mem-forget restore "
                                 f"{note.slug}` brings it back.")
        receipt.update(
            ok=True, body=note.body[offset:end], revision=note.git_blob,
            git_head=git_head_sha(clone), path=str(Path(note.path).relative_to(clone)),
            authority="git working tree", offset=offset, end_offset=end,
            total_chars=total, complete=end == total,
            next_offset=end if end < total else None,
        )
        if end < total:
            receipt["continuation"] = {
                "slug": note.slug, "profile": profile, "offset": end,
                "limit": limit, "revision": note.git_blob,
            }
        else:
            receipt["continuation"] = None
        return receipt
