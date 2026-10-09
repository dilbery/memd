"""Timeline-aware memory: time-bounded facts derived from notes (mem-facts).

A fact is (subject, predicate, object, valid_from, valid_to) plus the slug and
git blob of the note it came from, e.g. "nginx | runs on | vmhost |
2026-08-19 | (open)". With facts, "what is the current X" and "what was X on
date D" are exact lookups (timeline()) instead of search guesses.

Facts are a DERIVED, rebuildable cache in the index DB (tables facts and
fact_sources, schema in memd.index, versioned by _FACTS_VERSION). The Markdown
notes stay authoritative and are never written. Extraction is a batch job, never
on the save or recall hot path:

  * a cheap deterministic pass for explicit phrasings: "X moved/migrated to Y",
    "X was replaced by Y", "X runs on/uses/points to Y since|until DATE",
    "since DATE, X runs on Y" (ISO dates only);
  * when MEMD_LLM_URL is set, a chat model (memd.llm) asked for a strict JSON
    schema; at most --max-notes model calls per run, newest notes first.

A fact without a date in the text takes the note's observed_at, then
verified_at, then the date in its title/slug. fact_sources records which blob
of each note was extracted and whether the model took part, so a re-run
touches only changed notes (and notes the model has not seen yet).

Temporal closing (close_facts) is recomputed over every row after each run:
a fact's valid_to is the valid_from of the earliest later fact with the same
subject and predicate and a different object (or the date the text itself gave
with "until", whichever is earlier). Nothing is deleted. Undated facts neither
close nor are closed. A note whose every fact was closed by facts from OTHER
notes is reported as a candidate to supersede; it is never superseded here.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from memd.config import Config
from memd.hosts import canonical_host
from memd.llm import LLMError, chat, enabled
from memd.staleness import _parse_date, as_of
from memd.store import RETRACTED, Note, list_notes

MAX_NOTES = 20            # model calls per run (--max-notes)
MAX_FACTS = 20            # facts kept per note
NOTE_CHARS = 4000         # body characters per note in the prompt
SUBJECT_CHARS = 120
PREDICATE_CHARS = 60
OBJECT_CHARS = 160
BLOCK_LIMIT = 8           # facts in recall's "current facts" block

_ISO = r"20\d\d-\d\d-\d\d"
_ISO_RE = re.compile(rf"^{_ISO}$")
_FENCE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)

# Relation phrasings folded to one predicate, so a pattern fact ("moved to") and
# a model fact ("runs on") about the same thing close each other.
_PREDICATES = {
    "runs on": "runs on", "running on": "runs on", "is running on": "runs on",
    "hosted on": "runs on", "is hosted on": "runs on", "lives on": "runs on",
    "deployed on": "runs on", "is deployed on": "runs on", "deployed to": "runs on",
    "moved to": "runs on", "migrated to": "runs on", "was moved to": "runs on",
    "was migrated to": "runs on", "host": "runs on", "hosted by": "runs on",
    "uses": "uses", "is using": "uses", "using": "uses",
    "points to": "points to", "resolves to": "resolves to", "listens on": "listens on",
    "is at": "located at", "located at": "located at", "is located at": "located at",
    "ip": "ip address", "ip address": "ip address", "has ip": "ip address",
    "has ip address": "ip address", "address": "ip address",
    "version": "version", "is at version": "version", "runs version": "version",
    "has version": "version",
    "replaced by": "replaced by", "was replaced by": "replaced by",
    "is replaced by": "replaced by", "has been replaced by": "replaced by",
    "owned by": "owned by", "is owned by": "owned by",
}
_PRONOUNS = frozenset({"it", "this", "that", "we", "i", "they", "he", "she", "you",
                       "there", "which", "who", "then", "also", "now"})


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _clean(value) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("`", "").replace("**", "").split()).strip(" .,;:'\"")


def entity_key(value: str) -> str:
    """Light key for a subject or object: casefolded, trimmed, hosts canonical."""
    text = _clean(value).casefold()
    if text.startswith("the "):
        text = text[4:]
    if text and " " not in text:
        text = canonical_host(text)
    return text


def predicate_key(value: str) -> str:
    text = _clean(value).casefold()
    return _PREDICATES.get(text, text)


def note_date(note: Note) -> dt.date | None:
    """When the note's facts were true: observed_at, verified_at, then title/slug."""
    for value in (note.observed_at, note.verified_at):
        d = _parse_date(value)
        if d is not None:
            return d
    try:
        return as_of(note.to_dict())
    except Exception:
        return None


