"""Entity pages: everything memory holds about one host, service or tag (GET /entities).

An entity is a name the store already treats as a thing, found by explainable
rules over data memd keeps (nothing is inferred by a model):

  host     a note's ``host`` field (through canonical_host, ``any`` excluded), and
           the one-word object of a ``runs on`` fact ("nginx runs on vmhost").
  service  any other fact subject (memd.facts subject keys), e.g. "nginx proxy".
  tag      a tag on at least MIN_TAG_NOTES live notes that looks like a name: one
           lowercase word of letters, digits and ``._-`` (no ``repo:`` style
           prefixes), not a generic topic word (GENERIC_TAGS). A tag equal to a
           host or service name adds its notes to that entity instead.

A live note belongs to an entity when it is scoped to it (host field), states a
fact about it (as subject or object), carries it as a tag, or mentions it in its
title or body (the keyword index, whole tokens). The list keeps the
MAX_ENTITIES strongest candidates (notes scoped, tagged or stating facts, plus
current facts) and scans mentions for those only, so the work per request is
bounded; a detail page scans mentions for its own entity too.

Read-only: the index and the inbox side file are opened read-only and nothing is
written. Results are cached per store for CACHE_TTL_S seconds.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path

from memd.facts import entity_key
from memd.hosts import canonical_host
from memd.insights import _iso_ts, _ro, _tables, store_paths
from memd.query import normalize
from memd.staleness import (STALE_AFTER_DAYS, as_of, failed_verification, is_stale,
                            normalize_volatility, _parse_date)
from memd.store import RETRACTED, not_archived_sql

KINDS = ("host", "service", "tag")
MAX_ENTITIES = 150          # entities listed (strongest first); the rest are counted
MAX_TAG_ENTITIES = 40       # tag entities among the candidates
MIN_TAG_NOTES = 2
MAX_NAME_CHARS = 160
MAX_FACT_ROWS = 100000      # fact rows scanned per store
MAX_MENTIONS = 500          # mentioning notes counted per entity
DETAIL_NOTES = 50
DETAIL_FACTS = 50
TIMELINE_FACTS = 100
CONFLICT_PAIRS = 20         # (subject, predicate) pairs checked for conflicts
MAX_VALUES = 5
RELATED = 12
INBOX_SCAN = 1000           # pending candidates scanned
INBOX_ITEMS = 20
CACHE_TTL_S = 30.0
CACHE_ENTRIES = 128

_TAG_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,47}$")
# Tags that name a kind of note or a topic rather than a thing.
GENERIC_TAGS = frozenset({
    "bug", "bugs", "config", "configuration", "debug", "debugging", "decision", "decisions",
    "doc", "docs", "draft", "fix", "fixes", "general", "guide", "handoff", "howto", "how-to",
    "idea", "ideas", "important", "incident", "issue", "issues", "log", "logs", "meta", "misc",
    "note", "notes", "ops", "personal", "plan", "plans", "policy", "preference", "preferences",
    "project", "projects", "reference", "runbook", "setup", "summary", "todo", "troubleshooting",
    "wip", "work", "workflow",
})
REASONS = ("scoped", "fact", "tag", "mention")

_clock = time.monotonic     # tests move it to expire the cache
_cache: dict[tuple, tuple[float, object]] = {}
_cache_lock = threading.Lock()


class UnknownEntity(LookupError):
    """No entity of that kind and name in this store."""


# --------------------------------------------------------------------------- model


def _note_info(slug: str, title, host, metadata, today: dt.date) -> dict:
    try:
        meta = json.loads(metadata or "{}")
    except (TypeError, ValueError):
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    extra = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    note = {**meta, "slug": slug, "title": title}
    d = as_of(note)
    fail = failed_verification(note, today)
    vol = normalize_volatility(note.get("volatility"))
    stale = is_stale(note, today)
    if fail:
        verification = {"status": "failed", "checked_at": fail["checked_at"].isoformat(),
                        "probe": fail["probe"]}
    elif _parse_date(note.get("verified_at")) is not None:
        verification = {"status": "verified", "verified_at": _parse_date(note["verified_at"]).isoformat()}
    elif extra.get("verify"):
        verification = {"status": "unchecked"}
    else:
        verification = None
    tags = [str(t).strip() for t in (note.get("tags") or []) if str(t).strip()] \
        if isinstance(note.get("tags"), list) else []
    return {"slug": slug, "title": title or slug, "host": host or "any",
            "date": d.isoformat() if d else None, "stale": stale,
            "stale_reason": ("verification failed" if fail else
                             f"{vol}, older than {STALE_AFTER_DAYS[vol]} days") if stale else None,
            "verification": verification, "tags": tags}


def _entity(table: dict, key: str, kind: str) -> dict:
    ent = table.get(key)
    if ent is None:
        ent = table[key] = {"kind": kind, "key": key, "spellings": Counter(),
                            "scoped": set(), "fact": set(), "tag": set(),
                            "current": 0, "facts": 0, "last_fact": None}
    return ent


def _host_key(value) -> str:
    host = str(value or "").strip()
    return canonical_host(host) if host and host.casefold() != "any" else ""


def _build(conn, profile: str, today: dt.date) -> dict:
    """Every candidate entity and the live notes they touch (without mentions)."""
    notes: dict[str, dict] = {}
    blobs: dict[str, str] = {}
    for slug, title, host, blob, metadata in conn.execute(
            "SELECT slug, title, host, git_blob, metadata FROM notes WHERE profile=? "
            "AND superseded_by IS NULL AND slug != ? AND " + not_archived_sql() + " ORDER BY slug",
            (profile, RETRACTED)):
        notes[slug] = _note_info(slug, title, host, metadata, today)
        blobs[slug] = blob

    hosts: dict[str, dict] = {}
    for slug, info in notes.items():
        key = _host_key(info["host"])
        if key:
            ent = _entity(hosts, key, "host")
            ent["scoped"].add(slug)
            ent["spellings"][key] += 1

    facts: list[tuple] = []
    truncated = False
    if "facts" in _tables(conn):
        facts = conn.execute(
            "SELECT subject, subject_key, predicate_key, object, object_key, valid_from, valid_to, "
            "slug, git_blob FROM facts ORDER BY id DESC LIMIT ?", (MAX_FACT_ROWS + 1,)).fetchall()
        truncated = len(facts) > MAX_FACT_ROWS
        facts = [f for f in facts[:MAX_FACT_ROWS] if f[7] in notes]
    for _s, _sk, pkey, obj, okey, *_rest in facts:
        if pkey == "runs on" and okey and " " not in okey:
            _entity(hosts, okey, "host")["spellings"][obj] += 1

    entities: dict[str, dict] = dict(hosts)

    def add_fact(ent, spelling, f):
        subject, skey, pkey, obj, okey, since, until, slug, blob = f
        ent["spellings"][spelling] += 1
        ent["fact"].add(slug)
        ent["facts"] += 1
        if until is None and blobs.get(slug) == blob:
            ent["current"] += 1
        if since and (ent["last_fact"] is None or since > ent["last_fact"]):
            ent["last_fact"] = since

    for f in facts:
        subject, skey, _p, obj, okey = f[:5]
        if len(skey) >= 2:
            add_fact(_entity(entities, skey, "host" if skey in hosts else "service"), subject, f)
        if okey in hosts and okey != skey:
            add_fact(entities[okey], obj, f)

    tagged: dict[str, set] = {}
    for slug, info in notes.items():
        for tag in dict.fromkeys(t.casefold() for t in info["tags"]):
            tagged.setdefault(tag, set()).add(slug)
    tag_candidates = []
    for tag, slugs in tagged.items():
        if tag in entities:
            entities[tag]["tag"] |= slugs
        elif len(slugs) >= MIN_TAG_NOTES and _TAG_NAME.match(tag) and tag not in GENERIC_TAGS:
            tag_candidates.append((tag, slugs))
    tag_candidates.sort(key=lambda t: (-len(t[1]), t[0]))
    for tag, slugs in tag_candidates[:MAX_TAG_ENTITIES]:
        ent = _entity(entities, tag, "tag")
        ent["tag"] |= slugs
        ent["spellings"][tag] += 1
    return {"notes": notes, "entities": entities, "facts_truncated": truncated,
            "tags_skipped": max(0, len(tag_candidates) - MAX_TAG_ENTITIES)}


def _direct(ent: dict) -> set:
    return ent["scoped"] | ent["fact"] | ent["tag"]


def _strength(ent: dict) -> tuple:
    return (-(len(_direct(ent)) + ent["current"]), ent["kind"], ent["key"])


def _fts_phrase(name: str) -> str | None:
    text = normalize(name).strip()
    if not text:
        return None
    return '{title body} : "' + text.replace('"', '""') + '"'


def _mentions(conn, profile: str, name: str, live: dict) -> set:
    """Live notes whose title or body contains the name as whole tokens (keyword index)."""
    phrase = _fts_phrase(name)
    if phrase is None or "fts_notes" not in _tables(conn):
        return set()
    try:
        rows = conn.execute(
            "SELECT n.slug FROM fts_notes f JOIN notes n ON n.slug = f.slug "
            "WHERE fts_notes MATCH ? AND n.profile = ? AND n.superseded_by IS NULL LIMIT ?",
            (phrase, profile, MAX_MENTIONS)).fetchall()
    except sqlite3.Error:
        return set()
    return {r[0] for r in rows if r[0] in live}


def _display(ent: dict) -> str:
    if ent["kind"] == "host" or not ent["spellings"]:
        return ent["key"]
    return sorted(ent["spellings"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _summary(ent: dict, slugs: set, notes: dict) -> dict:
    dates = [notes[s]["date"] for s in slugs if notes[s]["date"]]
    if ent["last_fact"]:
        dates.append(ent["last_fact"])
    return {"kind": ent["kind"], "name": ent["key"], "display": _display(ent),
            "notes": len(slugs), "current_facts": ent["current"],
            "stale": sum(notes[s]["stale"] for s in slugs),
            "failed_verification": sum((notes[s]["verification"] or {}).get("status") == "failed"
                                       for s in slugs),
            "last_activity": max(dates) if dates else None}


def _model(cfg, profile: str, *, fresh: bool, now: float | None = None) -> dict | None:
    """The store's entities with their note sets, cached; None without an index."""
    clone, db_path = store_paths(cfg, profile)
    key = ("model", profile, str(db_path))
    hit = None if fresh else _cache_get(key)
    if hit is not None:
        return hit
    now = time.time() if now is None else now
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    if not Path(db_path).exists():
        return None
    conn = _ro(db_path)
    try:
        if "notes" not in _tables(conn):
            return None
        model = _build(conn, profile, today)
        ranked = sorted(model["entities"].values(), key=_strength)
        top = ranked[:MAX_ENTITIES]
        for ent in top:
            ent["mention"] = _mentions(conn, profile, ent["key"], model["notes"])
            ent["notes"] = _direct(ent) | ent["mention"]
    finally:
        conn.close()
    model.update(top=[e["key"] for e in top], total=len(ranked), generated_at=_iso_ts(now),
                 db_path=db_path)
    _cache_put(key, model)
    return model


