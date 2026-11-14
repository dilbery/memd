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
  * a signed-in account session (memd.access, CSRF-checked) that holds review
    on the store, a capability beside read and write, or the personal
    console's session for its own store;
  * the local ``mem-inbox`` CLI (it already has the store on disk);
  * an agent token issued with review, where its owner holds review; or any
    write token when MEMD_INBOX_TOKEN_REVIEW allows it: ``others`` lets it
    review candidates another credential proposed, ``all`` also its own. The
    default ``off`` leaves only tokens issued with review.

A reviewer who does not also hold write may change a candidate's tags and
importance, nothing else. The approved note is still written: the approval, not
the reviewer's own access, authorises that one save (memd.access.approved_write).

Those callers build a ``Reviewer`` (``reviewer_for``, ``console_reviewer``,
``operator_reviewer``); ``approve`` and ``reject`` take nothing else and apply
the rules themselves. A candidate records the account and credential that
proposed it (memd.actor.caller), and "its own" is decided by those ids; one
without them was filed by a local caller, or before they were recorded, and is
matched by label.

MEMD_SAVE_MODE=inbox makes the MCP and HTTP ``save`` of a remote agent token
file a candidate instead of writing (default ``direct``: save writes as
before). Browser sessions and local callers always save directly.

A store's review policy (``review_policy``) says the same per store, and more:
  * ``open`` (the default): everything above;
  * ``agents``: an agent token's save into this store is filed as a candidate,
    whatever MEMD_SAVE_MODE says;
  * ``four-eyes``: every save and publish into the store through memd is filed,
    a signed-in session's included. A candidate is decided by a reviewer with
    an account other than the one that proposed it, nobody changes more than
    its tags and importance, and MEMD_INBOX_TOKEN_REVIEW gives no token a say.
    The local CLI is the operator and is not bound by it.

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
import logging
import os
import re
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

STATUSES = ("pending", "approved", "rejected")
MAX_TITLE = 500
MAX_BODY = 200000
MAX_PENDING = 1000               # pending candidates per store; a flood is refused
DECIDED_RETENTION_DAYS = 180     # approved/rejected rows are pruned after this
CLAIM_TTL_S = 300                # an approval in progress blocks a second one this long
TOKEN_REVIEW_MODES = ("off", "others", "all")
SAVE_MODES = ("direct", "inbox")
REVIEW_POLICIES = ("open", "agents", "four-eyes")
EDITABLE = ("title", "body", "tags", "importance", "host", "description", "source",
            "observed_at", "verified_at", "pinned", "volatility", "verify", "supersedes",
            "slug", "expected_revision")
# Edits that leave the proposer's text theirs (all that a token reviewer, or one
# without write access to the store, may make).
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


class PolicyRefusal(PermissionError):
    """Refused by the store's review policy, not for want of access.

    A PermissionError, so every caller answers 403. Its message is one of this
    module's own sentences, safe to show where other refusals are reworded.
    """


# --------------------------------------------------------------------------- settings

log = logging.getLogger(__name__)
_warned: set[tuple[str, str]] = set()      # (store, value) of each unknown policy already logged


def token_review_mode(env=None) -> str:
    value = ((env if env is not None else os.environ).get("MEMD_INBOX_TOKEN_REVIEW") or "off").strip().lower()
    return value if value in TOKEN_REVIEW_MODES else "off"


def save_mode(env=None) -> str:
    value = ((env if env is not None else os.environ).get("MEMD_SAVE_MODE") or "direct").strip().lower()
    return value if value in SAVE_MODES else "direct"


