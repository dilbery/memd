"""Review inbox: candidate memories wait for a person before they are written.

An agent (or ``mem-inbox distill``) *proposes* a fact instead of saving it. The
candidate is kept in a per-store side file next to the index
(``memd.db`` -> ``memd.inbox.db``, owner-only, like memd.usage's file) with the
dry-run lint save would report (``memd.save.dry_run``: identity decision,
related notes, near-duplicates, conflicts), computed when it is proposed. A
reviewer lists candidates, approves one (optionally with edits), which calls the
real ``memd.save.save`` and records its receipt, or rejects it. Nothing reaches
the Git store before approval.

Who may review (memd.server, memd.user_web):
  * a signed-in account session (memd.access, CSRF-checked) with write access
    to the store, or the personal console's session for its own store;
  * the local ``mem-inbox`` CLI (it already has the store on disk);
  * an agent token only when MEMD_INBOX_TOKEN_REVIEW allows it: ``others``
    lets a write token review candidates proposed under a different label,
    ``all`` also its own. The default ``off`` means no token can approve.

MEMD_SAVE_MODE=inbox makes the MCP and HTTP ``save`` of a remote agent token
file a candidate instead of writing (default ``direct``: save writes as
before). Browser sessions and local callers always save directly.

Transcript distillation (``mem-inbox distill``) sends a Claude Code transcript
to the chat model (memd.llm; off unless MEMD_LLM_URL is set) in bounded chunks,
after redacting obvious secrets, and files each durable fact it extracts as a
candidate with source ``transcript``, skipping facts that duplicate an existing
note or a pending candidate.

A session handoff (memd.handoff: ``source: handoff``, tags ``handoff`` and one
``repo:<key>``) is filed on the ``handoff`` channel and replaces the same
proposer's older pending handoff for that repository.
"""
from __future__ import annotations

import argparse
import contextvars
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path

STATUSES = ("pending", "approved", "rejected")
MAX_TITLE = 500
MAX_BODY = 200000
MAX_PENDING = 1000               # pending candidates per store; a flood is refused
DECIDED_RETENTION_DAYS = 180     # approved/rejected rows are pruned after this
CLAIM_TTL_S = 300                # an approval in progress blocks a second one this long
TOKEN_REVIEW_MODES = ("off", "others", "all")
SAVE_MODES = ("direct", "inbox")
EDITABLE = ("title", "body", "tags", "importance", "host", "description", "source",
            "observed_at", "verified_at", "pinned", "volatility", "verify", "supersedes",
            "slug", "expected_revision")
