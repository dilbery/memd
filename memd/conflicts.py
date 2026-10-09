"""Advisory conflict warnings on save: does a new note contradict an existing one?

Two checks, cheapest first, both advisory. Neither ever blocks, fails or
changes a save, and nothing is superseded here: the receipt names the other
note and the explicit ``supersedes`` call that would retire it.

  * facts (default, no model): the deterministic patterns of memd.facts run
    over the new body. A new fact whose subject and predicate match a CURRENT
    fact of another live note with a different object is a conflict. Other
    facts come from the notes that mention the subject (pattern-extracted from
    Git, so no mem-facts run is needed) plus the index's fact table (model
    facts from mem-facts, used while their note's blob is unchanged). A newer
    new fact "updates" the other note; an undated or same-day one
    "contradicts" it; an older one is history and is not reported.
  * llm (MEMD_CONFLICT_CHECK=llm and MEMD_LLM_URL set): the new note and the
    top related notes save already found go to the chat model for a strict
    JSON verdict per note (contradicts / updates / consistent, quoting the
    conflicting claim). It runs after the commit, outside the clone lock,
    under a hard deadline (MEMD_CONFLICT_DEADLINE_MS, default 1500 ms); a
    timeout, transport error or malformed reply only adds a "skipped" warning.

The note being written and the note it explicitly supersedes are never
reported against themselves.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from memd.config import Config
from memd.facts import (Fact, _FENCE, _clean, entity_key, note_date, pattern_facts,
                        predicate_key)
from memd.llm import LLMError, chat, enabled
from memd.store import RETRACTED, Note

MAX_CONFLICTS = 5          # conflicts reported per save
MAX_SCAN_NOTES = 200       # notes pattern-scanned per save (those naming a new subject)
MAX_MODEL_NOTES = 3        # related notes sent to the model
MODEL_NOTE_CHARS = 2000    # body characters per note in the prompt
CLAIM_CHARS = 300          # quoted claim kept from a model verdict
KINDS = ("contradicts", "updates")


@dataclass(frozen=True)
class Conflict:
    """One existing note the saved note appears to conflict with."""

    slug: str
    title: str
    kind: str               # contradicts | updates
    evidence: str
    suggested_action: str
    method: str             # facts | llm

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def suggested_action(slug: str, other: str, kind: str) -> str:
    """The explicit call that would retire ``other`` in favour of ``slug``."""
    call = f"save again with slug={slug} and supersedes={other}"
    if kind == "updates":
        return f"{call} to retire the older note"
    return f"if this note is correct, {call}; otherwise correct this note"


def warning_line(c: Conflict) -> str:
    """The short line mirrored into the receipt's warnings."""
    verb = "Updates" if c.kind == "updates" else "Contradicts"
    return f"{verb} {c.slug}: {c.evidence} Never automatic: {c.suggested_action}."


def _live(notes: list[Note], exclude: set[str]) -> dict[str, Note]:
    return {n.slug: n for n in notes
            if not n.superseded_by and n.slug != RETRACTED and n.slug not in exclude
            and n.metadata.get("kind") != "summary"}


def _iso(d) -> str | None:
    return d.isoformat() if d else None


def _claim(f: Fact) -> str:
    return f"{f.subject} {f.predicate} {f.object}"


def _since(value: str | None) -> str:
    return f"since {value}" if value else "undated"


def _table_facts(db_path, keys: list[str], live: dict[str, Note]) -> list[tuple[str, Fact]]:
    """Facts of the index's fact table for these subjects; [] without a table.

    Read-only, and only rows extracted from the note's current blob: a changed
    note is covered by the Git scan instead.
    """
    path = Path(db_path) if db_path else None
    if path is None or not path.is_file() or not keys:
        return []
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.5)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='facts'").fetchone():
            return []
        rows = db.execute(
            "SELECT slug, git_blob, subject, predicate, object, valid_from, stated_to, method "
            "FROM facts WHERE subject_key IN (SELECT value FROM json_each(?))",
            (json.dumps(keys),)).fetchall()
    finally:
        db.close()
    out = []
    for slug, blob, s, p, o, start, stated, method in rows:
        note = live.get(slug)
        if note is not None and note.git_blob == blob:
            out.append((slug, Fact(s, p, o, start, stated, method)))
    return out


