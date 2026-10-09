"""Owner-only browsing and revision-checked corrections to existing memories.

Git owns the mutation and its provenance; the rebuildable search cache is
evicted before editing so a failed refresh cannot serve withdrawn/old content.
"""
import dataclasses
from datetime import datetime, timezone
from pathlib import Path
import re

from memd.config import Config
from memd.identity import current_identity
from memd.profiles import effective_environment, guard_paths
from memd.store import RETRACTED, assert_readable_tree, clone_lock, list_notes, read_note
from memd.save import RevisionConflict, _write_note, _restore_bytes, _commit_and_push, _CommitFailure

MAX_BODY = 200000


def owner_config():
    owner = current_identity()
    if not owner:
        raise PermissionError("A verified user identity is required")
    env = effective_environment()
    if not env.get("MEMD_STORES_ROOT"):
        raise PermissionError("Per-user stores are not enabled")
    cfg = Config.from_env({**env, "MEMD_PROFILE": owner}, env_file=None)
    clone, db = guard_paths(owner, cfg.clone, cfg.db)
    return dataclasses.replace(cfg, clone=clone, db=db, profile=owner)


def status(note):
    return "retracted" if note.superseded_by == RETRACTED else "superseded" if note.superseded_by else "active"


def summary(note):
    return {"slug": note.slug, "title": note.title, "description": note.description,
            "excerpt": note.body[:240], "tags": note.tags, "status": status(note),
            "revision": note.git_blob, "importance": note.importance, "source": note.source,
            "changed_at": note.metadata.get("user_changed_at"), "observed_at": note.observed_at,
            "verified_at": note.verified_at, "saved_by": note.saved_by}


def _notes(cfg):
    assert_readable_tree(cfg.clone)
    notes = list_notes(cfg.clone)
    if len({n.slug for n in notes}) != len(notes):
        raise RevisionConflict("Duplicate memory identifiers need repair before this store can be edited")
    return notes


def browse(query="", state="active", page=1):
    cfg = owner_config()
    if state not in {"active", "retracted", "superseded", "all"}:
        raise ValueError("Unknown memory status")
    if not isinstance(query, str) or len(query) > 200 or type(page) is not int or not 1 <= page <= 100000:
        raise ValueError("Invalid search or page")
    if not (cfg.clone / ".git" / "HEAD").exists():
        # Browsing never provisions or guesses a different store for a new user.
        return {"items": [], "total": 0, "page": 1, "pages": 1, "counts": {s:0 for s in ("active","retracted","superseded")}}
    with clone_lock(cfg.clone):
        notes = _notes(cfg)
        counts = {s:sum(status(n) == s for n in notes) for s in ("active","retracted","superseded")}
        terms = query.casefold().split()
        matches = [n for n in notes if (state == "all" or status(n) == state)
                   and all(t in (n.title + " " + n.body + " " + " ".join(n.tags) + " " + n.description).casefold() for t in terms)]
        matches.sort(key=lambda n: n.title.casefold())
        total = len(matches); pages = max(1, (total+24)//25); page = min(page,pages)
        return {"items": [summary(n) for n in matches[(page-1)*25:page*25]],
                "total": total, "page": page, "pages": pages, "counts": counts}


def detail(slug):
    cfg = owner_config()
    if not isinstance(slug, str) or not slug or len(slug) > 500:
        raise ValueError("A memory identifier is required")
    if not (cfg.clone / ".git" / "HEAD").exists():
        raise FileNotFoundError("Memory not found")
    with clone_lock(cfg.clone):
        note = next((n for n in _notes(cfg) if n.slug == slug), None)
        if note is None:
            raise FileNotFoundError("Memory not found")
        return {**summary(note), "body": note.body, "editable": status(note) == "active" and len(note.body) <= MAX_BODY,
                "host": note.host, "grounding": note.grounding,
                "superseded_by": note.superseded_by if status(note) == "superseded" else None}


def change(action, data, *, subject):
    cfg = owner_config()
    allowed = {"slug", "revision"} | ({"title", "body", "description", "tags"} if action == "edit" else set())
    if action not in {"edit", "retract"} or set(data) - allowed:
        raise ValueError("Unsupported memory fields")
    slug, revision = data.get("slug"), data.get("revision")
    if not isinstance(slug, str) or not slug or len(slug) > 500:
        raise ValueError("A memory identifier is required")
    if not isinstance(revision, str) or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", revision):
        raise ValueError("Read this memory before changing it; a content revision is required")
    if action == "edit":
        for key, maximum in (("title", 300), ("body", MAX_BODY), ("description", 1000)):
            value = data.get(key, "")
            if not isinstance(value, str) or len(value) > maximum or "\x00" in value or (key != "description" and not value.strip()):
                raise ValueError(f"Invalid {key}; maximum {maximum} characters")
        tags = data.get("tags", [])
        if not isinstance(tags, list) or len(tags) > 30 or not all(isinstance(t, str) and 0 < len(t.strip()) <= 80 and not any(ord(c)<32 for c in t) for t in tags):
            raise ValueError("Use at most 30 tags of up to 80 characters")
    if not (cfg.clone / ".git" / "HEAD").exists():
        raise FileNotFoundError("Memory not found")
    with clone_lock(cfg.clone):
        note = next((n for n in _notes(cfg) if n.slug == slug), None)
        if note is None:
            raise FileNotFoundError("Memory not found")
        if note.git_blob != revision or status(note) != "active":
            raise RevisionConflict("This memory has changed or is no longer active. Reload it before making another change.")
        updated = dataclasses.asdict(note)
        updated["metadata"] = dict(note.metadata)
        now = datetime.now(timezone.utc).isoformat()
        updated["metadata"].update(user_changed_at=now, user_changed_by=subject, user_action=action)
        updated["saved_by"] = cfg.profile
        if action == "retract":
            updated["superseded_by"] = RETRACTED
        else:
            updated.update(title=data["title"].strip(), body=data["body"].strip(),
                           description=data.get("description", "").strip(), tags=[t.strip() for t in tags],
                           grounding="unverified-remote")
        path = Path(note.path); before = path.read_bytes()
        index_path = cfg.clone / ".git" / "index"
        index_before = index_path.read_bytes() if index_path.exists() else None
        # A cache write failure blocks this operation before it changes Git.
        # Once evicted, a post-commit index failure leaves no stale content to serve.
        from memd.index import open_db, _prune_from_index
        db = open_db(cfg.db, dim=cfg.embed_dim)
        try:
            with db:
                _prune_from_index(db, slug)
                db.execute("DELETE FROM meta WHERE key IN ('head','lexical_head')")
        finally:
            db.close()
        try:
            _write_note(cfg.clone, updated, path=note.path)
        except (OSError, ValueError):
            _restore_bytes(path, before)
            raise
        try:
            receipt = _commit_and_push(cfg, f"memd: user {action} {slug}\n\nSaved-By: {cfg.profile}\nOIDC-Subject: {subject}", paths=[path])
        except _CommitFailure as error:
            _restore_bytes(path, before); _restore_bytes(index_path, index_before)
            raise error.__cause__ from error
        current = read_note(cfg.clone, slug)
        return {"saved": True, "action": action, "revision": current.git_blob if current else None,
                "synced": receipt.synced, "search_updated": receipt.lexical_indexed,
                "message": "Change saved." if receipt.synced else "Change saved locally. Backup sync is pending."}
