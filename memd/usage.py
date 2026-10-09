"""Recall usage log: what agents read after a recall, as a ranking signal and eval source.

Each store gets a derived SQLite file next to its index (``<index stem>.usage.db``,
mode 0600, safe to delete) with two tables:

  recalls  one row per recall served over HTTP or MCP: a recall id (returned to
           the caller as ``recall_id``), time, caller label, the normalised query
           text and its hash, and the returned match slugs in rank order.
  reads    one row per successful ``read``: time, caller, slug, and the recall it
           is attributed to -- the ``recall_id`` the caller passed back, else the
           same caller's latest recall within READ_WINDOW_S that returned the slug.

Privacy: queries can hold anything a user typed. MEMD_USAGE_LOG=on (default)
keeps the normalised text in this file only, never in logs or responses;
``hash`` keeps only its hash (no golden-set export then); ``off`` records
nothing. Rows older than MEMD_USAGE_RETENTION_DAYS (default 90) are pruned on
every write.

Signals, per recall that led to at least one read (a recall with no read says
nothing: the excerpt may have been enough):
  positive       a returned slug read within READ_WINDOW_S of the recall;
  weak negative  an unread returned slug ranked above a read one, or in the
                 top SKIP_TOP ("examined and passed over").

Learning (MEMD_USAGE_BOOST=on, default off because its effect cannot be measured
offline): a per-note read rate, smoothed towards the store-wide rate with
PRIOR_STRENGTH pseudo-observations, becomes a fusion weight in
[1 - BOOST_CAP, 1 + BOOST_CAP]; a note never shown or never judged keeps 1.0, so
new notes are not penalised. The boosted order may move a note at most MAX_SHIFT
places from its unboosted fused position, and the reranker still judges the
head. The risk is rich-get-richer: notes read because they were ranked first
get ranked first. Skip-above negatives, the smoothing and the shift cap bound it;
retention lets old habits expire.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from pathlib import Path

from memd import metrics
from memd.query import normalize

log = logging.getLogger(__name__)

READ_WINDOW_S = 15 * 60     # a read this soon after a recall counts for it
SKIP_TOP = 3                # unread top-3 results of a recall that led to a read are weak negatives
PRIOR_STRENGTH = 5.0        # pseudo-observations at the store-wide read rate
BOOST_CAP = 0.15            # fusion weight stays within 1 +/- BOOST_CAP
MAX_SHIFT = 2               # a boosted note moves at most this many fused places
WEIGHTS_TTL_S = 60.0        # recompute note weights at most this often per file
_RECALL_ID = re.compile(r"^[0-9a-f]{16}$")
_PUNCT = re.compile(r"[^\w\s\-.:/@]")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recalls (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    caller TEXT NOT NULL DEFAULT '',
    query_hash TEXT NOT NULL,
    query TEXT,
    slugs TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recalls_ts ON recalls(ts);
CREATE INDEX IF NOT EXISTS recalls_caller_ts ON recalls(caller, ts);
CREATE TABLE IF NOT EXISTS reads (
    ts REAL NOT NULL,
    caller TEXT NOT NULL DEFAULT '',
    slug TEXT NOT NULL,
    recall_id TEXT,
    explicit INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS reads_ts ON reads(ts);
CREATE INDEX IF NOT EXISTS reads_recall ON reads(recall_id);
"""


def usage_path(db_path: Path | str) -> Path:
    """The usage file of the index at db_path (``memd.db`` -> ``memd.usage.db``)."""
    db_path = Path(db_path)
    return db_path.with_name(db_path.stem + ".usage.db")


def index_path(cfg, profile: str) -> Path | None:
    """The profile's existing index file, or None (no index: nothing to log next to)."""
    try:
        from memd.profiles import guard_paths
        db = guard_paths(profile, cfg.clone, cfg.db)[1]
    except Exception:
        return None
    return Path(db) if db is not None and Path(db).exists() else None


def normalize_query(query: str) -> str:
    """Case-folded query text without punctuation (inner '-_.:/@' kept), spaces collapsed."""
    return " ".join(normalize(_PUNCT.sub(" ", query or "")).casefold().split())