# --------------------------------------------------------------------------- cache


def _cache_get(key):
    now = _clock()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < CACHE_TTL_S:
            return hit[1]
    return None


def _cache_put(key, value) -> None:
    now = _clock()
    with _cache_lock:
        for old in [k for k, (ts, _) in _cache.items() if now - ts >= CACHE_TTL_S]:
            _cache.pop(old, None)
        while len(_cache) >= CACHE_ENTRIES:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
        _cache[key] = (now, value)


# --------------------------------------------------------------------------- list


def list_entities(cfg, profile: str, *, fresh: bool = False) -> dict:
    """GET /entities: the strongest entities with their counts (read-only, cached)."""
    started = time.perf_counter()
    model = _model(cfg, profile, fresh=fresh)
    if model is None:
        return {"ok": False, "profile": profile, "err": "This store has no index yet; run a reindex first."}
    notes = model["notes"]
    items = [_summary(e, e["notes"], notes) for e in (model["entities"][k] for k in model["top"])]
    items.sort(key=lambda e: (-e["notes"], -e["current_facts"], e["display"].casefold(), e["kind"]))
    return {"ok": True, "profile": profile, "generated_at": model["generated_at"],
            "total": model["total"], "limit": MAX_ENTITIES,
            "truncated": model["total"] > len(items), "facts_truncated": model["facts_truncated"],
            "notes": len(notes), "entities": items,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}