def review_policy(store: str, *, config: dict | None = None) -> str:
    """The store's review policy, one of REVIEW_POLICIES.

    Its ``review_policy`` setting (Settings / PUT /admin/stores), else
    ``MEMD_<STORE>_REVIEW_POLICY`` for a store configured by environment, else
    ``open``: the resolution memd.share.publish_review uses. Case does not
    matter and ``four_eyes`` is read as ``four-eyes``. ``config`` is the
    store's configuration, for a caller that already holds it.

    Any other value means ``four-eyes`` and is logged: a mistyped policy must
    not quietly switch review off.
    """
    from memd import control
    if config is None and control.enabled():
        row = control.store(store)
        config = row["config"] if row else None
    value = (config or {}).get("review_policy")
    if value is None or (isinstance(value, str) and not value.strip()):
        from memd.profiles import effective_environment
        key = "MEMD_" + "".join(c if c.isalnum() else "_" for c in store.upper()) + "_REVIEW_POLICY"
        value = (effective_environment().get(key) or "").strip()
        if not value:
            return "open"
    policy = value.strip().lower().replace("_", "-") if isinstance(value, str) else None
    if policy in REVIEW_POLICIES:
        return policy
    seen = (store, repr(value)[:100])
    if seen not in _warned:
        _warned.add(seen)
        log.warning("store %s: review policy %s is not one of %s; treating it as four-eyes",
                    store, seen[1], ", ".join(REVIEW_POLICIES))
    return "four-eyes"


def save_routes_to_inbox(store: str) -> bool:
    """True when this request's `save` into `store` should file a candidate instead of writing.

    A local caller (the CLI, stdio) always saves directly. For a remote one the
    store's review policy decides:
      * ``open``: only in MEMD_SAVE_MODE=inbox, and only for a caller that is
        not a signed-in browser session: an agent token (legacy, registry,
        Kasm, OIDC);
      * ``agents``: such a caller, whatever MEMD_SAVE_MODE says;
      * ``four-eyes``: every caller, a signed-in session included.
    """
    from memd import access
    if not access.remote_request.get():
        return False
    policy = review_policy(store)
    if policy == "four-eyes":
        return True
    principal = access.current.get()
    if principal is not None and principal.session:
        return False
    return policy == "agents" or save_mode() == "inbox"


_BUILT = object()     # held only by the reviewers this module builds


@dataclass(frozen=True)
class Reviewer:
    """Who is deciding a candidate. Built only by reviewer_for, console_reviewer and operator_reviewer.

    ``can_write`` is whether the caller also holds write on the store: without
    it only tags and importance may change. ``store`` is the store the checks
    were made for; the operator's is None, any store.
    """
    label: str
    account: str | None = None
    credential: str | None = None
    is_token: bool = False
    can_write: bool = True
    operator: bool = False
    store: str | None = None
    # Not an init field, so neither Reviewer(...) nor dataclasses.replace carries it.
    _built: object = field(default=None, init=False, repr=False, compare=False)


def _reviewer(label: str, **fields) -> Reviewer:
    made = Reviewer(label, **fields)
    object.__setattr__(made, "_built", _BUILT)
    return made


def _require_reviewer(reviewer, profile: str) -> Reviewer:
    # The checks that make someone a reviewer live in the constructors below; a
    # label or a look-alike object would skip them. They were made for one
    # store, so the reviewer decides nothing in another.
    if type(reviewer) is not Reviewer or reviewer._built is not _BUILT:
        raise PermissionError("A decision needs a reviewer the inbox built (memd.inbox.reviewer_for).")
    if reviewer.store != profile and not (reviewer.operator and reviewer.store is None):
        raise PermissionError("This reviewer was built for another store.")
    return reviewer


_NEEDS_ACCOUNT = ("This store needs a reviewer with an account that holds review on it: sign in, or use a token "
                  "issued with review. MEMD_INBOX_TOKEN_REVIEW does not apply to it.")
_NEEDS_OPERATOR = ("This candidate was filed by a credential that belongs to no account, so no reviewer can be "
                   "shown to be a second one. The operator decides it on the server (mem-inbox).")