def query_hash(query: str) -> str:
    return hashlib.sha256(normalize_query(query).encode("utf-8")).hexdigest()[:32]


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Queries are private: create the file owner-only before SQLite opens it.
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(str(path), timeout=2.0)
    conn.executescript(_SCHEMA)
    return conn


def _prune(conn: sqlite3.Connection, retention_days: int, now: float) -> None:
    cutoff = now - retention_days * 86400
    conn.execute("DELETE FROM recalls WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM reads WHERE ts < ?", (cutoff,))


def log_recall(db_path: Path | str | None, query: str, slugs: list[str], *, cfg,
               caller: str = "", now: float | None = None, rid: str | None = None) -> str | None:
    """Record one recall; return its id, or None when logging is off or fails.

    Never raises and never logs the query: usage logging must not be able to fail
    or leak a recall.
    """
    if cfg.usage_log == "off" or db_path is None or not (query or "").strip():
        return None
    now = time.time() if now is None else now
    rid = rid if isinstance(rid, str) and _RECALL_ID.match(rid) else secrets.token_hex(8)
    text = normalize_query(query) if cfg.usage_log == "on" else None
    try:
        conn = _connect(usage_path(db_path))
        try:
            with conn:
                _prune(conn, cfg.usage_retention_days, now)
                conn.execute(
                    "INSERT INTO recalls(id, ts, caller, query_hash, query, slugs) VALUES (?,?,?,?,?,?)",
                    (rid, now, caller or "", query_hash(query), text,
                     json.dumps(list(dict.fromkeys(slugs)))))
        finally:
            conn.close()
    except Exception as exc:
        log.warning("usage log unavailable (%s); recall served without a recall_id", type(exc).__name__)
        metrics.inc("memd_usage_log_events_total", kind="recall", result="failed")
        return None
    metrics.inc("memd_usage_log_events_total", kind="recall", result="logged")
    return rid


def log_read(db_path: Path | str | None, slug: str, *, cfg, recall_id: str | None = None,
             caller: str = "", now: float | None = None) -> None:
    """Record one read, attributed to the recall it followed when one can be found."""
    if cfg.usage_log == "off" or db_path is None or not slug:
        return
    now = time.time() if now is None else now
    path = usage_path(db_path)
    try:
        if not path.exists():
            return          # no recall was ever logged here, so nothing to attribute
        conn = _connect(path)
        try:
            with conn:
                _prune(conn, cfg.usage_retention_days, now)
                attributed, explicit = None, 0
                if isinstance(recall_id, str) and _RECALL_ID.match(recall_id):
                    row = conn.execute("SELECT slugs FROM recalls WHERE id = ?", (recall_id,)).fetchone()
                    if row is not None and slug in json.loads(row[0]):
                        attributed, explicit = recall_id, 1
                if attributed is None:
                    for rid, slugs in conn.execute(
                            "SELECT id, slugs FROM recalls WHERE caller = ? AND ts >= ? AND ts <= ? "
                            "ORDER BY ts DESC", (caller or "", now - READ_WINDOW_S, now)):
                        if slug in json.loads(slugs):
                            attributed = rid
                            break
                conn.execute("INSERT INTO reads(ts, caller, slug, recall_id, explicit) VALUES (?,?,?,?,?)",
                             (now, caller or "", slug, attributed, explicit))
        finally:
            conn.close()
    except Exception as exc:
        log.warning("usage log unavailable (%s); read not recorded", type(exc).__name__)
        metrics.inc("memd_usage_log_events_total", kind="read", result="failed")
        return
    metrics.inc("memd_usage_log_events_total", kind="read", result="logged")


def record_recall(cfg, profile: str, query: str, shaped: dict) -> str | None:
    """log_recall for a rendered recall result: the matches the caller was shown."""
    from memd.actor import get_actor
    slugs = [e["slug"] for e in shaped.get("excerpts", []) if e.get("slug")]
    return log_recall(index_path(cfg, profile), query, slugs, cfg=cfg, caller=get_actor())