def _scan_facts(subjects: dict[str, set[str]], live: dict[str, Note]) -> list[tuple[str, Fact]]:
    """Pattern facts of the live notes whose text names one of the subjects."""
    out = []
    scanned = 0
    for note in live.values():
        text = f"{note.title}\n{note.body}".casefold()
        if not any(term in text for terms in subjects.values() for term in terms):
            continue
        scanned += 1
        if scanned > MAX_SCAN_NOTES:
            break
        d = note_date(note)
        out.extend((note.slug, f) for f in pattern_facts(note.body, _iso(d)))
    return out


def fact_conflicts(body: str, new_date: str | None, notes: list[Note], *, slug: str,
                   exclude: set[str], db_path=None) -> list[Conflict]:
    """Conflicts between the new body's pattern facts and other notes' current facts."""
    new = [f for f in pattern_facts(body, new_date) if not f.stated_to]
    if not new:
        return []
    live = _live(notes, exclude | {slug})
    wanted: dict[tuple[str, str], Fact] = {}
    subjects: dict[str, set[str]] = {}
    for f in new:
        s, p, _o = f.keys
        wanted.setdefault((s, p), f)
        subjects.setdefault(s, set()).update({s, _clean(f.subject).casefold()} - {""})
    seen: set = set()
    by_key: dict[tuple[str, str], list[tuple[str, Fact]]] = {}
    for other, f in _scan_facts(subjects, live) + _table_facts(db_path, sorted(subjects), live):
        s, p, o = f.keys
        if (s, p) not in wanted:
            continue
        mark = (other, s, p, o, f.valid_from)
        if mark in seen:
            continue
        seen.add(mark)
        by_key.setdefault((s, p), []).append((other, f))
    found: dict[str, Conflict] = {}
    for key, rows in by_key.items():
        mine = wanted[key]
        # The other notes' current values: not ended by an "until" date and not
        # followed by a later-dated different value (memd.facts.close_facts).
        dated = [f for _, f in rows if f.valid_from]
        current = [(other, f) for other, f in rows if not f.stated_to and not any(
            f.valid_from and g.valid_from > f.valid_from and g.keys[2] != f.keys[2] for g in dated)]
        for other, f in current:
            if f.keys[2] == mine.keys[2] or other in found:
                continue
            if mine.valid_from and f.valid_from and mine.valid_from < f.valid_from:
                continue    # the store already holds a newer value: this note is history
            kind = ("updates" if mine.valid_from and f.valid_from and mine.valid_from > f.valid_from
                    else "contradicts")
            evidence = (f'{other} says "{_claim(f)}" ({_since(f.valid_from)}); '
                        f'this note says "{_claim(mine)}" ({_since(mine.valid_from)}).')
            found[other] = Conflict(other, live[other].title, kind, evidence,
                                    suggested_action(slug, other, kind), "facts")
    return list(found.values())[:MAX_CONFLICTS]


# ---------------------------------------------------------------------------
# Model check
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You check a NEW note against EXISTING notes of a shared memory store about \
computers, services, configuration and preferences.

For each existing note give one verdict:
- "contradicts": the new note and the existing note state different values for \
the same thing and cannot both be true now.
- "updates": the new note records a later change that makes a claim of the \
existing note outdated.
- "consistent": no conflicting claim (different topics, agreement, or added detail).

For contradicts and updates, "claim" quotes the conflicting sentence of the \
EXISTING note verbatim; for consistent it is "". Judge only what the notes state.

Answer with JSON only, no prose and no code fence:
{"verdicts": [{"slug": "...", "verdict": "consistent", "claim": ""}]}
"""

VERDICT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdicts"],
    "properties": {"verdicts": {"type": "array", "maxItems": MAX_MODEL_NOTES, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["slug", "verdict", "claim"],
        "properties": {
            "slug": {"type": "string"},
            "verdict": {"type": "string", "enum": ["contradicts", "updates", "consistent"]},
            "claim": {"type": "string"},
        },
    }}},
}
RESPONSE_FORMAT = {"type": "json_schema",
                   "json_schema": {"name": "verdicts", "strict": True, "schema": VERDICT_SCHEMA}}


class VerdictParseError(ValueError):
    """The model's reply is not a JSON object with a verdicts list."""