def _iso(value) -> str | None:
    if isinstance(value, str) and _ISO_RE.match(value.strip()):
        try:
            return dt.date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class Fact:
    subject: str
    predicate: str
    object: str
    valid_from: str | None = None
    stated_to: str | None = None
    method: str = "pattern"

    @property
    def keys(self) -> tuple[str, str, str]:
        return entity_key(self.subject), predicate_key(self.predicate), entity_key(self.object)


def _fact(subject, predicate, obj, valid_from, stated_to, method) -> Fact | None:
    subject, predicate, obj = _clean(subject), _clean(predicate), _clean(obj)
    if not subject or not predicate or not obj:
        return None
    if len(subject) > SUBJECT_CHARS or len(predicate) > PREDICATE_CHARS or len(obj) > OBJECT_CHARS:
        return None
    if subject.casefold().split()[0] in _PRONOUNS:
        return None
    valid_from, stated_to = _iso(valid_from), _iso(stated_to)
    if valid_from and stated_to and stated_to <= valid_from:
        stated_to = None
    fact = Fact(subject, predicate, obj, valid_from, stated_to, method)
    s, p, o = fact.keys
    return fact if s and p and o and s != o else None


def _dedup(facts: list[Fact]) -> list[Fact]:
    seen: set = set()
    out: list[Fact] = []
    for f in facts:
        key = (*f.keys, f.valid_from, f.stated_to)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out[:MAX_FACTS]


# ---------------------------------------------------------------------------
# Deterministic pass
# ---------------------------------------------------------------------------

_TOK = r"[A-Za-z0-9][\w.\-/:@]*"
_STOP = r"(?:on|since|until|from|in|at|because|and|after|before|when|with|for|by|as|to|but|so|which|while)\b"
# Lazy, so "nginx was moved" keeps "was" out of the subject.
_SUBJECT = rf"(?P<subject>{_TOK}(?:\s+{_TOK}){{0,3}}?)"
_OBJECT = rf"(?P<object>{_TOK}(?:\s+(?!{_STOP}){_TOK}){{0,2}})"
_LEAD = re.compile(rf"^(?:on|since|as of|from)\s+(?P<date>{_ISO})\s*,?\s+", re.I)
_MOVE = re.compile(
    rf"^{_SUBJECT}\s+(?:was\s+|has\s+been\s+|is\s+|got\s+)?(?P<verb>moved|migrated)\s+"
    rf"(?:from\s+{_TOK}(?:\s+{_TOK})?\s+)?to\s+{_OBJECT}", re.I)
_REPLACED = re.compile(
    rf"^{_SUBJECT}\s+(?:was|has\s+been|is|got)\s+replaced\s+(?:with|by)\s+{_OBJECT}", re.I)
_STATE = re.compile(
    rf"^{_SUBJECT}\s+(?P<verb>runs\s+on|is\s+running\s+on|is\s+hosted\s+on|lives\s+on|"
    rf"is\s+deployed\s+on|uses|is\s+using|points\s+to|resolves\s+to|listens\s+on|is\s+at|"
    rf"is\s+located\s+at)\s+{_OBJECT}", re.I)
_SINCE = re.compile(rf"\b(?:since|from|as\s+of)\s+(?P<date>{_ISO})\b", re.I)
_UNTIL = re.compile(rf"\b(?:until|till|through)\s+(?P<date>{_ISO})\b", re.I)
_ON = re.compile(rf"\b(?:on|in|at)?\s*(?P<date>{_ISO})\b", re.I)
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+|^#+\s+")
# A time or a plain number is not a place something moved to; an IP address is.
_NUMERIC = re.compile(r"^(?:\d{1,2}(?::\d\d)+(?:\s*(?:am|pm))?|\d+(?:[.,]\d+)?)$", re.I)