# Edits that leave the proposer's text theirs (a token reviewer may make only these).
METADATA_EDITS = frozenset({"tags", "importance"})
_ID = re.compile(r"^[0-9a-f]{16}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    proposer TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    lint TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    reviewer TEXT,
    decided_at REAL,
    reason TEXT,
    slug TEXT,
    revision TEXT,
    receipt TEXT,
    original TEXT,
    claimed_at REAL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS candidates_status ON candidates(status, created);
CREATE INDEX IF NOT EXISTS candidates_hash ON candidates(content_hash);
"""


class NotPending(ValueError):
    """The candidate was already decided, or another approval is in progress."""


# --------------------------------------------------------------------------- settings


def token_review_mode(env=None) -> str:
    value = ((env if env is not None else os.environ).get("MEMD_INBOX_TOKEN_REVIEW") or "off").strip().lower()
    return value if value in TOKEN_REVIEW_MODES else "off"


def save_mode(env=None) -> str:
    value = ((env if env is not None else os.environ).get("MEMD_SAVE_MODE") or "direct").strip().lower()
    return value if value in SAVE_MODES else "direct"


def save_routes_to_inbox() -> bool:
    """True when this request's `save` should file a candidate instead of writing.

    Only in MEMD_SAVE_MODE=inbox, and only for a remote caller that is not a
    signed-in browser session: an agent token (legacy, registry, Kasm, OIDC).
    """
    if save_mode() != "inbox":
        return False
    from memd import access
    if not access.remote_request.get():
        return False
    principal = access.current.get()
    return not (principal is not None and principal.session)


def reviewer_for(profile: str) -> tuple[str, bool]:
    """(reviewer label, is_token) for the current HTTP/MCP caller, or PermissionError.

    A signed-in account session needs write access to the store. An agent token
    may review only when MEMD_INBOX_TOKEN_REVIEW is not ``off``, and then needs
    write access too. A local caller (CLI) is the operator.
    """
    from memd import access
    from memd.actor import get_actor
    principal = access.current.get()
    if principal is not None and principal.session:
        if principal.grants.get(profile) != "write":
            raise PermissionError("Reviewing the inbox needs write access to this store.")
        return principal.label, False
    if not access.remote_request.get():
        return get_actor() or "local", False
    if token_review_mode() == "off":
        raise PermissionError("Agent tokens cannot review the inbox; sign in with an account "
                              "(or set MEMD_INBOX_TOKEN_REVIEW).")
    access.authorize(profile, write=True)
    return get_actor() or "token", True


# --------------------------------------------------------------------------- storage


def inbox_file(db_path: Path | str) -> Path:
    """The inbox file of the index at db_path (``memd.db`` -> ``memd.inbox.db``)."""
    db_path = Path(db_path)
    return db_path.with_name(db_path.stem + ".inbox.db")


def inbox_path(cfg, profile: str) -> Path:
    from memd.profiles import guard_paths
    db = guard_paths(profile, cfg.clone, cfg.db)[1]
    if db is None:
        raise ValueError("no index configured for this store; the inbox lives next to it")
    return inbox_file(db)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Candidates can hold anything an agent wrote: owner-only before SQLite opens it.
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # Added with memd.share: where a published candidate was copied from.
    if "provenance" not in {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}:
        conn.execute("ALTER TABLE candidates ADD COLUMN provenance TEXT")
    return conn


def content_hash(title: str, body: str) -> str:
    text = " ".join((title + "\n" + body).casefold().split())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _row(row: sqlite3.Row, *, full: bool = True) -> dict:
    out = {
        "id": row["id"], "status": row["status"], "created": row["created"],
        "source": row["source"], "proposer": row["proposer"], "title": row["title"],
        "meta": json.loads(row["meta"] or "{}"), "lint": json.loads(row["lint"] or "{}"),
        "reviewer": row["reviewer"], "decided_at": row["decided_at"], "reason": row["reason"],
        "slug": row["slug"], "revision": row["revision"], "last_error": row["last_error"],
        "published_from": json.loads(row["provenance"]) if row["provenance"] else None,
    }
    if full:
        out["body"] = row["body"]
        out["receipt"] = json.loads(row["receipt"]) if row["receipt"] else None
        out["original"] = json.loads(row["original"]) if row["original"] else None
    else:
        out["excerpt"] = row["body"][:240]
    return out


def _check_id(candidate_id) -> str:
    if not isinstance(candidate_id, str) or not _ID.match(candidate_id):
        raise ValueError("A candidate id (16 hex characters) is required")
    return candidate_id


def _prune(conn: sqlite3.Connection, now: float) -> None:
    conn.execute("DELETE FROM candidates WHERE status <> 'pending' AND decided_at < ?",
                 (now - DECIDED_RETENTION_DAYS * 86400,))


# --------------------------------------------------------------------------- operations


def _split_fact(fact: dict) -> tuple[str, str, dict]:
    title, body = fact["title"], fact["body"]
    if len(title) > MAX_TITLE or len(body) > MAX_BODY or "\x00" in title + body:
        raise ValueError(f"A candidate title is at most {MAX_TITLE} and a body {MAX_BODY} characters")
    meta = {k: v for k, v in fact.items() if k not in ("title", "body", "profile")}
    return title, body, meta


def lint(fact: dict, profile: str, *, cfg) -> dict:
    """Save's dry-run checks; a failure becomes an error field, never an exception."""
    from memd.save import dry_run
    try:
        return dry_run(fact, profile, cfg=cfg)
    except Exception as exc:
        return {"error": f"lint unavailable: {type(exc).__name__}: {exc}"[:300],
                "related": [], "near_duplicates": [], "conflicts": [], "warnings": []}


def propose(payload: dict, profile: str, *, cfg, source: str = "agent", proposer: str = "",
            precomputed_lint: dict | None = None, now: float | None = None,
            provenance: dict | None = None) -> dict:
    """File one candidate; returns {ok, id, status, duplicate, lint, ...}.

    ``payload`` takes save's fields (memd.normalize). An identical pending
    candidate (same title, body and provenance) is returned instead of a second row.
    ``provenance`` is set only by memd.share.publish (never from a payload): the
    approved note then carries it as ``published_from``.
    """
    from memd.normalize import normalize_fact
    from memd import handoff
    fact = normalize_fact(payload or {})
    fact.pop("profile", None)
    # A session handoff (memd.handoff) has its own channel, is redacted again
    # here, and replaces the same proposer's older pending one for its repository.
    handoff_repo = handoff.repo_key(fact)
    if handoff_repo is not None:
        source = handoff.SOURCE
        fact["title"], fact["body"] = redact(fact["title"]), redact(fact["body"])
    title, body, meta = _split_fact(fact)
    digest = content_hash(title, body)
    now = time.time() if now is None else now
    path = inbox_path(cfg, profile)
    conn = _connect(path)
    try:
        # Provenance is part of identity: a publish must not fold into an ordinary
        # proposal (or another source's publish) and lose its published_from.
        existing = conn.execute(
            "SELECT * FROM candidates WHERE content_hash=? AND status='pending' AND provenance IS ?",
            (digest, json.dumps(provenance, sort_keys=True) if provenance else None)).fetchone()
        if existing is not None:
            return {"ok": True, "proposed": True, "duplicate": True, "id": existing["id"],
                    "status": "pending", "lint": json.loads(existing["lint"] or "{}"),
                    "message": "An identical candidate is already waiting for review."}
        pending = conn.execute("SELECT COUNT(*) FROM candidates WHERE status='pending'").fetchone()[0]
        if pending >= MAX_PENDING:
            raise ValueError(f"The inbox already holds {MAX_PENDING} pending candidates; review some first")
    finally:
        conn.close()
    # Lint outside the file's transaction: it reads Git and may call the model.
    report = precomputed_lint if precomputed_lint is not None else lint(fact, profile, cfg=cfg)
    cid = secrets.token_hex(8)
    conn = _connect(path)
    try:
        with conn:
            _prune(conn, now)
            conn.execute(
                "INSERT INTO candidates(id,created,source,proposer,title,body,meta,content_hash,lint,status,"
                "provenance) VALUES(?,?,?,?,?,?,?,?,?,'pending',?)",
                (cid, now, (source or "")[:100], (proposer or "")[:200], title, body,
                 json.dumps(meta, sort_keys=True), digest, json.dumps(report),
                 json.dumps(provenance, sort_keys=True) if provenance else None))
            if handoff_repo is not None:
                handoff.replace_pending(conn, handoff_repo, (proposer or "")[:200], cid)
    finally:
        conn.close()
    return {"ok": True, "proposed": True, "duplicate": False, "id": cid, "status": "pending",
            "lint": report, "message": "Waiting for review; nothing is saved until a person approves it."}


def queued_receipt(proposed: dict) -> dict:
    """A save-shaped receipt for a save that MEMD_SAVE_MODE=inbox filed as a candidate."""
    report = proposed.get("lint") or {}
    warnings = ["Queued for review (MEMD_SAVE_MODE=inbox): nothing is saved until a person "
                f"approves candidate {proposed['id']}."] + list(report.get("warnings") or [])
    if report.get("error"):
        warnings.append(f"Approving it as proposed would fail: {report['error']}")
    return {"ok": True, "queued": True, "inbox_id": proposed["id"], "duplicate": proposed["duplicate"],
            "saved": False, "synced": False, "indexed": False, "lexical_indexed": False,
            "slug": report.get("slug") or "", "action": "proposed", "revision": "",
            "related": list(report.get("related") or []), "conflicts": list(report.get("conflicts") or []),
            "warnings": warnings}


def list_candidates(cfg, profile: str, *, status: str = "pending", limit: int = 50,
                    offset: int = 0) -> dict:
    if status not in STATUSES + ("all",):
        raise ValueError("status must be pending, approved, rejected or all")
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    path = inbox_path(cfg, profile)
    counts = {s: 0 for s in STATUSES}
    if not path.exists():
        return {"ok": True, "items": [], "total": 0, "counts": counts, "offset": offset}
    conn = _connect(path)
    try:
        for s, n in conn.execute("SELECT status, COUNT(*) FROM candidates GROUP BY status"):
            if s in counts:
                counts[s] = n
        where, args = ("", ()) if status == "all" else ("WHERE status=?", (status,))
        total = conn.execute(f"SELECT COUNT(*) FROM candidates {where}", args).fetchone()[0]
        order = "created ASC" if status == "pending" else "COALESCE(decided_at, created) DESC"
        rows = conn.execute(f"SELECT * FROM candidates {where} ORDER BY {order}, id LIMIT ? OFFSET ?",
                            (*args, limit, offset)).fetchall()
    finally:
        conn.close()
    return {"ok": True, "items": [_row(r, full=False) for r in rows], "total": total,
            "counts": counts, "offset": offset}


def get(cfg, profile: str, candidate_id: str) -> dict:
    _check_id(candidate_id)
    path = inbox_path(cfg, profile)
    if not path.exists():
        raise FileNotFoundError("Candidate not found")
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise FileNotFoundError("Candidate not found")
    return _row(row)


def _merge(row: sqlite3.Row, edits: dict | None) -> dict:
    fact = {"title": row["title"], "body": row["body"], **json.loads(row["meta"] or "{}")}
    edits = edits or {}
    if not isinstance(edits, dict) or set(edits) - set(EDITABLE):
        raise ValueError("Editable fields are: " + ", ".join(EDITABLE))
    for key, value in edits.items():
        if value is None:
            fact.pop(key, None)       # an explicit null removes a proposed field
        else:
            fact[key] = value
    return fact


def approve(cfg, profile: str, candidate_id: str, *, reviewer: str, edits: dict | None = None,
            reviewer_is_token: bool = False, now: float | None = None, save_fn=None) -> dict:
    """Write the candidate (with edits) through the real save path; record the receipt.

    Raises NotPending when it was already decided (or is being approved),
    PermissionError when a token may not review it, and whatever save raises
    (the candidate then stays pending with ``last_error`` set).
    """
    from memd.normalize import normalize_fact
    from memd.actor import set_actor
    _check_id(candidate_id)
    path = inbox_path(cfg, profile)
    if not path.exists():
        raise FileNotFoundError("Candidate not found")
    now = time.time() if now is None else now
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise FileNotFoundError("Candidate not found")
        if row["status"] != "pending":
            raise NotPending(f"This candidate was already {row['status']}")
        if reviewer_is_token and token_review_mode() != "all" and row["proposer"] == reviewer:
            raise PermissionError("A token cannot approve its own proposal "
                                  "(MEMD_INBOX_TOKEN_REVIEW=all allows it)")
        fact = normalize_fact(_merge(row, edits))
        fact.pop("profile", None)
        proposed = normalize_fact(_merge(row, None))
        proposed.pop("profile", None)
        rewritten = {k for k in set(fact) | set(proposed) if fact.get(k) != proposed.get(k)} - METADATA_EDITS
        if reviewer_is_token and rewritten and token_review_mode() != "all":
            # Otherwise one token could rewrite another's candidate and approve it alone.
            raise PermissionError("A token reviewer may change only " + " and ".join(sorted(METADATA_EDITS))
                                  + "; other edits need a signed-in reviewer")
        title, body, meta = _split_fact(fact)
        with conn:
            claimed = conn.execute(
                "UPDATE candidates SET claimed_at=? WHERE id=? AND status='pending' "
                "AND (claimed_at IS NULL OR claimed_at < ?)",
                (now, candidate_id, now - CLAIM_TTL_S)).rowcount
        if not claimed:
            raise NotPending("Another approval of this candidate is in progress")
    finally:
        conn.close()
    if save_fn is None:
        from memd.save import save as save_fn
    # The note is attributed to the credential that proposed it; the reviewer and
    # any original text are recorded here. A token that rewrote the text (only
    # possible with MEMD_INBOX_TOKEN_REVIEW=all) wrote it, so it is credited.
    # A copied context keeps the caller's own label intact.
    author = reviewer if reviewer_is_token and rewritten else (row["proposer"] or reviewer)
    context = contextvars.copy_context()

    from memd.save import publishing
    provenance = json.loads(row["provenance"]) if row["provenance"] else None

    def _write():
        set_actor(author)
        with publishing(provenance):
            return save_fn(fact, profile=profile, cfg=cfg)
    try:
        result = context.run(_write)
    except Exception as exc:
        conn = _connect(path)
        try:
            with conn:
                conn.execute("UPDATE candidates SET claimed_at=NULL, last_error=? WHERE id=?",
                             (f"{type(exc).__name__}: {exc}"[:500], candidate_id))
        finally:
            conn.close()
        raise
    receipt = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    original = None
    if (title, body, meta) != (row["title"], row["body"], json.loads(row["meta"] or "{}")):
        original = {"title": row["title"], "body": row["body"], "meta": json.loads(row["meta"] or "{}")}
    conn = _connect(path)
    try:
        with conn:
            conn.execute(
                "UPDATE candidates SET status='approved', reviewer=?, decided_at=?, slug=?, revision=?,"
                " receipt=?, title=?, body=?, meta=?, original=?, claimed_at=NULL, last_error=NULL"
                " WHERE id=?",
                (reviewer[:200], now, receipt.get("slug"), receipt.get("revision"), json.dumps(receipt),
                 title, body, json.dumps(meta, sort_keys=True),
                 json.dumps(original) if original else None, candidate_id))
    finally:
        conn.close()
    return {"ok": True, "id": candidate_id, "status": "approved", "edited": original is not None,
            "receipt": receipt}


def reject(cfg, profile: str, candidate_id: str, *, reviewer: str, reason: str = "",
           reviewer_is_token: bool = False, now: float | None = None) -> dict:
    _check_id(candidate_id)
    if not isinstance(reason, str) or len(reason) > 1000:
        raise ValueError("A reason is at most 1000 characters")
    path = inbox_path(cfg, profile)
    if not path.exists():
        raise FileNotFoundError("Candidate not found")
    now = time.time() if now is None else now
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise FileNotFoundError("Candidate not found")
        if row["status"] != "pending":
            raise NotPending(f"This candidate was already {row['status']}")
        if reviewer_is_token and token_review_mode() != "all" and row["proposer"] == reviewer:
            raise PermissionError("A token cannot review its own proposal "
                                  "(MEMD_INBOX_TOKEN_REVIEW=all allows it)")
        with conn:
            changed = conn.execute(
                "UPDATE candidates SET status='rejected', reviewer=?, decided_at=?, reason=?"
                " WHERE id=? AND status='pending' AND (claimed_at IS NULL OR claimed_at < ?)",
                (reviewer[:200], now, reason.strip(), candidate_id, now - CLAIM_TTL_S)).rowcount
        if not changed:
            raise NotPending("This candidate is being approved")
    finally:
        conn.close()
    return {"ok": True, "id": candidate_id, "status": "rejected"}


def pending_texts(cfg, profile: str) -> list[tuple[str, str, str]]:
    """(id, title, body) of every pending candidate, for distill's dedupe."""
    path = inbox_path(cfg, profile)
    if not path.exists():
        return []
    conn = _connect(path)
    try:
        return [(r["id"], r["title"], r["body"]) for r in
                conn.execute("SELECT id, title, body FROM candidates WHERE status='pending'")]
    finally:
        conn.close()


def known(cfg, profile: str) -> tuple[set[str], set[str], int]:
    """(proposed sources, content hashes, pending count) over candidates of every status.

    memd.importers uses it so re-running an import never files an item again,
    even one a reviewer already approved or rejected (until decided rows are
    pruned after DECIDED_RETENTION_DAYS).
    """
    path = inbox_path(cfg, profile)
    if not path.exists():
        return set(), set(), 0
    conn = _connect(path)
    try:
        sources: set[str] = set()
        hashes: set[str] = set()
        pending = 0
        for row in conn.execute("SELECT status, meta, content_hash FROM candidates"):
            hashes.add(row["content_hash"])
            pending += row["status"] == "pending"
            try:
                source = json.loads(row["meta"] or "{}").get("source")
            except (ValueError, AttributeError):
                source = None
            if isinstance(source, str) and source:
                sources.add(source)
        return sources, hashes, pending
    finally:
        conn.close()


# --------------------------------------------------------------------------- redaction

_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
                r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)", re.S),
     "[REDACTED PRIVATE KEY]"),
    (re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|basic|token)\s+)[^\s\"'`]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:https?|ssh|git|ftp|postgres(?:ql)?|mysql|redis|mongodb(?:\+srv)?|amqp)://)"
                r"[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),
    (re.compile(r"\b(?:sk|pk|rk)-(?:[A-Za-z0-9]+-)*[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED]"),
    (re.compile(r"\bmemd_[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
    # password: "correct horse battery" -- a quoted value may contain spaces.
    (re.compile(r"(?i)(\b[\w.-]*(?:token|secret|passw(?:or)?d|passwd|pwd|api[_-]?key|access[_-]?key|"
                r"private[_-]?key|client[_-]?secret|credentials?)[\w.-]*[\"']?\s*[:=]\s*)"
                r"(?:\"[^\"\n]{5,}\"|'[^'\n]{5,}')"), r'\1"[REDACTED]"'),
    # NAME_TOKEN=value, "api_key": "value", password: value ...
    (re.compile(r"(?i)(\b[\w.-]*(?:token|secret|passw(?:or)?d|passwd|pwd|api[_-]?key|access[_-]?key|"
                r"private[_-]?key|client[_-]?secret|credentials?)[\w.-]*[\"']?\s*[:=]\s*[\"']?)"
                r"(?!\[REDACTED)[^\s\"',;}]{5,}"), r"\1[REDACTED]"),
    # Long mixed-case alphanumeric runs (keys); plain hex such as commit ids is kept.
    (re.compile(r"(?<![A-Za-z0-9+/_-])(?=[A-Za-z0-9+/_-]*[A-Z])(?=[A-Za-z0-9+/_-]*[a-z])"
                r"(?=[A-Za-z0-9+/_-]*[0-9])[A-Za-z0-9+/_-]{40,}={0,2}"), "[REDACTED]"),
]


def redact(text: str) -> str:
    """Strip obvious secrets (keys, tokens, passwords, URL credentials). Conservative."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# --------------------------------------------------------------------------- distillation

CHUNK_CHARS = 12000              # transcript characters per model call
MAX_CHUNKS = 20                  # model calls per transcript
MAX_FACTS_PER_CHUNK = 10
MESSAGE_CHARS = 4000             # one message's text kept (head) before chunking
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)

DISTILL_PROMPT = """\
You extract DURABLE facts from a transcript of a coding-agent session, for a \
shared memory store about computers, services, projects and preferences.

Keep only facts that stay useful in future sessions: configuration and where \
things run, decisions and their reasons, user preferences, fixes for problems \
that may recur, project conventions. Skip greetings, the task's step-by-step \
progress, speculation, anything only true for this session, and anything that \
looks like a password, token or key (write no credentials at all).

Each fact is self-contained: a short title and a body of one to five sentences \
that makes sense without the transcript. Name hosts, services and paths \
explicitly. Use at most 10 facts; none is a fine answer.

Answer with JSON only, no prose and no code fence:
{"facts": [{"title": "...", "body": "...", "tags": ["..."], "importance": 3}]}
"""

FACT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["facts"],
    "properties": {"facts": {"type": "array", "maxItems": MAX_FACTS_PER_CHUNK, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["title", "body", "tags", "importance"],
        "properties": {
            "title": {"type": "string"}, "body": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "importance": {"type": "integer", "minimum": 1, "maximum": 5},
        },
    }}},
}
RESPONSE_FORMAT = {"type": "json_schema",
                   "json_schema": {"name": "facts", "strict": True, "schema": FACT_SCHEMA}}


class FactParseError(ValueError):
    """The model's reply is not a JSON object with a facts list."""


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [item.get("text", "") for item in content
                 if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)]
        return "\n".join(p for p in parts if p)
    return ""


def transcript_messages(lines) -> tuple[list[tuple[str, str]], int]:
    """([(role, text)], malformed line count) from Claude Code transcript JSONL lines.

    Only user and assistant *text* is kept: tool calls, tool results, thinking
    and system reminders are dropped (they are bulky and the likeliest place for
    secrets). Malformed lines are counted and skipped.
    """
    messages: list[tuple[str, str]] = []
    bad = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(entry, dict) or entry.get("isMeta"):
            continue
        kind = entry.get("type")
        message = entry.get("message")
        if kind not in ("user", "assistant") or not isinstance(message, dict):
            continue
        text = _REMINDER.sub("", _content_text(message.get("content"))).strip()
        if not text or text.startswith(("<command-", "<local-command-")):
            continue
        if len(text) > MESSAGE_CHARS:
            text = text[:MESSAGE_CHARS] + " [...]"
        messages.append(("User" if kind == "user" else "Assistant", text))
    return messages, bad


def chunks(messages: list[tuple[str, str]], *, size: int = CHUNK_CHARS,
           limit: int = MAX_CHUNKS) -> tuple[list[str], bool]:
    """Redacted transcript text in chunks of at most `size` characters; (chunks, truncated)."""
    out: list[str] = []
    current = ""
    for role, text in messages:
        block = f"{role}: {redact(text)}\n\n"
        while len(block) > size:
            if current:
                out.append(current)
                current = ""
            out.append(block[:size])
            block = block[size:]
        if len(current) + len(block) > size:
            out.append(current)
            current = ""
        current += block
    if current.strip():
        out.append(current)
    return out[:limit], len(out) > limit


def parse_facts(text: str) -> list[dict]:
    """Validated facts from a model reply; bad items dropped, a bad reply raises."""
    text = (text or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise FactParseError("reply contains no JSON object") from None
        try:
            data = json.loads(text[start:end + 1])
        except ValueError as e:
            raise FactParseError(f"reply is not valid JSON: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
        raise FactParseError("reply JSON has no facts list")
    out = []
    for item in data["facts"][:MAX_FACTS_PER_CHUNK]:
        if not isinstance(item, dict):
            continue
        title, body = item.get("title"), item.get("body")
        if not isinstance(title, str) or not isinstance(body, str):
            continue
        title, body = " ".join(title.split())[:200], body.strip()[:4000]
        if not title or len(body) < 10:
            continue
        tags = item.get("tags")
        tags = [str(t).strip()[:60] for t in tags if isinstance(t, str) and t.strip()][:8] \
            if isinstance(tags, list) else []
        try:
            importance = max(1, min(5, int(item.get("importance", 3))))
        except (TypeError, ValueError):
            importance = 3
        out.append({"title": redact(title), "body": redact(body), "tags": tags, "importance": importance})
    return out


def _ask(chunk: str, cfg) -> str:
    from memd.llm import LLMError, chat
    messages = [{"role": "system", "content": DISTILL_PROMPT},
                {"role": "user", "content": "Transcript excerpt:\n\n" + chunk}]
    try:
        return chat(messages, cfg=cfg, max_tokens=1500, temperature=0.0, response_format=RESPONSE_FORMAT)
    except LLMError as e:
        # Not every OpenAI-compatible server accepts json_schema (see memd.facts).
        if "HTTP 400" not in str(e) and "HTTP 422" not in str(e):
            raise
        return chat(messages, cfg=cfg, max_tokens=1500, temperature=0.0)


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9][a-z0-9._-]{2,}", text.casefold())}


def _similar(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.7


def distill(lines, *, cfg, profile: str, name: str = "transcript", dry_run: bool = False,
            max_chunks: int = MAX_CHUNKS) -> dict:
    """Extract durable facts from a transcript and file them as candidates.

    Returns a report: chunks sent, facts extracted, candidates filed (ids),
    facts skipped as duplicates (with the note or candidate they repeat),
    malformed transcript lines and per-chunk model errors.
    """
    from memd.llm import enabled
    if not enabled(cfg):
        raise RuntimeError("transcript distillation needs a chat model (set MEMD_LLM_URL)")
    messages, bad = transcript_messages(lines)
    report = distill_messages(messages, cfg=cfg, profile=profile, name=name, dry_run=dry_run,
                              max_chunks=max_chunks)
    report["malformed_lines"] = bad
    return report


def distill_messages(messages: list[tuple[str, str]], *, cfg, profile: str, name: str = "transcript",
                     dry_run: bool = False, max_chunks: int = MAX_CHUNKS, channel: str = "transcript",
                     proposer: str = "mem-inbox distill", fact_source: str | None = None,
                     tags: list[str] | tuple[str, ...] = (), limit: int | None = None,
                     seen: list[tuple[str, str]] | None = None) -> dict:
    """Distill (role, text) messages into candidates; the body of distill.

    memd.importers reuses it for exported chat conversations: ``channel``,
    ``proposer``, ``fact_source`` and extra ``tags`` mark where a fact came
    from, ``limit`` stops filing after that many candidates (``capped`` in the
    report) and ``seen`` carries already-filed (title, body) pairs across calls
    so facts repeated between conversations are skipped.
    """
    from memd.llm import enabled
    if not enabled(cfg):
        raise RuntimeError("transcript distillation needs a chat model (set MEMD_LLM_URL)")
    parts, truncated = chunks(messages, limit=max(1, max_chunks))
    report: dict = {"ok": True, "transcript": name, "messages": len(messages), "malformed_lines": 0,
                    "chunks": len(parts), "truncated": truncated, "extracted": 0, "filed": [],
                    "skipped": [], "errors": [], "dry_run": dry_run, "facts": [], "capped": False}
    seen = [] if seen is None else seen
    pending = pending_texts(cfg, profile)
    source = (fact_source if fact_source is not None else f"transcript {name}")[:200]

    def full() -> bool:
        return limit is not None and len(report["filed"]) + len(report["facts"]) >= limit
    for number, part in enumerate(parts, 1):
        if full():
            report["capped"] = True
            break
        try:
            facts = parse_facts(_ask(part, cfg))
        except Exception as exc:
            report["errors"].append(f"chunk {number}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        for fact in facts:
            report["extracted"] += 1
            if full():
                report["capped"] = True
                continue
            text = fact["title"] + "\n" + fact["body"]
            dup = next((f"transcript fact '{t}'" for t, b in seen if _similar(text, t + "\n" + b)), None)
            dup = dup or next((f"candidate {cid}" for cid, t, b in pending
                               if content_hash(t, b) == content_hash(fact["title"], fact["body"])
                               or _similar(text, t + "\n" + b)), None)
            fact = {**fact, "source": source}
            if tags:
                fact["tags"] = list(dict.fromkeys([*fact.get("tags", []), *tags]))
            report_lint = None
            if dup is None:
                report_lint = lint(fact, profile, cfg=cfg)
                near = report_lint.get("near_duplicates") or []
                if near:
                    dup = f"note {near[0]['slug']}"
                elif report_lint.get("action") == "update":
                    dup = f"note {report_lint.get('slug')}"
            seen.append((fact["title"], fact["body"]))
            if dup is not None:
                report["skipped"].append({"title": fact["title"], "duplicate_of": dup})
                continue
            if dry_run:
                report["facts"].append(fact)
                continue
            filed = propose(fact, profile, cfg=cfg, source=channel, proposer=proposer,
                            precomputed_lint=report_lint)
            if filed["duplicate"]:
                report["skipped"].append({"title": fact["title"], "duplicate_of": f"candidate {filed['id']}"})
            else:
                report["filed"].append({"id": filed["id"], "title": fact["title"]})
    if parts and len(report["errors"]) == len(parts):
        report["ok"] = False
    return report


# --------------------------------------------------------------------------- CLI


def _print_candidate(item: dict) -> None:
    print(f"id:       {item['id']}")
    print(f"status:   {item['status']}")
    print(f"source:   {item['source']}  proposer: {item['proposer'] or '-'}")
    print(f"created:  {time.strftime('%Y-%m-%d %H:%M', time.localtime(item['created']))}")
    if item["reviewer"]:
        print(f"reviewer: {item['reviewer']}  slug: {item['slug'] or '-'}  revision: {item['revision'] or '-'}")
    if item.get("reason"):
        print(f"reason:   {item['reason']}")
    if item["meta"]:
        print(f"metadata: {json.dumps(item['meta'], sort_keys=True)}")
    report = item["lint"] or {}
    print(f"lint:     action={report.get('action', '?')} slug={report.get('slug', '?')}")
    for key in ("related", "near_duplicates", "conflicts", "warnings"):
        for value in report.get(key) or []:
            print(f"  {key}: {value if isinstance(value, str) else json.dumps(value)}")
    if report.get("error"):
        print(f"  error: {report['error']}")
    if item.get("last_error"):
        print(f"last approval error: {item['last_error']}")
    print(f"\n# {item['title']}\n\n{item['body']}")


def main(argv: list[str] | None = None) -> int:
    from memd.config import Config
    ap = argparse.ArgumentParser(
        prog="mem-inbox",
        description="Review candidate memories before they are written, and distill transcripts into candidates.")
    ap.add_argument("--json", action="store_true", help="print JSON")
    sub = ap.add_subparsers(dest="command", required=True)
    ls = sub.add_parser("list", help="list candidates (pending by default)")
    ls.add_argument("--status", default="pending", choices=list(STATUSES) + ["all"])
    ls.add_argument("--limit", type=int, default=50)
    sh = sub.add_parser("show", help="show one candidate with its lint preview")
    sh.add_argument("id")
    ok = sub.add_parser("approve", help="save a candidate through the normal save path")
    ok.add_argument("id")
    ok.add_argument("--title")
    ok.add_argument("--body-file", help="replacement body from a file ('-' for stdin)")
    ok.add_argument("--tags", help="replacement tags, comma-separated")
    ok.add_argument("--importance", type=int)
    ok.add_argument("--host")
    ok.add_argument("--supersedes", help="retire this existing slug in favour of the candidate")
    ok.add_argument("--reviewer", help="reviewer label recorded with the decision (default: cli:$USER)")
    no = sub.add_parser("reject", help="reject a candidate; nothing is written")
    no.add_argument("id")
    no.add_argument("--reason", default="")
    no.add_argument("--reviewer")
    ds = sub.add_parser("distill", help="extract durable facts from a Claude Code transcript (JSONL)")
    ds.add_argument("transcript", help="transcript .jsonl file, or '-' for stdin")
    ds.add_argument("--dry-run", action="store_true", help="print the facts; file no candidates")
    ds.add_argument("--max-chunks", type=int, default=MAX_CHUNKS,
                    help=f"model calls per transcript (default {MAX_CHUNKS})")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    cfg = Config.from_env()
    if cfg.clone is None or cfg.db is None:
        print("mem-inbox: no clone/index configured (MEMD_CLONE / MEMD_DB / MEMD_PROFILE)", file=sys.stderr)
        return 2
    profile = cfg.profile
    reviewer = getattr(args, "reviewer", None) or f"cli:{os.environ.get('USER') or 'local'}"
    try:
        if args.command == "list":
            out = list_candidates(cfg, profile, status=args.status, limit=args.limit)
            if args.json:
                print(json.dumps(out))
            else:
                counts = out["counts"]
                print(f"pending {counts['pending']}  approved {counts['approved']}  rejected {counts['rejected']}")
                for item in out["items"]:
                    flags = []
                    lint_report = item["lint"] or {}
                    if lint_report.get("near_duplicates"):
                        flags.append("near-duplicate")
                    if lint_report.get("conflicts"):
                        flags.append("conflict")
                    if lint_report.get("error"):
                        flags.append("error")
                    extra = f"  [{', '.join(flags)}]" if flags else ""
                    print(f"{item['id']}  {item['status']:<8}  {item['source']:<10}  {item['title']}{extra}")
            return 0
        if args.command == "show":
            item = get(cfg, profile, args.id)
            if args.json:
                print(json.dumps(item))
            else:
                _print_candidate(item)
            return 0
        if args.command == "approve":
            edits: dict = {}
            if args.title is not None:
                edits["title"] = args.title
            if args.body_file:
                edits["body"] = (sys.stdin.read() if args.body_file == "-"
                                 else Path(args.body_file).read_text(encoding="utf-8"))
            if args.tags is not None:
                edits["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
            for key in ("importance", "host", "supersedes"):
                if getattr(args, key) is not None:
                    edits[key] = getattr(args, key)
            out = approve(cfg, profile, args.id, reviewer=reviewer, edits=edits)
            receipt = out["receipt"]
            print(json.dumps(out) if args.json else
                  f"approved {args.id}: {receipt.get('action')} {receipt.get('slug')} "
                  f"(revision {receipt.get('revision') or '-'}, synced {receipt.get('synced')})")
            for warning in ([] if args.json else receipt.get("warnings") or []):
                print(f"  warning: {warning}")
            return 0
        if args.command == "reject":
            out = reject(cfg, profile, args.id, reviewer=reviewer, reason=args.reason)
            print(json.dumps(out) if args.json else f"rejected {args.id}")
            return 0
        if args.transcript == "-":
            lines, name = sys.stdin.read().splitlines(), "stdin"
        else:
            source = Path(args.transcript)
            lines, name = source.read_text(encoding="utf-8", errors="replace").splitlines(), source.name
        report = distill(lines, cfg=cfg, profile=profile, name=name, dry_run=args.dry_run,
                         max_chunks=args.max_chunks)
        if args.json:
            print(json.dumps(report))
        else:
            print(f"{report['messages']} messages in {report['chunks']} chunk(s)"
                  f"{' (truncated)' if report['truncated'] else ''}; {report['malformed_lines']} malformed "
                  f"line(s); {report['extracted']} fact(s) extracted")
            for item in report["filed"]:
                print(f"  filed {item['id']}  {item['title']}")
            for item in report["facts"]:
                print(f"  would file: {item['title']}")
            for item in report["skipped"]:
                print(f"  skipped (duplicate of {item['duplicate_of']}): {item['title']}")
            for error in report["errors"]:
                print(f"  error: {error}", file=sys.stderr)
        return 0 if report["ok"] else 1
    except (FileNotFoundError, NotPending, PermissionError, RuntimeError, ValueError, OSError) as exc:
        print(f"mem-inbox: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