def record_federated(cfg_for, query: str, shaped: dict) -> str | None:
    """One recall_id for a recall across stores, logged in each store's own log
    with the matches shown from that store. A read names its store, so it finds
    the id there. None when no store logged it."""
    from memd.actor import get_actor
    by_store: dict[str, list[str]] = {}
    for e in shaped.get("excerpts", []):
        if e.get("slug") and e.get("store"):
            by_store.setdefault(e["store"], []).append(e["slug"])
    rid, logged = secrets.token_hex(8), False
    for store, slugs in by_store.items():
        try:
            cfg = cfg_for(store)
            logged |= log_recall(index_path(cfg, store), query, slugs, cfg=cfg,
                                 caller=get_actor(), rid=rid) == rid
        except Exception:
            continue    # usage logging must never fail a recall
    return rid if logged else None


def record_read(cfg, profile: str, receipt: dict, recall_id=None) -> None:
    """log_read for a successful read receipt. Never raises."""
    from memd.actor import get_actor
    try:
        log_read(index_path(cfg, profile), receipt.get("slug") or "", cfg=cfg,
                 recall_id=recall_id, caller=get_actor())
    except Exception:
        pass


# --------------------------------------------------------------------------- signals


@dataclasses.dataclass
class Recall:
    id: str
    ts: float
    caller: str
    query_hash: str
    query: str | None
    slugs: list[str]
    read: set[str] = dataclasses.field(default_factory=set)

    def negatives(self) -> list[str]:
        """Unread slugs ranked above a read one or in the top SKIP_TOP; [] without a read."""
        if not self.read:
            return []
        deepest = max(self.slugs.index(s) for s in self.read)
        return [s for i, s in enumerate(self.slugs)
                if s not in self.read and (i < deepest or i < SKIP_TOP)]


@dataclasses.dataclass
class NoteSignal:
    shown: int = 0          # recalls that returned it
    reads: int = 0          # positives
    skips: int = 0          # weak negatives


def load_recalls(path: Path | str) -> list[Recall]:
    """Every retained recall with the returned slugs read within READ_WINDOW_S."""
    path = Path(path)
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    try:
        recalls = {r[0]: Recall(r[0], r[1], r[2], r[3], r[4], json.loads(r[5])) for r in conn.execute(
            "SELECT id, ts, caller, query_hash, query, slugs FROM recalls ORDER BY ts")}
        for rid, slug, ts in conn.execute(
                "SELECT recall_id, slug, ts FROM reads WHERE recall_id IS NOT NULL"):
            rec = recalls.get(rid)
            if rec is not None and slug in rec.slugs and 0 <= ts - rec.ts <= READ_WINDOW_S:
                rec.read.add(slug)
        return list(recalls.values())
    finally:
        conn.close()


def note_signals(recalls: list[Recall]) -> dict[str, NoteSignal]:
    out: dict[str, NoteSignal] = {}
    for rec in recalls:
        for s in rec.slugs:
            out.setdefault(s, NoteSignal()).shown += 1
        for s in rec.read:
            out[s].reads += 1
        for s in rec.negatives():
            out[s].skips += 1
    return out


def note_weights(signals: dict[str, NoteSignal]) -> dict[str, float]:
    """Fusion weight per judged note, in [1 - BOOST_CAP, 1 + BOOST_CAP].

    rate = (reads + PRIOR_STRENGTH * base) / (reads + skips + PRIOR_STRENGTH),
    where base is the store-wide read share of judged results; the weight is the
    rate's normalised distance from base, scaled by BOOST_CAP. Notes without a
    read or skip are left out (weight 1.0).
    """
    reads = sum(s.reads for s in signals.values())
    skips = sum(s.skips for s in signals.values())
    if reads + skips == 0:
        return {}
    base = reads / (reads + skips)
    out: dict[str, float] = {}
    for slug, sig in signals.items():
        n = sig.reads + sig.skips
        if n == 0:
            continue
        rate = (sig.reads + PRIOR_STRENGTH * base) / (n + PRIOR_STRENGTH)
        span = (1 - base) if rate >= base else base
        lift = (rate - base) / span if span > 0 else 0.0
        out[slug] = 1.0 + BOOST_CAP * max(-1.0, min(1.0, lift))
    return out