# --------------------------------------------------------------------------- detail


def lookup_key(kind: str, name: str) -> str:
    """The entity key a (kind, name) path names; UnknownEntity when it cannot be one."""
    if kind not in KINDS or not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS:
        raise UnknownEntity(name)
    if kind == "tag":
        return name.strip().casefold()
    key = _host_key(name) if kind == "host" else entity_key(name)
    if not key:
        raise UnknownEntity(name)
    return key


def _fact_rows(conn, key: str, live: dict, blobs: dict) -> list[dict]:
    if "facts" not in _tables(conn):
        return []
    out = []
    for (fid, subject, skey, pkey, obj, okey, since, until, slug, blob, method, closer) in conn.execute(
            "SELECT f.id, f.subject, f.subject_key, f.predicate_key, f.object, f.object_key, "
            "f.valid_from, f.valid_to, f.slug, f.git_blob, f.method, c.slug FROM facts f "
            "LEFT JOIN facts c ON c.id = f.closed_by WHERE f.subject_key = ? OR f.object_key = ? "
            "ORDER BY COALESCE(f.valid_from, '') DESC, f.id DESC LIMIT ?",
            (key, key, MAX_FACT_ROWS)):
        if slug not in live:
            continue
        out.append({"subject": subject, "subject_key": skey, "predicate": pkey, "object": obj,
                    "object_key": okey, "valid_from": since, "valid_to": until,
                    "current": until is None and blobs.get(slug) == blob,
                    "note_changed": blobs.get(slug) != blob,
                    "role": "subject" if skey == key else "object",
                    "source": slug, "source_title": live[slug]["title"], "method": method,
                    "closed_by": closer})
    return out


