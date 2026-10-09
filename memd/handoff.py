"""Session handoff notes: where a coding session left off in a repository.

The Claude Code hook (memd.hooks.handoff, clients/memd-handoff-hook) writes a
short handoff when a session ends -- what was done, what is unfinished, next
steps, files touched and the Git state -- and files it through the ordinary
propose path (``POST /propose`` or memd.inbox.propose in process). The next
session in the same repository reads it back at start (``GET /handoff``).

This module is the store side:
  * a handoff is a fact with ``source: handoff`` and the tags ``handoff`` and
    exactly one ``repo:<key>`` (``repo_key``); memd.inbox.propose files it on the
    ``handoff`` channel, redacts it again, and deletes the same proposer's older
    pending handoff for that repository, so they replace rather than pile up;
  * approving one makes it an ordinary low-importance ``state`` note; its title
    ("Handoff: <key>") keeps one note per repository, updated in place;
  * ``latest`` returns the newest handoff for a repository from one store: a
    pending candidate only when the caller proposed it (pending text is not
    readable by other credentials), or an approved one whose note still exists.
    Text only; never another store's.
"""
from __future__ import annotations

import json
import re
import time

TAG = "handoff"
REPO_TAG = "repo:"
SOURCE = "handoff"
DEFAULT_MAX_AGE_DAYS = 14
MAX_AGE_DAYS_CEILING = 365
MAX_TEXT = 6000
_SCAN_LIMIT = 500
_REPO_RE = re.compile(r"^[a-z0-9][a-z0-9._/@~+-]{0,199}$")


def valid_repo(key) -> bool:
    return isinstance(key, str) and bool(_REPO_RE.match(key)) and ".." not in key


def repo_key(fact: dict) -> str | None:
    """The repository key of a handoff fact, or None when the fact is not one."""
    if not isinstance(fact, dict) or (fact.get("source") or "") != SOURCE:
        return None
    tags = fact.get("tags") or []
    if not isinstance(tags, list) or TAG not in tags:
        return None
    repos = [t[len(REPO_TAG):] for t in tags if isinstance(t, str) and t.startswith(REPO_TAG)]
    if len(repos) != 1 or not valid_repo(repos[0]):
        return None
    return repos[0]


def replace_pending(conn, repo: str, proposer: str, keep_id: str) -> int:
    """Delete ``proposer``'s other pending handoffs for ``repo``; the number removed.

    They were never reviewed, and only the newest says where the work stands.
    """
    stale = []
    for row in conn.execute("SELECT id, meta FROM candidates WHERE status='pending' AND source=? "
                            "AND proposer=? AND id<>?", (SOURCE, proposer, keep_id)):
        try:
            meta = json.loads(row["meta"] or "{}")
        except ValueError:
            continue
        if repo_key(meta) == repo:
            stale.append(row["id"])
    for cid in stale:
        conn.execute("DELETE FROM candidates WHERE id=? AND status='pending' AND claimed_at IS NULL", (cid,))
    return len(stale)


def max_age_days(value, default: int = DEFAULT_MAX_AGE_DAYS) -> int:
    try:
        return max(1, min(MAX_AGE_DAYS_CEILING, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def latest(cfg, profile: str, repo: str, *, proposer: str, max_age: int = DEFAULT_MAX_AGE_DAYS,
           now: float | None = None) -> dict | None:
    """The newest handoff for ``repo`` in this store, or None.

    Returns ``{repo, status, as_of, text}``: ``status`` is ``pending`` (a
    candidate the same ``proposer`` filed) or ``approved`` (the saved note's
    current text); ``as_of`` is when the handoff was written (epoch seconds).
    """
    from memd import inbox
    from memd.profiles import guard_paths
    from memd.store import read_note
    if not valid_repo(repo):
        raise ValueError("repo must be a repository key such as example.com/team/project")
    now = time.time() if now is None else now
    path = inbox.inbox_path(cfg, profile)
    if not path.exists():
        return None
    cutoff = now - max_age_days(max_age) * 86400
    conn = inbox._connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM candidates WHERE source=? AND status IN ('pending','approved') AND created>=? "
            "ORDER BY created DESC, id LIMIT ?", (SOURCE, cutoff, _SCAN_LIMIT)).fetchall()
    finally:
        conn.close()
    clone = None
    for row in rows:
        try:
            meta = json.loads(row["meta"] or "{}")
        except ValueError:
            continue
        if repo_key(meta) != repo:
            continue
        if row["status"] == "pending":
            if row["proposer"] != proposer:
                continue
            text = row["body"]
        else:
            if not row["slug"]:
                continue
            if clone is None:
                clone = guard_paths(profile, cfg.clone, cfg.db)[0]
            note = read_note(clone, row["slug"])
            if note is None:
                continue
            text = note.body
        return {"repo": repo, "status": row["status"], "as_of": row["created"],
                "text": (text or "")[:MAX_TEXT]}
    return None