def reviewer_for(profile: str) -> Reviewer:
    """The current HTTP/MCP caller as a reviewer of this store, or PermissionError.

    A signed-in account session needs review on the store (memd.access:
    Principal.reviews). So does an agent token, which holds it only when issued
    with review and while its owner holds it; failing that, a token with write
    access may review when MEMD_INBOX_TOKEN_REVIEW is not ``off``, except in a
    ``four-eyes`` store, where that switch gives no one a say. A local caller
    (CLI) is the operator.
    """
    from memd import access
    from memd.actor import caller, get_actor
    from memd.registry import Denied, enforce
    who = caller()
    principal = access.current.get()
    if principal is not None and principal.session:
        if profile not in principal.reviews:
            raise PermissionError("Reviewing the inbox needs review access to this store.")
        return _reviewer(principal.label, account=who.account, credential=who.credential,
                         can_write=principal.grants.get(profile) == "write", store=profile)
    if not access.remote_request.get():
        return _reviewer(get_actor() or "local", operator=True)
    if principal is None or profile not in principal.reviews:
        if review_policy(profile) == "four-eyes":
            # The switch asks only for write and is set for the whole instance.
            # Here a decision has to come from an account that holds review.
            raise PolicyRefusal(_NEEDS_ACCOUNT)
        if token_review_mode() == "off":
            raise PermissionError("Agent tokens cannot review the inbox unless issued with review; sign in "
                                  "with an account (or set MEMD_INBOX_TOKEN_REVIEW).")
        access.authorize(profile, write=True)
        # authorize checks nothing for a caller without a principal. What a
        # registry token may do is the registry's to say, and the switch is for
        # tokens that may save; whoever asks here may have checked only read.
        try:
            enforce("save", profile)
        except Denied as exc:
            raise PermissionError(str(exc)) from exc
    # A caller without a principal (registry token, bound identity) got here
    # through the switch, so it may write the store: by the check above, or as
    # the identity the store belongs to.
    return _reviewer(get_actor() or "token", account=who.account, credential=who.credential, is_token=True,
                     can_write=principal is None or principal.grants.get(profile) == "write", store=profile)


def console_reviewer(store: str) -> Reviewer:
    """The personal console's session as the reviewer of its own store.

    Call it only after the console's guard. That session has no principal: the
    identity its guard bound selects the store and is the account that decides.
    """
    from memd.identity import current_identity
    if not store or current_identity() != store:
        raise PermissionError("The console reviews only the signed-in user's own store.")
    return _reviewer(store, account=f"identity:{store}", credential=f"identity:{store}", store=store)


def operator_reviewer(label: str = "") -> Reviewer:
    """The operator: a local caller that already has the store on disk (mem-inbox)."""
    from memd import access
    if access.remote_request.get() or access.current.get() is not None:
        raise PermissionError("Only a local caller reviews as the operator.")
    return _reviewer(label or "local", operator=True)


def _own_proposal(row: sqlite3.Row, reviewer: Reviewer) -> bool:
    """Whether the reviewer's credential filed this candidate.

    Labels are not unique, so ids decide. A candidate without them was filed by
    a local caller or before ids were recorded: the label is all there is.
    Rotation keeps a token the proposer of its predecessors' candidates.
    """
    from memd.registry import lineage
    if row["proposer_credential"]:
        return row["proposer_credential"] in lineage(reviewer.credential)
    return row["proposer"] == reviewer.label


def _four_eyes(reviewer: Reviewer, profile: str) -> bool:
    """Whether the store's ``four-eyes`` policy binds this reviewer. The operator is exempt."""
    return not reviewer.operator and review_policy(profile) == "four-eyes"


def _require_second_account(row: sqlite3.Row, reviewer: Reviewer) -> None:
    """Under ``four-eyes``: refuse a decision from the account that proposed the candidate.

    The account, not the credential: a user's session and all of their tokens
    are one proposer here, and an administrator is an account like any other.
    A candidate filed by a remote credential that belongs to no account (a
    legacy file token, a registry token that is no one's own) has none to
    compare, and whoever holds that credential may hold a reviewing account
    too, so it is the operator's to decide. A candidate with no ids at all was
    filed on the server, by the operator's tools or before ids were recorded:
    any reviewer with an account may decide it.
    """
    if not reviewer.account:
        raise PolicyRefusal(_NEEDS_ACCOUNT)
    if row["proposer_account"]:
        if row["proposer_account"] == reviewer.account:
            raise PolicyRefusal("This store needs a second reviewer: a candidate cannot be decided from the "
                                "account that proposed it.")
    elif row["proposer_credential"]:
        raise PolicyRefusal(_NEEDS_OPERATOR)


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
    columns = {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}
    if "provenance" not in columns:
        _add_column(conn, "provenance")
    # Who proposed and who decided, by id (memd.actor.caller): labels are not
    # unique. NULL for a local caller and for rows older than the columns.
    for column in ("proposer_account", "proposer_credential", "reviewer_account"):
        if column not in columns:
            _add_column(conn, column)
    return conn