def pattern_facts(body: str, default_date: str | None = None) -> list[Fact]:
    """Facts from explicit dated phrasings; conservative by design."""
    out: list[Fact] = []
    for raw in _SENTENCES.split(body or ""):
        sentence = _BULLET.sub("", raw.replace("`", "").replace("**", "")).strip()
        if not sentence or len(sentence) > 400:
            continue
        lead = _LEAD.match(sentence)
        clause = sentence[lead.end():] if lead else sentence
        since, until = _SINCE.search(sentence), _UNTIL.search(sentence)
        start = (lead.group("date") if lead else None) or (since.group("date") if since else None)
        stated_to = until.group("date") if until else None
        for rx, predicate in ((_MOVE, "runs on"), (_REPLACED, "replaced by"), (_STATE, None)):
            m = rx.match(clause)
            if m is None:
                continue
            obj = m.group("object")
            if _NUMERIC.match(obj):
                break
            if predicate is None:
                # A plain state sentence is a fact only when the text dates it.
                if not (start or stated_to):
                    break
                predicate = " ".join(m.group("verb").split())
            when = start
            if when is None:
                # A bare or "on" date dates the change; an "until" date ends it.
                rest = _UNTIL.sub(" ", clause[m.end():])
                on = _ON.search(rest) or _ON.search(clause[:m.start("object")])
                when = on.group("date") if on else None
            fact = _fact(m.group("subject"), predicate, obj, when or default_date,
                         stated_to, "pattern")
            if fact is not None:
                out.append(fact)
            break
    return _dedup(out)


# ---------------------------------------------------------------------------
# Model pass
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You extract time-bounded facts from ONE note of a shared memory store about \
computers, services and configuration.

Rules:
- Only facts about state that can change: what runs where, versions, addresses, \
ports, what uses or points to what, ownership, replacements.
- subject: the thing the fact is about, a short name as written (a service, \
host, device or setting). object: its value, as written.
- predicate: a short lowercase relation; prefer one of: runs on, version, \
ip address, port, uses, points to, located at, owned by, replaced by, \
configured as.
- valid_from: the ISO date (YYYY-MM-DD) the fact became true ONLY if the note \
states it, else null. valid_to: the ISO date it stopped being true ONLY if \
stated, else null. Never guess a date.
- State only facts found in the note. At most 20 facts; none is a valid answer.

Answer with JSON only, no prose and no code fence:
{"facts": [{"subject": "...", "predicate": "...", "object": "...", \
"valid_from": null, "valid_to": null}]}
"""

FACT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["facts"],
    "properties": {"facts": {"type": "array", "maxItems": MAX_FACTS, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["subject", "predicate", "object", "valid_from", "valid_to"],
        "properties": {
            "subject": {"type": "string"}, "predicate": {"type": "string"},
            "object": {"type": "string"},
            "valid_from": {"type": ["string", "null"]}, "valid_to": {"type": ["string", "null"]},
        },
    }}},
}
RESPONSE_FORMAT = {"type": "json_schema",
                   "json_schema": {"name": "facts", "strict": True, "schema": FACT_SCHEMA}}


class FactParseError(ValueError):
    """The model's reply is not a JSON object with a facts list."""


def build_messages(note: Note, *, note_chars: int = NOTE_CHARS) -> list[dict]:
    d = note_date(note)
    body = note.body if len(note.body) <= note_chars else note.body[:note_chars] + "\n[... truncated]"
    user = (f"### [{note.slug}] {note.title}\n"
            f"note date: {d.isoformat() if d else 'undated'}\n\n{body}\n")
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def parse_facts(text: str, default_date: str | None = None) -> list[Fact]:
    """Validate a model reply; malformed items are dropped, a malformed reply raises."""
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
    out: list[Fact] = []
    for item in data["facts"]:
        if not isinstance(item, dict):
            continue
        fact = _fact(item.get("subject"), item.get("predicate"), item.get("object"),
                     _iso(item.get("valid_from")) or default_date, item.get("valid_to"), "llm")
        if fact is not None:
            out.append(fact)
    return _dedup(out)


def model_facts(note: Note, cfg: Config) -> list[Fact]:
    """One chat completion for one note. Raises LLMError or FactParseError."""
    d = note_date(note)
    messages = build_messages(note)
    try:
        reply = chat(messages, cfg=cfg, max_tokens=1500, temperature=0.0,
                     response_format=RESPONSE_FORMAT)
    except LLMError as e:
        # Not every OpenAI-compatible server accepts json_schema; the prompt
        # still asks for JSON and parse_facts validates it either way.
        if "HTTP 400" not in str(e) and "HTTP 422" not in str(e):
            raise
        reply = chat(messages, cfg=cfg, max_tokens=1500, temperature=0.0)
    return parse_facts(reply, d.isoformat() if d else None)


