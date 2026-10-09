"""Memory health: where a store's memory is rotting or wasted (mem-health, GET /insights).

One read-only aggregation per store over the sources memd already keeps. Nothing
is written: the index is opened read-only, the usage and inbox side files too,
and Git is only asked for one tree listing.

  usage        never-recalled notes (only notes that existed before the usage
               window began; a newer note has not had its chance), notes shown
               by recall again and again but never read, and the most read notes
               (memd.usage). The window is the retained log, at most the newest
               MAX_RECALLS (20,000) recalls.
  staleness    changeable-state notes past their freshness window and recent
               failed re-checks (memd.staleness), plus heatmap data: live notes
               counted by tag (and by host) x age bucket, age from the note's
               as-of date (verified_at, observed_at, a date in the title or slug).
  facts        notes whose every fact was closed by newer facts from other notes
               (supersede candidates, memd.facts) and contradiction clusters:
               one subject and predicate with different CURRENT objects in
               different live notes.
  summaries    summary notes whose sources changed or were superseded
               (memd.summarize).
  inbox        candidates waiting for review (memd.inbox).
  forget       notes archived by review, and archive proposals on the
               mem-forget branch not merged yet (memd.forget). Archived notes
               are not live: no other section counts them.
  coverage     live notes still waiting for a current note vector or chunk
               vectors (memd.index).

Every list is capped at `limit` items and carries its full `total`. A section
whose source is absent (usage logging off, no facts extracted yet, no summaries)
reports ``available: false`` with a ``reason`` instead of zeros, so an empty list
always means "checked, nothing found". Reports are cached per store for
CACHE_TTL_S seconds.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from itertools import chain
from pathlib import Path

from memd.staleness import (FAILED_VERIFICATION_DAYS, STALE_AFTER_DAYS, as_of,
                            failed_verification, is_stale, normalize_volatility)
from memd.store import RETRACTED, archived_value

LIST_LIMIT = 20             # items per list (limit, 1..MAX_LIMIT)
MAX_LIMIT = 100
MAX_RECALLS = 20000         # newest recalls scanned; bounds the work on a busy store
MIN_SHOWN = 3               # shown this often and never read -> "recalled, never read"
MIN_WINDOW_DAYS = 1.0       # a usage log younger than this says nothing yet
HEATMAP_ROWS = 12           # tags / hosts per heatmap
MAX_VALUES = 5              # conflicting values listed per contradiction cluster
MAX_REASONS = 3             # stale-summary reasons listed per summary
CACHE_TTL_S = 60.0
GIT_TIMEOUT_S = 5.0
UNTAGGED = "(untagged)"
AGE_BUCKETS = (("0-7d", 7), ("8-30d", 30), ("31-90d", 90), ("91-365d", 365), (">1y", None))
UNDATED = "undated"

_clock = time.monotonic     # tests move it to expire the cache
_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _no_data(reason: str, **extra) -> dict:
    return {"available": False, "reason": reason, **extra}


def _capped(items: list, limit: int, **extra) -> dict:
    return {"available": True, "reason": None, "total": len(items), "items": items[:limit], **extra}


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


# --------------------------------------------------------------------------- notes


class _Note:
    """One index row: the columns this report needs and its parsed metadata."""

    __slots__ = ("slug", "title", "host", "importance", "path", "blob", "superseded_by",
                 "vector_current", "chunks_current", "meta")

    def __init__(self, row):
        (self.slug, self.title, self.host, self.importance, self.path, self.blob,
         self.superseded_by, vector_blob, chunk_blob, metadata) = row
        self.vector_current = bool(vector_blob) and vector_blob == self.blob
        self.chunks_current = bool(chunk_blob) and chunk_blob == self.blob
        try:
            meta = json.loads(metadata or "{}")
        except (TypeError, ValueError):
            meta = {}
        self.meta = meta if isinstance(meta, dict) else {}

    @property
    def extra(self) -> dict:
        """Unknown frontmatter (verify, verification, kind, sources, ...)."""
        extra = self.meta.get("metadata")
        return extra if isinstance(extra, dict) else {}

    def as_note(self) -> dict:
        """The dict shape memd.staleness reads."""
        return {**self.meta, "slug": self.slug, "title": self.title}

    def ref(self, **more) -> dict:
        return {"slug": self.slug, "title": self.title or self.slug, "host": self.host or "any",
                "importance": self.importance, **more}


def _load_notes(conn, profile: str) -> list[_Note]:
    return [_Note(r) for r in conn.execute(
        "SELECT slug, title, host, importance, path, git_blob, superseded_by, vector_blob, "
        "chunk_blob, metadata FROM notes WHERE profile=? ORDER BY slug", (profile,))]


def _rel(prefix: str, path: str) -> str:
    """The note's path relative to the clone (prefix: the clone path with a trailing slash)."""
    path = path or ""
    return path[len(prefix):] if prefix and path.startswith(prefix) else path