def _conflicts(conn, pairs: list[tuple[str, str]], live: dict, blobs: dict) -> list[dict]:
    """Current facts of one subject and predicate with different objects in different notes."""
    clusters = []
    for skey, pkey in pairs[:CONFLICT_PAIRS]:
        values: dict[str, dict] = {}
        subject = skey
        for subj, obj, okey, slug, blob in conn.execute(
                "SELECT subject, object, object_key, slug, git_blob FROM facts WHERE subject_key = ? "
                "AND predicate_key = ? AND valid_to IS NULL ORDER BY id", (skey, pkey)):
            if slug not in live or blobs.get(slug) != blob:
                continue
            subject = subj
            value = values.setdefault(okey, {"object": obj, "slugs": []})
            if slug not in value["slugs"]:
                value["slugs"].append(slug)
        slugs = {s for v in values.values() for s in v["slugs"]}
        if len(values) < 2 or len(slugs) < 2:
            continue
        ordered = sorted(values.values(), key=lambda v: (-len(v["slugs"]), v["object"]))
        clusters.append({"subject": subject, "predicate": pkey, "notes": len(slugs),
                         "values": [{"object": v["object"],
                                     "notes": [{"slug": s, "title": live[s]["title"]} for s in sorted(v["slugs"])]}
                                    for v in ordered[:MAX_VALUES]],
                         "more_values": max(0, len(ordered) - MAX_VALUES)})
    return clusters


def _word_pattern(name: str) -> re.Pattern:
    text = re.escape(normalize(name).casefold())
    return re.compile(rf"(?<![\w\-.:/@]){text}(?![\w\-.:/@])")


def _inbox(db_path: Path, ent: dict, *, can_review: bool) -> dict:
    from memd.inbox import inbox_file
    path = inbox_file(db_path)
    out = {"available": False, "pending": 0, "can_review": can_review, "items": None}
    if not path.exists():
        return out
    conn = _ro(path)
    try:
        if "candidates" not in _tables(conn):
            return out
        rows = conn.execute("SELECT id, created, source, title, body, meta FROM candidates "
                            "WHERE status='pending' ORDER BY created DESC LIMIT ?", (INBOX_SCAN,)).fetchall()
    finally:
        conn.close()
    pattern = _word_pattern(ent["key"])
    matched = []
    for cid, created, source, title, body, meta in rows:
        try:
            meta = json.loads(meta or "{}")
        except (TypeError, ValueError):
            meta = {}
        meta = meta if isinstance(meta, dict) else {}
        tags = meta.get("tags") if isinstance(meta.get("tags"), list) else []
        hit = (pattern.search(normalize(f"{title or ''}\n{body or ''}").casefold())
               or (ent["kind"] == "host" and _host_key(meta.get("host")) == ent["key"])
               or ent["key"] in {str(t).strip().casefold() for t in tags})
        if hit:
            matched.append({"id": cid, "title": title or "(untitled)", "source": source or "",
                            "created": _iso_ts(created) if isinstance(created, (int, float)) else None})
    out.update(available=True, pending=len(matched), truncated=len(rows) >= INBOX_SCAN)
    if can_review:
        out["items"] = matched[:INBOX_ITEMS]
    return out