def _add_column(conn: sqlite3.Connection, column: str) -> None:
    try:
        conn.execute(f"ALTER TABLE candidates ADD COLUMN {column} TEXT")
    except sqlite3.OperationalError as exc:
        # Two requests can open an older file at once; the other one added it first.
        if "duplicate column name" not in str(exc):
            raise


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
            provenance: dict | None = None, caller=None) -> dict:
    """File one candidate; returns {ok, id, status, duplicate, lint, ...}.

    ``payload`` takes save's fields (memd.normalize). An identical pending
    candidate (same title, body and provenance) is returned instead of a second row.
    ``provenance`` is set only by memd.share.publish (never from a payload): the
    approved note then carries it as ``published_from``.
    ``caller`` is the remote proposer (memd.actor.caller), whose account and
    credential are recorded beside the ``proposer`` label. A local caller passes
    none and is known by its label alone.

    The stored lint lists every pending candidate the fact restates
    (``pending_duplicates``: id and title) for reviewers. The returned lint is
    the proposer's view of it (``_proposer_lint``); for an identical candidate
    someone else filed, it is the lint of this caller's fact, not the stored one.
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
    account, credential = (caller.account, caller.credential) if caller is not None else (None, None)
    own = _proposer_key(proposer, account, credential)
    path = inbox_path(cfg, profile)
    conn = _connect(path)
    try:
        # Provenance is part of identity: a publish must not fold into an ordinary
        # proposal (or another source's publish) and lose its published_from.
        existing = conn.execute(
            "SELECT * FROM candidates WHERE content_hash=? AND status='pending' AND provenance IS ?",
            (digest, json.dumps(provenance, sort_keys=True) if provenance else None)).fetchone()
        if existing is None:
            pending = conn.execute("SELECT COUNT(*) FROM candidates WHERE status='pending'").fetchone()[0]
            if pending >= MAX_PENDING:
                raise ValueError(f"The inbox already holds {MAX_PENDING} pending candidates; review some first")
    finally:
        conn.close()
    # Every pending text is read, so only after the cheap checks above: a full
    # inbox has refused by now. A handoff replaces its proposer's older pending
    # one rather than repeating it, so handoffs are not compared with each other.
    restated = _restated(title, body, (c for c in pending_candidates(cfg, profile)
                                        if handoff_repo is None or c["source"] != handoff.SOURCE))
    if existing is not None:
        # The stored lint was written for reviewers and for the candidate's own
        # proposer: it shows the slug and the superseded note that proposer
        # asked for. Anyone else is told what its own fact would do.
        if _is_own(existing, own):
            report = json.loads(existing["lint"] or "{}")
        else:
            report = precomputed_lint if precomputed_lint is not None else lint(fact, profile, cfg=cfg)
        return {"ok": True, "proposed": True, "duplicate": True, "id": existing["id"], "status": "pending",
                "lint": _proposer_lint(report, [c for c in restated if c["id"] != existing["id"]], own),
                "message": "An identical candidate is already waiting for review."}
    # Lint outside the file's transaction: it reads Git and may call the model.
    report = precomputed_lint if precomputed_lint is not None else lint(fact, profile, cfg=cfg)
    report = {**report, "pending_duplicates": [{"id": c["id"], "title": c["title"]} for c in restated]}
    cid = secrets.token_hex(8)
    conn = _connect(path)
    try:
        with conn:
            _prune(conn, now)
            conn.execute(
                "INSERT INTO candidates(id,created,source,proposer,title,body,meta,content_hash,lint,status,"
                "provenance,proposer_account,proposer_credential) VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (cid, now, (source or "")[:100], (proposer or "")[:200], title, body,
                 json.dumps(meta, sort_keys=True), digest, json.dumps(report),
                 json.dumps(provenance, sort_keys=True) if provenance else None, account, credential))
            if handoff_repo is not None:
                handoff.replace_pending(conn, handoff_repo, (proposer or "")[:200], cid,
                                        account=account, credential=credential)
    finally:
        conn.close()
    return {"ok": True, "proposed": True, "duplicate": False, "id": cid, "status": "pending",
            "lint": _proposer_lint(report, restated, own),
            "message": "Waiting for review; nothing is saved until a person approves it."}


def _proposer_key(label: str, account: str | None, credential: str | None) -> dict:
    """The column values that make a candidate this caller's own to read back.

    Narrower than ``_own_proposal``. Every session of an account carries the
    same credential, so the account and the credential must both match. A
    caller without ids is a local one, known by its label, and so are its
    candidates: one that has ids is never matched by a label. A rotated
    registry token keeps its predecessors' candidates (memd.registry.lineage),
    so the credential column's value is a tuple.
    """
    from memd.registry import lineage
    if account is None and credential is None:
        return {"proposer_account": None, "proposer_credential": None, "proposer": (label or "")[:200]}
    return {"proposer_account": account, "proposer_credential": lineage(credential) or None}


def _restated(title: str, body: str, waiting) -> list[dict]:
    """The pending candidates that say much the same as this text (distill's check), without their text."""
    mine = _tokens(title + "\n" + body)       # once, not once per candidate
    return [{k: v for k, v in c.items() if k != "body"} for c in waiting
            if _similar_tokens(mine, _tokens(c["title"] + "\n" + c["body"]))]


def _is_own(row, own: dict) -> bool:
    """Whether a candidate row (or dict) has the column values of ``_proposer_key``.

    A tuple value is the credential and its predecessors: any of them matches.
    """
    for column, value in own.items():
        if isinstance(value, tuple):
            if row[column] not in value:
                return False
        else:
            if row[column] != value:
                return False
    return True


def _proposer_lint(report: dict, restated: list[dict], own: dict) -> dict:
    """A lint as the proposer may see it.

    Pending text is its proposer's and the reviewers' to read. The reply names
    the proposer's own pending duplicates and says nothing of anyone else's, not
    even how many: a count would let a proposer test guesses about what others
    have waiting.
    """
    mine = [{"id": c["id"], "title": c["title"]} for c in restated if _is_own(c, own)]
    return {**report, "pending_duplicates": mine}


def queued_receipt(proposed: dict, policy: str = "open") -> dict:
    """A save-shaped receipt for a save that was filed as a candidate.

    ``policy`` is the store's review policy, so the receipt names what queued
    the save: that policy, or MEMD_SAVE_MODE=inbox under ``open``.
    """
    report = proposed.get("lint") or {}
    why = "MEMD_SAVE_MODE=inbox" if policy == "open" else f"this store's review policy is {policy}"
    who = "a reviewer from another account" if policy == "four-eyes" else "a person"
    warnings = [f"Queued for review ({why}): nothing is saved until {who} "
                f"approves candidate {proposed['id']}."] + list(report.get("warnings") or [])
    mine = report.get("pending_duplicates") or []
    if mine:
        warnings.append("Similar candidates are already waiting for review: "
                        + ", ".join(f"{c['id']} ({c['title'][:80]})" for c in mine) + ".")
    if report.get("error"):
        warnings.append(f"Approving it as proposed would fail: {report['error']}")
    return {"ok": True, "queued": True, "inbox_id": proposed["id"], "duplicate": proposed["duplicate"],
            "saved": False, "synced": False, "indexed": False, "lexical_indexed": False,
            "slug": report.get("slug") or "", "action": "proposed", "revision": "",
            "related": list(report.get("related") or []), "conflicts": list(report.get("conflicts") or []),
            "warnings": warnings}


def _proposal(row: sqlite3.Row, *, full: bool = False) -> dict:
    """A candidate as its proposer sees it: what became of it, not the reviewers' lint."""
    out = {"id": row["id"], "status": row["status"], "created": row["created"], "source": row["source"],
           "title": row["title"], "excerpt": row["body"][:240], "decided_at": row["decided_at"],
           "reviewer": row["reviewer"], "reason": row["reason"], "slug": row["slug"],
           "revision": row["revision"], "edited": row["original"] is not None,
           "last_error": row["last_error"]}
    if full:
        out["body"] = row["body"]
    return out


def _own_where(caller) -> tuple[str, tuple]:
    own = _proposer_key(caller.label, caller.account, caller.credential)
    conditions = []
    args = []
    for column, value in own.items():
        if isinstance(value, tuple):
            conditions.append(f"{column} IN ({','.join('?' * len(value))})")
            args.extend(value)
        else:
            conditions.append(f"{column} IS ?")
            args.append(value)
    return " AND ".join(conditions), tuple(args)


def own_proposals(cfg, profile: str, caller, *, status: str = "pending", limit: int = 50,
                  offset: int = 0) -> dict:
    """The candidates this caller (memd.actor.Caller) filed in the store, newest first.

    ``total`` and ``counts`` cover the caller's own candidates only, so they say
    nothing about anyone else's. A decided one is listed until it is pruned.
    """
    if status not in STATUSES + ("all",):
        raise ValueError("status must be pending, approved, rejected or all")
    limit = max(1, min(200, int(limit)))
    # SQLite cannot bind a larger integer, and nothing is listed that far along.
    offset = max(0, min(2 ** 63 - 1, int(offset)))
    path = inbox_path(cfg, profile)
    counts = {s: 0 for s in STATUSES}
    if not path.exists():
        return {"ok": True, "items": [], "total": 0, "counts": counts, "offset": offset}
    own, args = _own_where(caller)
    conn = _connect(path)
    try:
        for s, n in conn.execute(f"SELECT status, COUNT(*) FROM candidates WHERE {own} GROUP BY status", args):
            if s in counts:
                counts[s] = n
        where, args = (own, args) if status == "all" else (own + " AND status=?", (*args, status))
        rows = conn.execute(f"SELECT * FROM candidates WHERE {where} "
                            "ORDER BY COALESCE(decided_at, created) DESC, id LIMIT ? OFFSET ?",
                            (*args, limit, offset)).fetchall()
    finally:
        conn.close()
    return {"ok": True, "items": [_proposal(r) for r in rows],
            "total": sum(counts.values()) if status == "all" else counts[status],
            "counts": counts, "offset": offset}


def own_proposal(cfg, profile: str, caller, candidate_id: str) -> dict:
    """One of the caller's own candidates, with its full text.

    Someone else's candidate raises exactly what a missing one does, so an id
    tells a caller nothing about a candidate it did not file. A caller can hold
    such an id (an identical proposal is answered with the id of the candidate
    already waiting), hence "among your proposals".
    """
    _check_id(candidate_id)
    path = inbox_path(cfg, profile)
    row = None
    if path.exists():
        own, args = _own_where(caller)
        conn = _connect(path)
        try:
            row = conn.execute(f"SELECT * FROM candidates WHERE id=? AND {own}", (candidate_id, *args)).fetchone()
        finally:
            conn.close()
    if row is None:
        raise FileNotFoundError("Candidate not found among your proposals")
    return _proposal(row, full=True)


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


def approve(cfg, profile: str, candidate_id: str, *, reviewer: Reviewer, edits: dict | None = None,
            now: float | None = None, save_fn=None) -> dict:
    """Write the candidate (with edits) through the real save path; record the receipt.

    Raises NotPending when it was already decided (or is being approved),
    PermissionError when the reviewer was not built here for this store, is a
    token that may not review it, or changes more than it may (PolicyRefusal
    when the store's ``four-eyes`` policy is what refuses), and whatever save
    raises (the candidate then stays pending with ``last_error`` set).
    """
    from memd import access
    from memd.normalize import normalize_fact
    from memd.actor import set_actor
    _require_reviewer(reviewer, profile)
    strict = _four_eyes(reviewer, profile)
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
        if strict:
            _require_second_account(row, reviewer)
        elif reviewer.is_token and token_review_mode() != "all" and _own_proposal(row, reviewer):
            raise PermissionError("A token cannot approve its own proposal "
                                  "(MEMD_INBOX_TOKEN_REVIEW=all allows it)")
        fact = normalize_fact(_merge(row, edits))
        fact.pop("profile", None)
        proposed = normalize_fact(_merge(row, None))
        proposed.pop("profile", None)
        rewritten = {k for k in set(fact) | set(proposed) if fact.get(k) != proposed.get(k)} - METADATA_EDITS
        if strict and rewritten:
            # Whoever holds write, and whatever MEMD_INBOX_TOKEN_REVIEW says: a
            # reviewer who rewrote the text would be approving their own words.
            raise PolicyRefusal("In this store a reviewer may change only " + " and ".join(sorted(METADATA_EDITS))
                                + "; reject the candidate with a reason to have its text corrected.")
        if reviewer.is_token and rewritten and token_review_mode() != "all":
            # Otherwise one token could rewrite another's candidate and approve it alone.
            raise PermissionError("A token reviewer may change only " + " and ".join(sorted(METADATA_EDITS))
                                  + "; other edits need a signed-in reviewer")
        if rewritten and not reviewer.can_write:
            # Otherwise review would be a way to write: propose anything, then rewrite it here.
            raise PermissionError("A reviewer without write access to this store may change only "
                                  + " and ".join(sorted(METADATA_EDITS)))
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
    author = reviewer.label if reviewer.is_token and rewritten else (row["proposer"] or reviewer.label)
    context = contextvars.copy_context()

    from memd.save import publishing
    provenance = json.loads(row["provenance"]) if row["provenance"] else None

    def _write():
        set_actor(author)
        # The decision above is what authorises this save, whether or not the
        # reviewer may write. Set in the copy only, so it ends with this call.
        access.approved_write.set(profile)
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
                "UPDATE candidates SET status='approved', reviewer=?, reviewer_account=?, decided_at=?,"
                " slug=?, revision=?, receipt=?, title=?, body=?, meta=?, original=?, claimed_at=NULL,"
                " last_error=NULL WHERE id=?",
                (reviewer.label[:200], reviewer.account, now, receipt.get("slug"), receipt.get("revision"),
                 json.dumps(receipt), title, body, json.dumps(meta, sort_keys=True),
                 json.dumps(original) if original else None, candidate_id))
    finally:
        conn.close()
    _audit("inbox.approve", reviewer, profile, candidate_id)
    return {"ok": True, "id": candidate_id, "status": "approved", "edited": original is not None,
            "receipt": receipt}