def _paths_before(clone: Path | None, cutoff: float) -> set[str] | None:
    """Paths committed in the clone before `cutoff` (epoch s); None when Git cannot say."""
    if clone is None or not Path(clone).exists():
        return None
    try:
        rev = subprocess.run(["git", "-C", str(clone), "rev-list", "-1", f"--before=@{int(cutoff)}", "HEAD"],
                             capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
        if rev.returncode != 0:
            return None
        commit = rev.stdout.strip()
        if not commit:
            return set()        # the whole history is younger than the window
        tree = subprocess.run(["git", "-C", str(clone), "ls-tree", "-r", "-z", "--name-only", commit],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
        if tree.returncode != 0:
            return None
        return {p for p in tree.stdout.split("\0") if p}
    except (OSError, subprocess.SubprocessError):
        return None


def _older_than(notes: list[_Note], clone: Path | None, cutoff: float) -> tuple[list[_Note], str]:
    """Notes that already existed at `cutoff`, and how that was decided.

    Git first: the note's path was in the last commit before the cutoff. Without
    Git, the file's modification time, which is never earlier than its creation,
    so a note is only ever counted as old when it certainly is.
    """
    committed = _paths_before(clone, cutoff)
    if committed is not None:
        prefix = str(clone).rstrip("/") + "/" if clone is not None else ""
        return [n for n in notes if _rel(prefix, n.path) in committed], "git"
    old = []
    for n in notes:
        p = Path(n.path)
        if clone is not None and not p.is_absolute():
            p = Path(clone) / p
        try:
            if p.stat().st_mtime < cutoff:
                old.append(n)
        except OSError:
            pass
    return old, "mtime"


# --------------------------------------------------------------------------- usage


def _usage(cfg, db_path: Path, now: float) -> tuple[dict, dict[str, int], dict[str, int]]:
    """(usage section, shown count per slug, read count per slug)."""
    from memd.usage import usage_path
    if getattr(cfg, "usage_log", "on") == "off":
        return _no_data("Usage logging is off (MEMD_USAGE_LOG=off)."), {}, {}
    path = usage_path(db_path)
    if not path.exists():
        return _no_data("No recalls have been logged for this store yet."), {}, {}
    conn = _ro(path)
    try:
        if not {"recalls", "reads"} <= _tables(conn):
            return _no_data("No recalls have been logged for this store yet."), {}, {}
        count, oldest = conn.execute(
            "SELECT COUNT(*), MIN(ts) FROM (SELECT ts FROM recalls ORDER BY ts DESC LIMIT ?)",
            (MAX_RECALLS,)).fetchone()
        if not count:
            return _no_data("No recalls have been logged for this store yet."), {}, {}
        retention = getattr(cfg, "usage_retention_days", 90)
        start = max(oldest, now - retention * 86400)
        # Slugs are de-duplicated per recall when logged, so an item count is a recall count.
        rows = conn.execute("SELECT slugs FROM recalls ORDER BY ts DESC LIMIT ?", (MAX_RECALLS,)).fetchall()
        shown = Counter(s for s in chain.from_iterable(_slug_list(r[0]) for r in rows) if isinstance(s, str))
        reads = dict(conn.execute("SELECT slug, COUNT(*) FROM reads WHERE ts >= ? GROUP BY slug",
                                  (start,)).fetchall())
    finally:
        conn.close()
    days = (now - start) / 86400
    section = {"available": True, "reason": None, "recalls": count,
               "reads": sum(reads.values()), "since": _iso_ts(start),
               "window_days": round(days, 1), "truncated": count >= MAX_RECALLS,
               "start": start}
    if days < MIN_WINDOW_DAYS:
        section.update(available=False,
                       reason="The usage log covers less than a day; check back later.")
    return section, shown, reads


def _slug_list(text) -> list:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _iso_ts(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- staleness


def _bucket(age: int | None) -> str:
    if age is None:
        return UNDATED
    for label, bound in AGE_BUCKETS:
        if bound is None or age <= bound:
            return label
    return AGE_BUCKETS[-1][0]


def _heatmap(rows: dict[str, dict], buckets: list[str]) -> dict:
    ordered = sorted(rows.items(), key=lambda kv: (-kv[1]["stale"], -kv[1]["notes"], kv[0]))
    return {"total_rows": len(ordered), "rows": [
        {"key": key, "notes": r["notes"], "stale": r["stale"],
         "cells": [r["cells"].get(b, 0) for b in buckets]}
        for key, r in ordered[:HEATMAP_ROWS]]}


def _staleness(live: list[_Note], today: dt.date, limit: int) -> tuple[dict, dict, dict]:
    """(stale section, failed-verification section, heatmap)."""
    buckets = [label for label, _ in AGE_BUCKETS] + [UNDATED]
    by_tag: dict[str, dict] = {}
    by_host: dict[str, dict] = {}
    stale, failed = [], []
    judged = probed = 0
    for n in live:
        note = n.as_note()
        d = as_of(note)
        age = (today - d).days if d is not None else None
        vol = normalize_volatility(note.get("volatility"))
        judged += vol in STALE_AFTER_DAYS
        probed += bool(n.extra.get("verify")) or isinstance(n.extra.get("verification"), dict)
        fail = failed_verification(note, today)
        rotten = is_stale(note, today)
        if fail:
            failed.append(n.ref(checked_at=fail["checked_at"].isoformat(), probe=fail["probe"]))
        if rotten:
            stale.append(n.ref(volatility=vol, as_of=d.isoformat() if d else None, age_days=age,
                               reason=("verification failed" if fail
                                       else f"{vol}, older than {STALE_AFTER_DAYS[vol]} days")))
        bucket = _bucket(age)
        tags = [str(t) for t in (note.get("tags") or []) if str(t).strip()] or [UNTAGGED]
        for key, table in [(t, by_tag) for t in dict.fromkeys(tags)] + [(n.host or "any", by_host)]:
            row = table.setdefault(key, {"notes": 0, "stale": 0, "cells": {}})
            row["notes"] += 1
            row["stale"] += rotten
            row["cells"][bucket] = row["cells"].get(bucket, 0) + 1
    stale.sort(key=lambda r: (-(r["age_days"] or 0), r["slug"]))
    failed.sort(key=lambda r: (r["checked_at"], r["slug"]), reverse=True)
    if judged or probed:
        stale_section = _capped(stale, limit)
    else:
        stale_section = _no_data("No note declares a volatility (state or volatile) or a verify "
                                 "probe, so none can be judged stale.", total=0, items=[])
    if probed:
        failed_section = _capped(failed, limit, days=FAILED_VERIFICATION_DAYS)
    else:
        failed_section = _no_data("No note declares verify probes (mem-verify checks those).",
                                  total=0, items=[])
    heat = {"available": bool(live), "reason": None if live else "No live notes in the index.",
            "buckets": buckets, "tags": _heatmap(by_tag, buckets), "hosts": _heatmap(by_host, buckets)}
    return stale_section, failed_section, heat


# --------------------------------------------------------------------------- facts


def _facts(conn, live: dict[str, _Note], limit: int) -> tuple[dict, dict]:
    """(supersede candidates, contradiction clusters)."""
    reason = "No facts extracted yet; run mem-facts to derive them."
    if not {"facts"} <= _tables(conn):
        return _no_data(reason, total=0, items=[]), _no_data(reason, total=0, items=[])
    if conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0:
        return _no_data(reason, total=0, items=[]), _no_data(reason, total=0, items=[])
    from memd.facts import supersede_candidates
    candidates = [live[c["slug"]].ref(facts=c["facts"], closed_by=c["closed_by"])
                  for c in supersede_candidates(conn) if c["slug"] in live]
    groups: dict[tuple[str, str], dict] = {}
    for subject, predicate, obj, skey, pkey, okey, slug, blob in conn.execute(
            "SELECT subject, predicate, object, subject_key, predicate_key, object_key, slug, git_blob "
            "FROM facts WHERE valid_to IS NULL ORDER BY id"):
        note = live.get(slug)
        if note is None or note.blob != blob:
            continue            # retired note, or a fact of an older revision
        g = groups.setdefault((skey, pkey), {"subject": subject, "predicate": pkey, "values": {}})
        value = g["values"].setdefault(okey, {"object": obj, "slugs": []})
        if slug not in value["slugs"]:
            value["slugs"].append(slug)
    clusters = []
    for g in groups.values():
        slugs = {s for v in g["values"].values() for s in v["slugs"]}
        if len(g["values"]) < 2 or len(slugs) < 2:
            continue
        values = sorted(g["values"].values(), key=lambda v: (-len(v["slugs"]), v["object"]))
        clusters.append({"subject": g["subject"], "predicate": g["predicate"], "notes": len(slugs),
                         "values": [{"object": v["object"],
                                     "notes": [live[s].ref() for s in sorted(v["slugs"])]}
                                    for v in values[:MAX_VALUES]],
                         "more_values": max(0, len(values) - MAX_VALUES)})
    clusters.sort(key=lambda c: (-c["notes"], c["subject"].casefold(), c["predicate"]))
    return _capped(candidates, limit), _capped(clusters, limit)


# --------------------------------------------------------------------------- summaries


def _summaries(notes: list[_Note], live: list[_Note], limit: int) -> dict:
    from memd.store import Note
    from memd.summarize import SUMMARY_KIND, stale_reasons
    summaries = [n for n in live if n.extra.get("kind") == SUMMARY_KIND]
    if not summaries:
        return _no_data("No summary notes in this store (mem-summarize proposes them).",
                        total=0, items=[])
    by_slug = {n.slug: Note(title=n.title or "", slug=n.slug, path=n.path or "", body="",
                            git_blob=n.blob or "", superseded_by=n.superseded_by) for n in notes}
    stale = []
    for s in summaries:
        summary = Note(title=s.title or "", slug=s.slug, path=s.path or "", body="",
                       git_blob=s.blob or "", metadata=s.extra)
        reasons = stale_reasons(summary, by_slug)
        if reasons:
            stale.append(s.ref(reasons=reasons[:MAX_REASONS], more_reasons=max(0, len(reasons) - MAX_REASONS)))
    stale.sort(key=lambda r: (-len(r["reasons"]) - r["more_reasons"], r["slug"]))
    return _capped(stale, limit, summaries=len(summaries))


# --------------------------------------------------------------------------- inbox


def _inbox(db_path: Path) -> dict:
    from memd.inbox import inbox_file
    path = inbox_file(db_path)
    if not path.exists():
        return _no_data("No candidates have been proposed to this store's inbox.", pending=None)
    conn = _ro(path)
    try:
        if "candidates" not in _tables(conn):
            return _no_data("No candidates have been proposed to this store's inbox.", pending=None)
        pending = conn.execute("SELECT COUNT(*) FROM candidates WHERE status='pending'").fetchone()[0]
        oldest = conn.execute("SELECT MIN(created) FROM candidates WHERE status='pending'").fetchone()[0]
    finally:
        conn.close()
    return {"available": True, "reason": None, "pending": pending,
            "oldest": _iso_ts(oldest) if isinstance(oldest, (int, float)) else None}


# --------------------------------------------------------------------------- forget


def _forget(clone: Path | None, archived: int) -> dict:
    from memd.forget import branch_name, pending
    branch = branch_name()
    waiting = pending(clone, branch)
    if waiting is None:
        return _no_data("Git could not list the mem-forget review branch.",
                        archived=archived, pending=None, branch=branch)
    return {"available": True, "reason": None, "archived": archived, "pending": waiting,
            "branch": branch}


# --------------------------------------------------------------------------- report


def compute(cfg, profile: str, *, clone: Path | None, db_path: Path, limit: int = LIST_LIMIT,
            now: float | None = None) -> dict:
    """The health report for one store's index at db_path (uncached, read-only)."""
    started = time.perf_counter()
    now = time.time() if now is None else now
    limit = max(1, min(MAX_LIMIT, int(limit)))
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    clone = Path(clone) if clone is not None else None
    db_path = Path(db_path)
    report: dict = {"ok": True, "profile": profile, "generated_at": _iso_ts(now), "limit": limit}
    if not db_path.exists():
        return {**report, "ok": False, "err": "This store has no index yet; run a reindex first."}
    conn = _ro(db_path)
    try:
        if "notes" not in _tables(conn):
            return {**report, "ok": False, "err": "This store has no index yet; run a reindex first."}
        notes = _load_notes(conn, profile)
        archived = sum(1 for n in notes if not n.superseded_by and archived_value(n.extra))
        live = [n for n in notes if not n.superseded_by and n.slug != RETRACTED
                and not archived_value(n.extra)]
        live_by_slug = {n.slug: n for n in live}
        supersede, contradictions = _facts(conn, live_by_slug, limit)
        heads = dict(conn.execute("SELECT key, value FROM meta WHERE key IN ('head','lexical_head')")
                     .fetchall()) if "meta" in _tables(conn) else {}
    finally:
        conn.close()

    usage, shown, reads = _usage(cfg, db_path, now)
    if usage["available"]:
        start = usage["start"]
        old, basis = _older_than(live, clone, start)
        never = [n.ref() for n in old if not shown.get(n.slug)]
        never.sort(key=lambda r: (-(r["importance"] or 0), (r["title"] or "").casefold(), r["slug"]))
        if old:
            never_recalled = _capped(never, limit, eligible=len(old), basis=basis)
        else:
            never_recalled = _no_data("No note is older than the usage window yet.",
                                      total=0, items=[], eligible=0, basis=basis)
        unread = [live_by_slug[s].ref(shown=c) for s, c in shown.items()
                  if s in live_by_slug and c >= MIN_SHOWN and not reads.get(s)]
        unread.sort(key=lambda r: (-r["shown"], r["slug"]))
        useful = [live_by_slug[s].ref(reads=c, shown=shown.get(s, 0),
                                      read_rate=round(min(1.0, c / shown[s]), 3) if shown.get(s) else None)
                  for s, c in reads.items() if s in live_by_slug and c]
        useful.sort(key=lambda r: (-r["reads"], r["shown"], r["slug"]))
        unread_section = _capped(unread, limit, min_shown=MIN_SHOWN)
        useful_section = _capped(useful, limit)
    else:
        never_recalled = _no_data(usage["reason"], total=0, items=[])
        unread_section = _no_data(usage["reason"], total=0, items=[], min_shown=MIN_SHOWN)
        useful_section = _no_data(usage["reason"], total=0, items=[])
    usage.pop("start", None)

    stale, failed, heatmap = _staleness(live, today, limit)
    pending_vectors = sum(not n.vector_current for n in live)
    pending_chunks = sum(not n.chunks_current for n in live)
    coverage = {"available": True, "reason": None, "notes": len(live),
                "superseded": len(notes) - len(live) - archived, "archived": archived,
                "pending_vectors": pending_vectors, "pending_chunks": pending_chunks,
                "pending": sum(not (n.vector_current and n.chunks_current) for n in live),
                "vector_share": round(1 - pending_vectors / len(live), 4) if live else None,
                "chunk_share": round(1 - pending_chunks / len(live), 4) if live else None,
                "head": heads.get("head"), "lexical_head": heads.get("lexical_head")}
    report.update({
        "usage": usage,
        "never_recalled": never_recalled,
        "recalled_unread": unread_section,
        "most_useful": useful_section,
        "stale": stale,
        "heatmap": heatmap,
        "failed_verifications": failed,
        "supersede_candidates": supersede,
        "contradictions": contradictions,
        "stale_summaries": _summaries(notes, live, limit),
        "inbox": _inbox(db_path),
        "forget": _forget(clone, archived),
        "coverage": coverage,
    })
    report["summary"] = _summary(report)
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return report


def _summary(report: dict) -> dict:
    """Headline numbers; None where the source has no data (never a misleading 0)."""
    def total(key):
        section = report[key]
        return section.get("total") if section.get("available") else None
    inbox, forget = report["inbox"], report["forget"]
    return {"notes": report["coverage"]["notes"],
            "archived": report["coverage"]["archived"],
            "pending_forget": forget.get("pending") if forget.get("available") else None,
            "never_recalled": total("never_recalled"),
            "recalled_unread": total("recalled_unread"),
            "stale": total("stale"),
            "failed_verifications": total("failed_verifications"),
            "contradictions": total("contradictions"),
            "supersede_candidates": total("supersede_candidates"),
            "stale_summaries": total("stale_summaries"),
            "pending_review": inbox.get("pending") if inbox.get("available") else None,
            "pending_index": report["coverage"]["pending"]}


def store_paths(cfg, profile: str) -> tuple[Path | None, Path]:
    """The profile's (clone, index) under the same isolation guard recall uses."""
    from memd.profiles import guard_paths
    clone, db = guard_paths(profile, cfg.clone, cfg.db)
    return (Path(clone) if clone is not None else None), Path(db)


def report(cfg, profile: str, *, limit: int = LIST_LIMIT, fresh: bool = False) -> dict:
    """compute() for the profile's store, cached per store for CACHE_TTL_S seconds."""
    clone, db_path = store_paths(cfg, profile)
    limit = max(1, min(MAX_LIMIT, int(limit)))
    key = (profile, str(db_path), limit)
    now = _clock()
    if not fresh:
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None and now - hit[0] < CACHE_TTL_S:
                return {**hit[1], "cached": True, "age_s": round(now - hit[0], 1)}
    result = compute(cfg, profile, clone=clone, db_path=db_path, limit=limit)
    if result.get("ok"):
        with _cache_lock:
            for stale_key in [k for k, (ts, _) in _cache.items() if now - ts >= CACHE_TTL_S]:
                _cache.pop(stale_key, None)
            _cache[key] = (now, result)
    return {**result, "cached": False, "age_s": 0.0}


# --------------------------------------------------------------------------- CLI


def _count(value) -> str:
    return "no data" if value is None else str(value)


def format_text(r: dict) -> str:
    """The report as plain text (for cron mail)."""
    if not r.get("ok"):
        return f"memory health: {r.get('err', 'unavailable')}\n"
    s, cov, usage = r["summary"], r["coverage"], r["usage"]
    lines = [f"memory health for {r['profile']} at {r['generated_at']}",
             f"notes {s['notes']} live, {cov['superseded']} superseded, {cov['archived']} archived; index pending "
             f"{cov['pending_vectors']} note vectors, {cov['pending_chunks']} chunk sets"]
    if usage["available"]:
        lines.append(f"usage window {usage['window_days']} days since {usage['since']}: "
                     f"{usage['recalls']} recalls, {usage['reads']} reads"
                     + (" (newest recalls only)" if usage.get("truncated") else ""))
    else:
        lines.append(f"usage: no data ({usage['reason']})")
    lines.append("")
    rows = [("never recalled", "never_recalled"), ("recalled, never read", "recalled_unread"),
            ("stale", "stale"), ("failed verifications", "failed_verifications"),
            ("contradictions", "contradictions"), ("supersede candidates", "supersede_candidates"),
            ("stale summaries", "stale_summaries"), ("pending review", "pending_review"),
            ("archived", "archived"), ("pending forget", "pending_forget")]
    for label, key in rows:
        lines.append(f"  {label:<22} {_count(s[key])}")

    def section(title: str, key: str, fmt) -> None:
        sec = r[key]
        lines.append("")
        if not sec.get("available"):
            lines.append(f"{title}: no data ({sec['reason']})")
            return
        lines.append(f"{title}: {sec['total']}")
        for item in sec["items"]:
            lines.append("  " + fmt(item))
        if sec["total"] > len(sec["items"]):
            lines.append(f"  ... and {sec['total'] - len(sec['items'])} more")

    section("never recalled", "never_recalled", lambda i: f"{i['slug']}  (importance {i['importance']})")
    section(f"recalled {MIN_SHOWN}+ times, never read", "recalled_unread",
            lambda i: f"{i['slug']}  shown {i['shown']}")
    section("most useful", "most_useful", lambda i: f"{i['slug']}  read {i['reads']} of {i['shown']} shown")
    section("stale", "stale", lambda i: f"{i['slug']}  {i['reason']}"
            + (f", as of {i['as_of']}" if i.get("as_of") else ""))
    section("failed verifications", "failed_verifications",
            lambda i: f"{i['slug']}  {i['checked_at']}: {i['probe']}")
    section("contradictions", "contradictions",
            lambda c: f"{c['subject']} | {c['predicate']}: " + "; ".join(
                f"{v['object']} ({', '.join(n['slug'] for n in v['notes'])})" for v in c["values"]))
    section("supersede candidates", "supersede_candidates",
            lambda i: f"{i['slug']}  closed by {', '.join(i['closed_by'])}")
    section("stale summaries", "stale_summaries", lambda i: f"{i['slug']}  {'; '.join(i['reasons'])}")
    heat = r["heatmap"]
    if heat["available"]:
        lines.append("")
        lines.append("age by tag (notes; stale):")
        width = max([len(row["key"]) for row in heat["tags"]["rows"]] + [8])
        lines.append("  " + " " * width + "".join(f"{b:>9}" for b in heat["buckets"]) + "    stale")
        for row in heat["tags"]["rows"]:
            lines.append("  " + row["key"][:width].ljust(width) + "".join(f"{c:>9}" for c in row["cells"])
                         + f"{row['stale']:>9}")
        if heat["tags"]["total_rows"] > len(heat["tags"]["rows"]):
            lines.append(f"  ... and {heat['tags']['total_rows'] - len(heat['tags']['rows'])} more tags")
    inbox = r["inbox"]
    lines.append("")
    lines.append(f"inbox: {inbox['pending']} pending" if inbox["available"]
                 else f"inbox: no data ({inbox['reason']})")
    forget = r["forget"]
    lines.append(f"forget: {forget['archived']} archived, {forget['pending']} proposed on {forget['branch']}"
                 if forget["available"] else f"forget: {forget['archived']} archived ({forget['reason']})")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="mem-health",
        description="Report where this store's memory is rotting or wasted (read-only).")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--limit", type=int, default=LIST_LIMIT,
                    help=f"items per list (default {LIST_LIMIT}, at most {MAX_LIMIT})")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    from memd.config import Config
    cfg = Config.from_env()
    if cfg.db is None:
        print("mem-health: no index configured (MEMD_DB / MEMD_PROFILE)", file=sys.stderr)
        return 2
    try:
        clone, db_path = store_paths(cfg, cfg.profile)
        out = compute(cfg, cfg.profile, clone=clone, db_path=db_path, limit=args.limit)
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"mem-health: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(out))
    else:
        sys.stdout.write(format_text(out))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