_weights_cache: dict[str, tuple[float, dict[str, float]]] = {}
_weights_lock = threading.Lock()


def weights_for(db_path: Path | str) -> dict[str, float]:
    """Cached note_weights for an index's usage file ({} when absent or unreadable)."""
    path = usage_path(db_path)
    key = str(path)
    now = time.monotonic()
    with _weights_lock:
        hit = _weights_cache.get(key)
        if hit is not None and now - hit[0] < WEIGHTS_TTL_S:
            return hit[1]
    try:
        weights = note_weights(note_signals(load_recalls(path)))
    except Exception as exc:
        log.warning("usage weights unavailable (%s)", type(exc).__name__)
        weights = {}
    with _weights_lock:
        _weights_cache[key] = (now, weights)
    return weights


def bounded_reorder(base: list[str], score: dict[str, float], max_shift: int = MAX_SHIFT) -> list[str]:
    """Order base by score (desc), but move no item more than max_shift places.

    At each output position the best-scoring item within reach (base position
    <= i + max_shift) is taken, unless an item would otherwise fall more than
    max_shift places below its base position; that one goes first.
    """
    pos = {s: i for i, s in enumerate(base)}
    remaining = list(base)
    out: list[str] = []
    for i in range(len(base)):
        due = [s for s in remaining if pos[s] <= i - max_shift]
        if due:
            pick = min(due, key=pos.__getitem__)
        else:
            reach = [s for s in remaining if pos[s] <= i + max_shift]
            pick = max(reach, key=lambda s: (score.get(s, 0.0), -pos[s]))
        remaining.remove(pick)
        out.append(pick)
    return out


# --------------------------------------------------------------------------- CLI


def stats(recalls: list[Recall], *, top: int = 10) -> dict:
    """Aggregate counts only; never query text."""
    signals = note_signals(recalls)
    engaged = [r for r in recalls if r.read]
    by_rank: dict[int, list[int]] = {}
    for rec in recalls:
        for i, s in enumerate(rec.slugs[:10]):
            by_rank.setdefault(i + 1, [0, 0])
            by_rank[i + 1][0] += 1
            by_rank[i + 1][1] += s in rec.read
    weights = note_weights(signals)
    ranked = sorted(signals.items(), key=lambda kv: (-kv[1].reads, kv[0]))
    return {
        "recalls": len(recalls),
        "distinct_queries": len({r.query_hash for r in recalls}),
        "callers": len({r.caller for r in recalls}),
        "zero_result_recalls": sum(not r.slugs for r in recalls),
        "recalls_with_read": len(engaged),
        "read_rate": round(len(engaged) / len(recalls), 4) if recalls else 0.0,
        "read_rate_by_rank": {str(k): round(v[1] / v[0], 4) for k, v in sorted(by_rank.items())},
        "top_read": [{"slug": s, **dataclasses.asdict(sig), "weight": round(weights.get(s, 1.0), 4)}
                     for s, sig in ranked[:top] if sig.reads],
        "most_skipped": [{"slug": s, **dataclasses.asdict(sig), "weight": round(weights.get(s, 1.0), 4)}
                         for s, sig in sorted(signals.items(), key=lambda kv: (-kv[1].skips, kv[0]))[:top]
                         if sig.skips],
    }


def _split(qhash: str) -> str:
    """eval/README's one-in-three test split, keyed on the query hash so re-exports agree."""
    return "test" if int(qhash[:8], 16) % 3 == 0 else "dev"