def reject(cfg, profile: str, candidate_id: str, *, reviewer: Reviewer, reason: str = "",
           now: float | None = None) -> dict:
    _require_reviewer(reviewer, profile)
    strict = _four_eyes(reviewer, profile)
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
        if strict:
            _require_second_account(row, reviewer)
        elif reviewer.is_token and token_review_mode() != "all" and _own_proposal(row, reviewer):
            raise PermissionError("A token cannot review its own proposal "
                                  "(MEMD_INBOX_TOKEN_REVIEW=all allows it)")
        with conn:
            changed = conn.execute(
                "UPDATE candidates SET status='rejected', reviewer=?, reviewer_account=?, decided_at=?,"
                " reason=? WHERE id=? AND status='pending' AND (claimed_at IS NULL OR claimed_at < ?)",
                (reviewer.label[:200], reviewer.account, now, reason.strip(), candidate_id,
                 now - CLAIM_TTL_S)).rowcount
        if not changed:
            raise NotPending("This candidate is being approved")
    finally:
        conn.close()
    _audit("inbox.reject", reviewer, profile, candidate_id)
    return {"ok": True, "id": candidate_id, "status": "rejected"}


def _audit(action: str, reviewer: Reviewer, profile: str, candidate_id: str) -> None:
    from memd import control
    control.audit_decision(action, reviewer.label, f"{profile}:{candidate_id}")


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