def _block(label: str, note: Note) -> str:
    body = note.body if len(note.body) <= MODEL_NOTE_CHARS else note.body[:MODEL_NOTE_CHARS] + "\n[... truncated]"
    d = note_date(note)
    return f"### {label} [{note.slug}] {note.title}\nnote date: {_iso(d) or 'undated'}\n\n{body}\n"


def build_messages(new: Note, others: list[Note]) -> list[dict]:
    user = _block("NEW", new) + "\n" + "\n".join(_block("EXISTING", n) for n in others)
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def parse_verdicts(text: str, candidates: set[str]) -> dict[str, tuple[str, str]]:
    """{slug: (verdict, claim)} for known slugs; bad items dropped, a bad reply raises."""
    text = (text or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise VerdictParseError("reply contains no JSON object") from None
        try:
            data = json.loads(text[start:end + 1])
        except ValueError as e:
            raise VerdictParseError(f"reply is not valid JSON: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), list):
        raise VerdictParseError("reply JSON has no verdicts list")
    out: dict[str, tuple[str, str]] = {}
    for item in data["verdicts"]:
        if not isinstance(item, dict):
            continue
        slug, verdict, claim = item.get("slug"), item.get("verdict"), item.get("claim")
        if not isinstance(slug, str) or slug.strip() not in candidates or slug.strip() in out:
            continue
        verdict = verdict.strip().lower() if isinstance(verdict, str) else ""
        claim = " ".join(claim.split()) if isinstance(claim, str) else ""
        if verdict == "consistent":
            out[slug.strip()] = (verdict, "")
        elif verdict in KINDS and claim:
            # A conflict without the quoted claim is not evidence; drop it.
            out[slug.strip()] = (verdict, claim[:CLAIM_CHARS])
    return out


def _ask(messages: list[dict], cfg: Config) -> str:
    try:
        return chat(messages, cfg=cfg, max_tokens=600, temperature=0.0,
                    response_format=RESPONSE_FORMAT)
    except LLMError as e:
        # Not every OpenAI-compatible server accepts json_schema (see memd.facts).
        if "HTTP 400" not in str(e) and "HTTP 422" not in str(e):
            raise
        return chat(messages, cfg=cfg, max_tokens=600, temperature=0.0)


def model_enabled(cfg: Config) -> bool:
    return cfg.conflict_check == "llm" and enabled(cfg)


def model_conflicts(new: Note, others: list[Note], *, cfg: Config) -> tuple[list[Conflict], str | None]:
    """(conflicts, skipped reason) from one deadline-bounded chat completion.

    The completion runs on a worker thread that is abandoned at the deadline;
    its HTTP timeout is the same deadline, so it ends soon after. Never raises.
    """
    others = [n for n in others if n.slug != new.slug][:MAX_MODEL_NOTES]
    if not others:
        return [], None
    ms = cfg.conflict_deadline_ms
    bounded = dataclasses.replace(cfg, llm_timeout_s=ms / 1000.0)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="memd-conflict")
    try:
        future = pool.submit(_ask, build_messages(new, others), bounded)
        try:
            reply = future.result(timeout=ms / 1000.0)
        except concurrent.futures.TimeoutError:
            return [], f"model did not answer within {ms} ms"
        except Exception as e:  # LLMError, or anything the client raised
            return [], f"model unavailable: {str(e)[:200]}"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    try:
        verdicts = parse_verdicts(reply, {n.slug for n in others})
    except VerdictParseError as e:
        return [], f"model reply unusable: {e}"
    out = []
    for note in others:
        verdict, claim = verdicts.get(note.slug, ("consistent", ""))
        if verdict in KINDS:
            evidence = f'{note.slug} says "{claim}" (model verdict: {verdict}).'
            out.append(Conflict(note.slug, note.title, verdict, evidence,
                                suggested_action(new.slug, note.slug, verdict), "llm"))
    return out, None