# ---------------------------------------------------------------------------
# Storage and closing
# ---------------------------------------------------------------------------


def _insert(db, note: Note, facts: list[Fact]) -> None:
    for f in facts:
        s, p, o = f.keys
        db.execute(
            "INSERT INTO facts(slug,git_blob,subject,predicate,object,subject_key,predicate_key,"
            "object_key,valid_from,stated_to,valid_to,closed_by,method) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
            (note.slug, note.git_blob, f.subject, f.predicate, f.object, s, p, o,
             f.valid_from, f.stated_to, f.stated_to, f.method))


def close_facts(db) -> int:
    """Recompute every fact's valid_to/closed_by; returns how many are closed.

    For each (subject, predicate), a dated fact is closed by the earliest fact
    dated strictly later with a different object; a stated "until" date wins
    when it is earlier. Idempotent, and nothing is ever deleted.
    """
    groups: dict[tuple[str, str], list[tuple]] = {}
    for row in db.execute("SELECT id, subject_key, predicate_key, object_key, valid_from, "
                          "stated_to, valid_to, closed_by FROM facts"):
        groups.setdefault((row[1], row[2]), []).append(row)
    closed = 0
    for rows in groups.values():
        dated = sorted((r for r in rows if r[4]), key=lambda r: (r[4], r[0]))
        for fid, _s, _p, obj, start, stated, old_to, old_by in rows:
            closer = None
            if start:
                closer = next((g for g in dated if g[4] > start and g[3] != obj), None)
            valid_to, closed_by = stated, None
            if closer is not None and (stated is None or closer[4] <= stated):
                valid_to, closed_by = closer[4], closer[0]
            if (valid_to, closed_by) != (old_to, old_by):
                db.execute("UPDATE facts SET valid_to=?, closed_by=? WHERE id=?",
                           (valid_to, closed_by, fid))
            closed += valid_to is not None
    return closed


def supersede_candidates(db) -> list[dict]:
    """Notes whose every fact was closed by newer facts from other notes."""
    rows = db.execute(
        "SELECT f.slug, f.closed_by, c.slug FROM facts f "
        "LEFT JOIN facts c ON c.id = f.closed_by ORDER BY f.slug").fetchall()
    by_slug: dict[str, list[str | None]] = {}
    for slug, closed_by, closer_slug in rows:
        by_slug.setdefault(slug, []).append(
            closer_slug if closed_by is not None and closer_slug != slug else None)
    return [{"slug": slug, "facts": len(closers), "closed_by": sorted(set(closers))}
            for slug, closers in sorted(by_slug.items())
            if closers and all(closers)]


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@dataclass
class Extraction:
    note: Note
    facts: list[Fact] = field(default_factory=list)
    llm: bool = False
    error: str | None = None


def _live(notes: list[Note]) -> list[Note]:
    return [n for n in notes if not n.superseded_by and n.slug != RETRACTED
            and n.metadata.get("kind") != "summary"]


def _newest_first(notes: list[Note]) -> list[Note]:
    return sorted(notes, key=lambda n: (note_date(n) is None,
                                        -(note_date(n) or dt.date.min).toordinal(), n.slug))