def entity(cfg, profile: str, kind: str, name: str, *, can_review: bool = False,
           fresh: bool = False) -> dict:
    """GET /entities/{kind}/{name}: one entity's page. UnknownEntity when it does not exist."""
    started = time.perf_counter()
    key = lookup_key(kind, name)
    cache_key = ("entity", profile, kind, key, bool(can_review))
    model = _model(cfg, profile, fresh=fresh)
    if model is None:
        raise UnknownEntity(name)
    ent = model["entities"].get(key)
    if ent is None or ent["kind"] != kind:
        raise UnknownEntity(name)
    cache_key += (str(model["db_path"]),)
    hit = None if fresh else _cache_get(cache_key)
    if hit is not None:
        return {**hit, "cached": True}
    notes = model["notes"]
    conn = _ro(model["db_path"])
    try:
        # Entities past the list cap had no mention scan; the model stays untouched.
        mention = ent["mention"] if "mention" in ent else _mentions(conn, profile, key, notes)
        blobs = dict(conn.execute("SELECT slug, git_blob FROM notes WHERE profile=? "
                                  "AND superseded_by IS NULL", (profile,)).fetchall())
        rows = _fact_rows(conn, key, notes, blobs)
        pairs = list(dict.fromkeys((r["subject_key"], r["predicate"]) for r in rows if r["current"]))
        conflicts = _conflicts(conn, pairs, notes, blobs)
    finally:
        conn.close()

    def fact(r, *fields):
        return {k: r[k] for k in fields}
    current = [fact(r, "subject", "predicate", "object", "role", "source", "source_title", "method")
               | {"since": r["valid_from"]} for r in rows if r["current"]]
    timeline = [fact(r, "subject", "predicate", "object", "role", "valid_from", "valid_to", "current",
                     "source", "source_title", "closed_by", "note_changed")
                for r in sorted(rows, key=lambda r: (r["valid_from"] or "", r["current"]), reverse=True)]

    sets = {"scoped": ent["scoped"], "fact": ent["fact"], "tag": ent["tag"], "mention": mention}
    ent_notes = _direct(ent) | mention
    listed = []
    for slug in ent_notes:
        info = notes[slug]
        why = [r for r in REASONS if slug in sets[r]]
        listed.append({k: info[k] for k in ("slug", "title", "host", "date", "stale", "stale_reason",
                                             "verification")} | {"why": why})
    listed.sort(key=lambda n: (not n["stale"], (n["verification"] or {}).get("status") != "failed",
                               -(dt.date.fromisoformat(n["date"]).toordinal() if n["date"] else 0),
                               n["title"].casefold(), n["slug"]))

    linked = {r["object_key"] if r["role"] == "subject" else r["subject_key"] for r in rows}
    related = []
    for other_key in model["top"]:
        other = model["entities"][other_key]
        if other_key == key:
            continue
        shared = len(ent_notes & other["notes"])
        link = other_key in linked
        if shared or link:
            related.append({"kind": other["kind"], "name": other_key, "display": _display(other),
                            "shared_notes": shared, "fact_link": link})
    related.sort(key=lambda r: (-(r["shared_notes"] + 2 * r["fact_link"]), r["display"].casefold()))

    summary = _summary(ent, ent_notes, notes)
    body = {"ok": True, "profile": profile, "generated_at": model["generated_at"],
            "entity": summary | {"reasons": {r: len(sets[r]) for r in REASONS}},
            "current_facts": {"total": len(current), "items": current[:DETAIL_FACTS]},
            "timeline": {"total": len(timeline), "items": timeline[:TIMELINE_FACTS]},
            "notes": {"total": len(listed), "items": listed[:DETAIL_NOTES]},
            "related": {"total": len(related), "items": related[:RELATED]},
            "conflicts": {"total": len(conflicts), "items": conflicts},
            "inbox": _inbox(Path(model["db_path"]), ent, can_review=can_review),
            "cached": False}
    body["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    _cache_put(cache_key, body)
    return body