def pending_candidates(cfg, profile: str):
    """Each pending candidate in turn, oldest first, with its text and who proposed it.

    For propose's pending-duplicate lint, which names a match only to its own
    proposer: id, title, body, source, proposer (label), proposer_account and
    proposer_credential. Every proposal reads them all, and a full inbox holds
    hundreds of megabytes of text, so one body is in memory at a time.
    """
    path = inbox_path(cfg, profile)
    if not path.exists():
        return
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT id, title, source, proposer, proposer_account, proposer_credential "
            "FROM candidates WHERE status='pending' ORDER BY created, id").fetchall()
        for row in rows:
            # A query of its own, so no read lock is held while the caller compares the text.
            found = conn.execute("SELECT body FROM candidates WHERE id=? AND status='pending'",
                                 (row["id"],)).fetchone()
            if found is not None:       # else it was decided after the list was read
                yield {**dict(row), "body": found["body"]}
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
    return _similar_tokens(_tokens(a), _tokens(b))


def _similar_tokens(ta: set[str], tb: set[str]) -> bool:
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
    for key in ("related", "near_duplicates", "pending_duplicates", "conflicts", "warnings"):
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
    try:
        reviewer = operator_reviewer(getattr(args, "reviewer", None) or f"cli:{os.environ.get('USER') or 'local'}")
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
                    if lint_report.get("pending_duplicates"):
                        flags.append("pending-duplicate")
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