def run(cfg: Config, *, dry_run: bool = False, max_notes: int = MAX_NOTES,
        use_llm: bool = True) -> dict:
    """Extract facts for changed notes, close them, and report; dry_run stores no facts.

    The keyword index is brought current first. Model calls happen before any
    fact write transaction, so the index is never locked across network I/O. The apply phase is one transaction, rolled back
    under dry_run after the report (closing, candidates) is computed.
    """
    from memd.index import open_db
    from memd.refresh import ensure_lexical

    # Keyword data only (no embedding): timeline serves facts of notes the index
    # knows are live, and a conflicted tree is refused here before extraction.
    ensure_lexical(cfg)
    notes = _live(list_notes(Path(cfg.clone)))
    live = {n.slug: n for n in notes}
    model_on = use_llm and enabled(cfg)
    db = open_db(cfg.db, dim=cfg.embed_dim)
    try:
        have = {r[0]: (r[1], bool(r[2])) for r in db.execute(
            "SELECT slug, git_blob, llm FROM fact_sources")}
        stale = [n for n in notes if have.get(n.slug, (None,))[0] != n.git_blob]
        want_model = [n for n in notes if model_on and not
                      (have.get(n.slug) == (n.git_blob, True))]
        budget = max(0, int(max_notes))
        model_todo = _newest_first(want_model)[:budget]
        deferred = len(want_model) - len(model_todo)
        todo = {n.slug: n for n in stale}
        todo.update({n.slug: n for n in model_todo})
        model_slugs = {n.slug for n in model_todo}
        extractions: list[Extraction] = []
        for note in _newest_first(list(todo.values())):
            d = note_date(note)
            out = Extraction(note, pattern_facts(note.body, d.isoformat() if d else None))
            if note.slug in model_slugs:
                try:
                    out.facts = _dedup(model_facts(note, cfg) + out.facts)
                    out.llm = True
                except (LLMError, FactParseError) as e:
                    out.error = str(e)
            extractions.append(out)
        gone = sorted(set(have) - set(live))

        db.execute("BEGIN IMMEDIATE")
        try:
            for slug in gone:
                db.execute("DELETE FROM facts WHERE slug=?", (slug,))
                db.execute("DELETE FROM fact_sources WHERE slug=?", (slug,))
            now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            for ex in extractions:
                db.execute("DELETE FROM facts WHERE slug=?", (ex.note.slug,))
                _insert(db, ex.note, ex.facts)
                db.execute("INSERT INTO fact_sources(slug,git_blob,llm,extracted_at,error) "
                           "VALUES (?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET "
                           "git_blob=excluded.git_blob, llm=excluded.llm, "
                           "extracted_at=excluded.extracted_at, error=excluded.error",
                           (ex.note.slug, ex.note.git_blob, int(ex.llm), now, ex.error))
            close_facts(db)
            total = db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            current = db.execute("SELECT COUNT(*) FROM facts WHERE valid_to IS NULL").fetchone()[0]
            candidates = supersede_candidates(db)
            ids = {ex.note.slug for ex in extractions}
            rows = _fact_rows(db, "f.slug IN (SELECT value FROM json_each(?))",
                              (json.dumps(sorted(ids)),)) if ids else []
        except BaseException:
            db.rollback()
            raise
        if dry_run:
            db.rollback()
        else:
            db.commit()
    finally:
        db.close()
    stored: dict[str, list[dict]] = {}
    for row in rows:
        stored.setdefault(row["source"], []).append(row)
    return {
        "dry_run": dry_run,
        "model": bool(model_on),
        "notes": len(notes),
        "extracted": [{"slug": ex.note.slug, "llm": ex.llm, "error": ex.error,
                       "facts": stored.get(ex.note.slug, [])} for ex in extractions],
        "deferred": deferred,
        "pruned": gone,
        "facts": total,
        "current": current,
        "supersede": candidates,
    }


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

_SELECT = ("SELECT f.subject, f.predicate, f.object, f.valid_from, f.valid_to, f.slug, "
           "f.git_blob, f.method, c.slug, f.subject_key, f.predicate_key, f.object_key, "
           "n.git_blob FROM facts f LEFT JOIN facts c ON c.id = f.closed_by "
           "LEFT JOIN notes n ON n.slug = f.slug ")


def _fact_rows(db, where: str, params: tuple, *, live_only: bool = False) -> list[dict]:
    sql = _SELECT + "WHERE " + where
    if live_only:
        sql += " AND n.slug IS NOT NULL AND n.superseded_by IS NULL"
    sql += " ORDER BY f.predicate_key, COALESCE(f.valid_from, '') DESC, f.slug, f.id"
    out = []
    for r in db.execute(sql, params):
        out.append({
            "subject": r[0], "predicate": r[10], "object": r[2],
            "valid_from": r[3], "valid_to": r[4], "current": r[4] is None,
            "source": r[5], "revision": r[6], "method": r[7], "closed_by": r[8],
            "note_changed": r[12] is not None and r[12] != r[6],
        })
    return out


def _date_arg(value) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return (value.date() if isinstance(value, dt.datetime) else value).isoformat()
    try:
        return dt.date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        raise ValueError(f"at must be an ISO date (YYYY-MM-DD), got {value!r}") from None