def golden_candidates(recalls: list[Recall], *, min_reads: int = 1, split: str | None = None,
                      exclude_queries: set[str] = frozenset()) -> list[dict]:
    """Candidate golden rows: each read slug of a logged query as a grade-2 candidate."""
    grouped: dict[str, dict] = {}
    for rec in recalls:
        if not rec.query or not rec.read or rec.query in exclude_queries:
            continue
        g = grouped.setdefault(rec.query_hash, {"query": rec.query, "recalls": 0, "reads": {}, "last": 0.0})
        g["recalls"] += 1
        g["last"] = max(g["last"], rec.ts)
        for s in rec.read:
            g["reads"][s] = g["reads"].get(s, 0) + 1
    rows = []
    for qh, g in sorted(grouped.items(), key=lambda kv: kv[1]["last"]):
        slugs = sorted((s for s, n in g["reads"].items() if n >= min_reads),
                       key=lambda s: (-g["reads"][s], s))
        if not slugs:
            continue
        rows.append({
            "id": f"usage-{qh[:10]}", "query": g["query"], "category": "usage",
            "split": split or _split(qh),
            "gold": [{"slug": s, "grade": 2} for s in slugs],
            "source": "usage", "needs_review": True,
            "why": (f"read after {g['recalls']} logged recall(s); a read is not proof of an "
                    "answer: confirm each slug, regrade, set category, drop needs_review"),
            "reads": {s: g["reads"][s] for s in slugs},
            "last_seen": time.strftime("%Y-%m-%d", time.gmtime(g["last"])),
        })
    return rows


def _golden_queries(path: Path) -> set[str]:
    out = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                row = json.loads(line)
                if isinstance(row, dict) and isinstance(row.get("query"), str):
                    out.add(normalize_query(row["query"]))
    return out


def _resolve_path(args) -> Path | None:
    if args.usage_db:
        return Path(args.usage_db)
    from memd.config import Config
    cfg = Config.from_env()
    return usage_path(cfg.db) if cfg.db is not None else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="mem-usage",
        description="Inspect the recall usage log and export golden-set candidates from it.")
    ap.add_argument("--usage-db", help="usage file (default: next to this store's index, MEMD_PROFILE/MEMD_DB)")
    sub = ap.add_subparsers(dest="command", required=True)
    st = sub.add_parser("stats", help="counts, read rate by rank, most read and most skipped notes")
    st.add_argument("--json", action="store_true")
    st.add_argument("--top", type=int, default=10)
    ex = sub.add_parser("export-golden", help="write candidate golden rows (needs_review) as JSONL")
    ex.add_argument("--out", help="output file (default stdout); created owner-only")
    ex.add_argument("--min-reads", type=int, default=1, help="reads of a slug for the same query (default 1)")
    ex.add_argument("--split", choices=["dev", "test", "fresh"],
                    help="force one split (default: one in three test, by query hash)")
    ex.add_argument("--exclude", type=Path, action="append", default=[],
                    help="golden JSONL whose queries to skip (repeatable)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    path = _resolve_path(args)
    if path is None:
        print("mem-usage: no index configured (MEMD_DB / MEMD_PROFILE) and no --usage-db", file=sys.stderr)
        return 2
    try:
        recalls = load_recalls(path)
    except sqlite3.Error as exc:
        print(f"mem-usage: cannot read {path}: {exc}", file=sys.stderr)
        return 2

    if args.command == "stats":
        out = stats(recalls, top=args.top)
        if args.json:
            print(json.dumps(out))
            return 0
        print(f"usage log: {path}")
        print(f"recalls {out['recalls']} ({out['distinct_queries']} distinct queries, "
              f"{out['callers']} callers, {out['zero_result_recalls']} with no match)")
        print(f"recalls followed by a read: {out['recalls_with_read']} ({out['read_rate']:.1%})")
        if out["read_rate_by_rank"]:
            print("read rate by rank: " + "  ".join(f"{k}:{v:.0%}" for k, v in out["read_rate_by_rank"].items()))
        for title, key in (("most read", "top_read"), ("most skipped", "most_skipped")):
            if out[key]:
                print(f"{title}:")
                for row in out[key]:
                    print(f"  {row['slug']}  shown {row['shown']} read {row['reads']} "
                          f"skipped {row['skips']} weight {row['weight']:.3f}")
        return 0

    exclude: set[str] = set()
    for golden in args.exclude:
        exclude |= _golden_queries(golden)
    rows = golden_candidates(recalls, min_reads=max(1, args.min_reads), split=args.split,
                             exclude_queries=exclude)
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    if args.out:
        out_path = Path(args.out)
        fd = os.open(out_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"mem-usage: wrote {len(rows)} candidate rows to {out_path}; review before adding them "
              "to a golden set", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