def query_facts(db, subject: str, predicate: str | None = None,
                at: str | None = None) -> tuple[str, list[dict]]:
    """(match, facts): current facts, or facts valid on ``at``, for a subject."""
    key = entity_key(subject or "")
    if not key:
        raise ValueError("subject is required")
    at = _date_arg(at)
    cond, params = [], []
    if predicate and predicate_key(predicate):
        cond.append("f.predicate_key = ?")
        params.append(predicate_key(predicate))
    if at is None:
        cond.append("f.valid_to IS NULL")
    else:
        cond.append("f.valid_from IS NOT NULL AND f.valid_from <= ? "
                    "AND (f.valid_to IS NULL OR f.valid_to > ?)")
        params += [at, at]
    extra = " AND " + " AND ".join(cond)
    rows = _fact_rows(db, "f.subject_key = ?" + extra, (key, *params), live_only=True)
    if rows:
        return "exact", rows
    # A subject named more briefly than it was written ("nginx" for "nginx proxy").
    pattern = re.compile(rf"(?<![\w.-]){re.escape(key)}(?![\w.-])")
    subjects = [s for (s,) in db.execute("SELECT DISTINCT subject_key FROM facts")
                if s != key and pattern.search(s)]
    if not subjects:
        return "none", []
    rows = _fact_rows(db, "f.subject_key IN (SELECT value FROM json_each(?))" + extra,
                      (json.dumps(subjects), *params), live_only=True)
    return ("partial" if rows else "none"), rows


def fact_line(f: dict) -> str:
    span = f"{f['valid_from'] or 'undated'} .. {f['valid_to'] or 'now'}"
    return f"- {f['subject']} | {f['predicate']} | {f['object']} ({span}; {f['source']})"


def timeline(subject: str, predicate: str | None = None, at=None, *,
             profile: str = "amber", cfg: Config) -> dict:
    """Current facts about ``subject`` (valid_to open), or facts valid on ``at``."""
    from memd.index import open_db
    from memd.profiles import guard_paths

    clone, db_path = guard_paths(profile, cfg.clone, cfg.db)
    cfg = dataclasses.replace(cfg, clone=clone, db=db_path, profile=profile)
    at = _date_arg(at)
    db = open_db(cfg.db, dim=cfg.embed_dim)
    try:
        match, facts = query_facts(db, subject, predicate, at)
    finally:
        db.close()
    when = f"on {at}" if at else "now"
    if facts:
        text = f"Facts about {subject} {when} (subject | predicate | object (valid from .. to; source)):\n" \
            + "\n".join(fact_line(f) for f in facts)
    else:
        text = (f"No recorded facts about {subject} {when}. Facts come from the mem-facts "
                "job; use recall for notes that mention it.")
    return {"ok": True, "profile": profile, "subject": subject, "predicate": predicate,
            "at": at, "match": match, "facts": facts, "text": text}


# ---------------------------------------------------------------------------
# Recall's "current facts" block (read-only, never ranking)
# ---------------------------------------------------------------------------


def current_fact_rows(db_path, query: str, *, limit: int = BLOCK_LIMIT) -> list[dict]:
    """Current facts whose subject the query names, as fact dicts; [] if none.

    Opens the index read-only and never creates or migrates it: an index without
    fact tables, or any failure, simply yields no facts.
    """
    path = Path(db_path)
    if not path.is_file():
        return []
    # The subjects a query can name: its word n-grams, keyed like subjects are,
    # so the lookup is an indexed IN rather than a scan of every subject.
    words = [w.strip(".,;:!?'\"") for w in re.findall(r"[\w.\-/:@']+", query.casefold())]
    words = [w for w in words if w]
    names = {entity_key(" ".join(words[i:i + n]))
             for n in range(1, 7) for i in range(len(words) - n + 1)}
    names = sorted(n for n in names if len(n) >= 3)
    if not names:
        return []
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.5)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='facts'").fetchone():
            return []
        # Only facts the note still states: an edit since extraction may have changed them.
        return _fact_rows(db, "f.valid_to IS NULL AND f.subject_key IN (SELECT value FROM json_each(?)) "
                              "AND n.git_blob = f.git_blob",
                          (json.dumps(names),), live_only=True)[:limit]
    finally:
        db.close()


def current_facts_block(db_path, query: str, *, limit: int = BLOCK_LIMIT) -> tuple[str, int]:
    """A compact block of current facts whose subject the query names; ("", 0) if none."""
    rows = current_fact_rows(db_path, query, limit=limit)
    if not rows:
        return "", 0
    lines = [f"- {f['subject']} | {f['predicate']} | {f['object']} "
             f"(since {f['valid_from'] or 'undated'}; {f['source']})" for f in rows]
    return "### Current facts (timeline)\n" + "\n".join(lines), len(rows)


def append_current_facts(shaped: dict, *, query: str, db_path, max_chars) -> dict:
    """Append the current-facts block to rendered recall text when it fits.

    Only for present-state queries (recall.CURRENT_INTENT). Ranking and the
    rendered notes are untouched; the block is added after them only if the
    whole text stays within max_chars. Any failure leaves ``shaped`` as it was.
    """
    try:
        from memd.recall import CURRENT_INTENT
        from memd.render import DEFAULT_MAX_CHARS, _number

        if not query or not CURRENT_INTENT.search(query):
            return shaped
        block, count = current_facts_block(db_path, query)
        if not count:
            return shaped
        budget = _number(max_chars, DEFAULT_MAX_CHARS, 256, 100000)
        text = shaped.get("text") or ""
        joined = f"{text}\n\n{block}" if text else block
        if len(joined) <= budget:
            shaped["text"] = joined
            shaped["current_facts"] = count
    except Exception:
        pass
    return shaped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_report(report: dict, *, show_facts: bool) -> None:
    for ex in report["extracted"]:
        how = "model+patterns" if ex["llm"] else "patterns"
        line = f"extract  {ex['slug']}: {len(ex['facts'])} fact(s) ({how})"
        if ex["error"]:
            line += f" MODEL SKIPPED: {ex['error']}"
        print(line)
        if show_facts:
            for f in ex["facts"]:
                print("    " + fact_line(f)[2:])
    for slug in report["pruned"]:
        print(f"prune    {slug} (no longer a live note)")
    if report["deferred"]:
        print(f"deferred {report['deferred']} note(s) over --max-notes; the next run continues")
    if report["supersede"]:
        print("\nsupersede candidates (every fact closed by newer notes; review, never automatic):")
        for c in report["supersede"]:
            print(f"  {c['slug']} ({c['facts']} fact(s)) closed by {', '.join(c['closed_by'])}")
    verb = "would hold" if report["dry_run"] else "holds"
    print(f"\n{len(report['extracted'])} note(s) extracted; the index {verb} {report['facts']} "
          f"fact(s), {report['current']} current." + (" dry-run: nothing written." if report["dry_run"] else ""))
    if not report["model"]:
        print("chat model off (set MEMD_LLM_URL): deterministic patterns only.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="mem-facts",
        description="Extract time-bounded facts from notes into the index (derived; notes untouched).")
    ap.add_argument("--dry-run", action="store_true",
                    help="extract and report, including supersede candidates; write nothing")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    ap.add_argument("--max-notes", type=int, default=MAX_NOTES,
                    help=f"chat model calls per run (default {MAX_NOTES})")
    ap.add_argument("--no-llm", action="store_true", help="deterministic patterns only")
    ap.add_argument("--subject", help="instead of extracting, print the timeline of this subject")
    ap.add_argument("--predicate", help="with --subject: only this predicate")
    ap.add_argument("--at", help="with --subject: facts valid on this date (YYYY-MM-DD)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    cfg = Config.from_env()
    if cfg.clone is None or cfg.db is None:
        print("mem-facts: no clone/index configured (MEMD_CLONE / MEMD_DB / MEMD_PROFILE)",
              file=sys.stderr)
        return 2
    if args.subject:
        try:
            out = timeline(args.subject, args.predicate, args.at, profile=cfg.profile, cfg=cfg)
        except ValueError as e:
            print(f"mem-facts: {e}", file=sys.stderr)
            return 2
        print(json.dumps(out) if args.json else out["text"])
        return 0
    report = run(cfg, dry_run=args.dry_run, max_notes=args.max_notes, use_llm=not args.no_llm)
    if args.json:
        print(json.dumps(report))
    else:
        _print_report(report, show_facts=args.dry_run)
    attempted = [e for e in report["extracted"] if e["error"] is not None or e["llm"]]
    return 1 if attempted and all(e["error"] for e in attempted) else 0


if __name__ == "__main__":
    raise SystemExit(main())
