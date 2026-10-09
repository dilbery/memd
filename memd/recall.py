"""Recall cached notes without running bulk embedding on the request path.

recall(query, profile="amber", k=8, *, cfg, include_core=True, host=None, tags=None)
returns up to k query matches (1..50), plus eligible core notes when requested.
An empty query returns only core notes. An empty corpus or scope can return [].

Core eligibility is importance>=4 or pinned=True; matched provenance distinguishes
query results from core-only entries. Rendering separately limits the core count,
selects query-centered excerpts and reports omissions within a character budget.

An empty cache can seed lexical data without waiting for the clone lock. A stale
lexical HEAD or pending vectors schedules a background refresh while this request
uses the existing cache. Host/tag scopes apply before retrieval limits.

The query is distilled to its rarer terms (memd.query) for the keyword arm. The
vector arm searches per-chunk vectors (memd.chunk) and scores a note by its best
chunk; MEMD_RECALL_VECTORS=notes restores whole-note vectors. Arms are fused by
weighted reciprocal-rank fusion where a keyword hit's weight grows with the
IDF-weighted share of query terms the note contains; with MEMD_USAGE_BOOST=on a
bounded per-note weight learned from reads after recalls (memd.usage) nudges the
fused order by at most two places. The fused head is reranked,
each note shown by its best-matching chunk when that lies past its opening. If
reranking fails, vector-distance order wins and BM25 fills any remaining places;
if embedding also fails, use BM25 alone. Core notes remain
available when model backends fail, provided the cache is readable.
"""
from __future__ import annotations

import dataclasses
import json
import datetime
import re
import time
from itertools import zip_longest

import sqlite_vec

from memd.chunk import chunk_body
from memd.config import Config
from memd import metrics
from memd.embed import embed_with_deadline
from memd.index import open_db, head_in_index, lexical_head_in_index, pending_vectors
from memd.profiles import guard_paths
from memd.refresh import request_refresh, ensure_lexical
from memd.rerank import rerank
from memd.staleness import as_of
from memd.store import Note, git_head_sha, not_archived_sql
from memd.query import Distilled, coverage, distill, match_expr, normalize

VEC_K = 50
# Per-chunk vector arm (2026-09-28, not yet measured on the golden set; see
# eval/README.md). A note scores its best chunk's cosine similarity, plus
# CHUNK_HIT_BONUS for each further chunk of it among the VEC_CHUNK_K nearest (at
# most CHUNK_HIT_CAP), so a note on-topic throughout edges out a single stray hit.
VEC_CHUNK_K = 200            # chunk neighbours scanned before collapsing to VEC_K notes
CHUNK_HIT_BONUS = 0.005
CHUNK_HIT_CAP = 2
FTS_K = 50
UNION_K = 100                # preserves both candidate arms for k up to 50
RERANK_MAX_CANDIDATES = 15   # a cross-encoder reranker costs ~29 ms/doc on a GPU host: 15 docs ~435 ms of the 900 ms deadline
RERANK_BODY_CHARS = 1000     # bound payload; retain original bodies for rendering
# The cross-encoder judges "title + opening body". Titles here are dense summaries;
# measured 2026-09-28 on 147 held-out queries: nDCG@10 +0.045 with the fresh arm,
# no category worse. A query-centred body window instead of the opening text
# was tried and lost on paraphrase/multi queries.
RERANK_TITLE = True
# When a note's best-matching chunk lies past that opening, the reranker sees the
# opening's first RERANK_LEAD_CHARS (what the note is) and then that chunk (the
# fact asked about) instead: an umbrella note's answer is rarely in its opening.
RERANK_CHUNK = True
RERANK_LEAD_CHARS = 200
FUSION = "rrf"              # "interleave" restores the pre-2026-09-27 zip of both arms
RRF_K = 20
KW_WEIGHT = 1.0             # keyword-arm RRF multiplier (scaled by IDF coverage below)
KW_COVERAGE_FLOOR = 0.5     # keyword term weight = KW_WEIGHT * (floor + (1-floor) * coverage)
EMBED_QUERY = "raw"         # "raw" | "distilled" | "auto"
AUTO_DISTILL_TOKENS = 12    # "auto": embed the distilled query when the raw query has more tokens than this
RERANK_BLEND = 1.0          # 0: reranker order wins the head; >0: RRF-blend reranker and fused ranks
BLEND_K = 10
KW_SEATS = 3                # keyword hits always given this many seats in the rerank head

# "What is the current X" (2026-09-28): a strict present-state intent gate and a
# third candidate list of the newest topical notes. The newest note on a topic
# was usually outside the pool; this puts it in and lets the reranker judge it.
# Tried and rejected: a recency multiplier on fused scores, reserved rerank seats.
CURRENT_INTENT = re.compile(
    r"\b(current|currently|latest|right now|at the moment|these days|as of now|at present|nowadays)\b"
    r"|\bnow\s+(after|that)\b|\bnow\s*[?.!]*\s*$", re.I)
FRESH_K = 30                 # fresh-arm candidates (newest first)
FRESH_POOL = 200             # FTS matches scanned for the fresh arm
FRESH_MIN_COVERAGE = 0.3     # a fresh candidate must hold this IDF share of the query terms
FRESH_WEIGHT = 1.0           # RRF weight of the fresh arm


def _row_to_note(row: dict) -> Note:
    try:
        metadata = json.loads(row.get("metadata") or "{}")
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    # Only dataclass fields are accepted; canonical indexed columns win over
    # cached metadata. Old cache rows stay explicitly unverified until hydrated.
    allowed = {f.name for f in dataclasses.fields(Note)}
    values = {k: v for k, v in metadata.items() if k in allowed}
    values.setdefault("grounding", "unverified-local")
    values.update(
        slug=row["slug"], path=row["path"], title=row["title"],
        profile=row["profile"], host=row["host"], importance=row["importance"],
        last_used=row["last_used"], superseded_by=row["superseded_by"],
        body=row["body"], git_blob=row.get("git_blob", ""),
    )
    return Note(**values)


def _archived_filter(include_archived: bool, alias: str = "n") -> str:
    """" AND <not archived>" unless archived notes (memd.forget) were asked for."""
    return "" if include_archived else f" AND {not_archived_sql(alias)}"


def _fetch_notes(db, slugs: list[str], include_archived: bool = False) -> dict[str, Note]:
    if not slugs:
        return {}
    qmarks = ",".join("?" * len(slugs))
    # belt-and-braces: superseded notes are skipped at index time, but never
    # surface one even if a stale row lingers (filter superseded_by IS NULL).
    cur = db.execute(
        f"SELECT slug, path, title, profile, host, importance, last_used, "
        f"superseded_by, body, git_blob, metadata FROM notes WHERE slug IN ({qmarks}) "
        f"AND superseded_by IS NULL" + _archived_filter(include_archived, ""),
        slugs,
    )
    cols = [c[0] for c in cur.description]
    return {r[0]: _row_to_note(dict(zip(cols, r))) for r in cur.fetchall()}


def _core_set(db) -> list[Note]:
    # Archived notes (memd.forget) are never core, even with include_archived.
    cur = db.execute(
        "SELECT slug, path, title, profile, host, importance, last_used, "
        "superseded_by, body, git_blob, metadata FROM notes "
        "WHERE (importance >= 4 OR json_extract(metadata,'$.pinned') = 1) "
        "AND superseded_by IS NULL" + _archived_filter(False, "") + " "
        "ORDER BY COALESCE(json_extract(metadata,'$.pinned'),0) DESC, importance DESC, slug"
    )
    cols = [c[0] for c in cur.description]
    return [_row_to_note(dict(zip(cols, r))) for r in cur.fetchall()]


@dataclasses.dataclass(frozen=True)
class _ChunkHit:
    ordinal: int
    start: int
    end: int
    similarity: float       # cosine similarity to the query


def _chunk_hits(db, qvec: list[float], scoped: list[str] | None, k: int,
                include_archived: bool = False) -> dict[str, list[_ChunkHit]]:
    """The k nearest current chunks, grouped by note, best first within each note."""
    if scoped == []:
        return {}
    # slug is a vec0 metadata column: the scope filters inside the KNN, before k.
    # Notes gate on chunk_blob so chunks of a superseded or changed note never surface.
    scope_sql = " AND v.slug IN (SELECT value FROM json_each(?)) " if scoped is not None else " "
    rows = db.execute(
        "SELECT v.slug, v.ordinal, v.start_char, v.end_char, v.distance FROM vec_chunks v "
        "JOIN notes n ON n.slug = v.slug "
        "WHERE v.embedding MATCH ? AND k = ? AND n.superseded_by IS NULL "
        "AND n.chunk_blob = n.git_blob" + _archived_filter(include_archived) + scope_sql +
        "ORDER BY v.distance",
        (sqlite_vec.serialize_float32(qvec), k) + ((json.dumps(scoped),) if scoped is not None else ()),
    ).fetchall()
    hits: dict[str, list[_ChunkHit]] = {}
    for slug, ordinal, start, end, distance in rows:
        if distance is None:        # a zero vector has no cosine; never rank it first
            continue
        hits.setdefault(slug, []).append(_ChunkHit(ordinal, start, end, 1.0 - distance))
    return hits


def _unchunked_similarity(db, qvec: list[float], scoped: list[str] | None,
                          include_archived: bool = False) -> dict[str, float]:
    """Whole-note cosine for notes whose note vector is current but chunks are not.

    Only non-empty while chunks are (re)built after an upgrade or a
    _CHUNK_VERSION bump, so those notes stay semantically findable meanwhile.
    """
    slugs = [r[0] for r in db.execute(
        "SELECT slug FROM notes WHERE superseded_by IS NULL AND vector_blob = git_blob "
        "AND (chunk_blob IS NULL OR chunk_blob != git_blob)" + _archived_filter(include_archived, ""))]
    if scoped is not None:
        allowed = set(scoped)
        slugs = [s for s in slugs if s in allowed]
    if not slugs:
        return {}
    rows = db.execute(
        "SELECT slug, vec_distance_cosine(embedding, ?) FROM vec_notes "
        "WHERE slug IN (SELECT value FROM json_each(?))",
        (sqlite_vec.serialize_float32(qvec), json.dumps(slugs)),
    ).fetchall()
    return {slug: 1.0 - d for slug, d in rows if d is not None}


def _vector_arm(db, qvec: list[float], scoped: list[str] | None = None,
                include_archived: bool = False) -> list[str]:
    """Notes by their best chunk's similarity (max), plus a small multi-chunk bonus."""
    if scoped == []:
        return []
    hits = _chunk_hits(db, qvec, scoped, VEC_CHUNK_K, include_archived)
    scores = {slug: h[0].similarity + CHUNK_HIT_BONUS * min(CHUNK_HIT_CAP, len(h) - 1)
              for slug, h in hits.items()}
    for slug, similarity in _unchunked_similarity(db, qvec, scoped, include_archived).items():
        scores.setdefault(slug, similarity)
    # dicts keep KNN order, so equal scores stay in distance order.
    position = {s: i for i, s in enumerate(scores)}
    return sorted(scores, key=lambda s: (-scores[s], position[s]))[:VEC_K]


def _best_chunks(db, qvec: list[float], slugs: list[str]) -> dict[str, _ChunkHit]:
    """Each listed note's best-matching chunk (for the reranker's view of it)."""
    if not slugs:
        return {}
    try:
        hits = _chunk_hits(db, qvec, slugs, VEC_CHUNK_K, include_archived=True)
    except Exception:
        return {}           # optional: the opening body is still a usable view
    return {slug: h[0] for slug, h in hits.items()}


def _note_arm(db, qvec: list[float], scoped: list[str] | None = None,
              include_archived: bool = False) -> list[str]:
    """The whole-note vector arm (MEMD_RECALL_VECTORS=notes)."""
    if scoped == []:
        return []
    q = sqlite_vec.serialize_float32(qvec)
    # belt-and-braces: superseded notes are not indexed, but also gate the arm
    # on notes.superseded_by IS NULL so a stale vec row can never surface one.
    # sqlite-vec requires k on MATCH; slug scope constrains its candidate pool.
    scope_sql = " AND v.slug IN (SELECT value FROM json_each(?)) " if scoped is not None else " "
    rows = db.execute(
        "SELECT v.slug FROM vec_notes v "
        "JOIN notes n ON n.slug = v.slug "
        "WHERE v.embedding MATCH ? AND k = ? AND n.superseded_by IS NULL "
        "AND n.vector_blob = n.git_blob" + _archived_filter(include_archived) + " " + scope_sql +
        "ORDER BY v.distance",
        (q, VEC_K) + ((json.dumps(scoped),) if scoped is not None else ()),
    ).fetchall()
    return [r[0] for r in rows]


def _bm25_arm(db, distilled: Distilled, scoped: list[str] | None = None,
              include_archived: bool = False) -> list[str]:
    if scoped == []:
        return []
    if not distilled.terms:
        return []
    match = match_expr(distilled.terms)
    try:
        # belt-and-braces: superseded notes are not indexed, but also gate the
        # arm on notes.superseded_by IS NULL so a stale fts row can't surface one.
        scope_sql = " AND n.slug IN (SELECT value FROM json_each(?)) " if scoped is not None else " "
        rows = db.execute(
            "SELECT f.slug FROM fts_notes f "
            "JOIN notes n ON n.slug = f.slug "
            "WHERE fts_notes MATCH ? AND n.superseded_by IS NULL"
            + _archived_filter(include_archived) + " " + scope_sql +
            "ORDER BY bm25(fts_notes) LIMIT ?",
            (match,) + ((json.dumps(scoped),) if scoped is not None else ()) + (FTS_K,),
        ).fetchall()
    except Exception:
        return []
    return [r[0] for r in rows]


def _query_for_embedding(query: str, distilled: Distilled) -> str:
    """The text the vector arm embeds, per EMBED_QUERY."""
    distil = EMBED_QUERY == "distilled" or (
        EMBED_QUERY == "auto" and len(query.split()) > AUTO_DISTILL_TOKENS)
    if not distil or not distilled.terms:
        return query
    lowered = normalize(query).lower()
    return " ".join(sorted(distilled.terms, key=lambda t: (
        lowered.find(t) if t in lowered else len(lowered), t)))


def _interleave(vec_slugs: list[str], bm_slugs: list[str]) -> list[str]:
    seen: set[str] = set()
    union: list[str] = []
    for pair in zip_longest(vec_slugs, bm_slugs):
        for s in pair:
            if s is None or s in seen:
                continue
            seen.add(s)
            union.append(s)
    return union


def _note_date(note: Note) -> datetime.date | None:
    try:
        return as_of(note.to_dict())
    except Exception:
        return None


def _fresh_arm(db, distilled: Distilled, scoped: list[str] | None = None,
               include_archived: bool = False) -> list[str]:
    if scoped == []:
        return []
    if not distilled.terms:
        return []
    match = match_expr(distilled.terms)
    try:
        scope_sql = " AND n.slug IN (SELECT value FROM json_each(?)) " if scoped is not None else " "
        rows = db.execute(
            "SELECT f.slug FROM fts_notes f "
            "JOIN notes n ON n.slug = f.slug "
            "WHERE fts_notes MATCH ? AND n.superseded_by IS NULL"
            + _archived_filter(include_archived) + " " + scope_sql +
            "ORDER BY bm25(fts_notes) LIMIT ?",
            (match,) + ((json.dumps(scoped),) if scoped is not None else ()) + (FRESH_POOL,),
        ).fetchall()
    except Exception:
        return []
    slugs = [r[0] for r in rows]
    notes = _fetch_notes(db, slugs, include_archived)
    kept: list[tuple[datetime.date, int, str]] = []
    for idx, slug in enumerate(slugs):
        n = notes.get(slug)
        if n is None:
            continue
        d = _note_date(n)
        if d is None:
            continue
        cov = coverage(f"{n.title}\n{' '.join(map(str, n.tags))}\n{n.body}", distilled)
        if cov >= FRESH_MIN_COVERAGE:
            kept.append((d, idx, slug))
    kept.sort(key=lambda x: (-x[0].toordinal(), x[1]))
    return [k[2] for k in kept[:FRESH_K]]


def _fuse(vec_slugs: list[str], bm_slugs: list[str], notes_by_slug: dict[str, Note],
          distilled: Distilled, fresh_slugs: list[str] | None = None,
          weights: dict[str, float] | None = None) -> list[str]:
    """Order the union of both arms (FUSION): weighted RRF, or the legacy interleave.

    RRF: 1/(K+rank) per arm. A keyword hit is weighted by the IDF-weighted share
    of the distilled query terms the note actually contains, so a note matching
    every rare term outranks one that matched a single incidental word.

    weights (MEMD_USAGE_BOOST, memd.usage) multiply fused scores by a bounded
    per-note factor learned from reads after recalls; the result may move a
    note at most usage.MAX_SHIFT places from its unweighted position.
    """
    interleave_order = _interleave(vec_slugs, bm_slugs)
    if FUSION == "interleave":
        return interleave_order

    fresh = fresh_slugs or []
    seen_interleave = set(interleave_order)
    extra_fresh = [s for s in fresh if s not in seen_interleave]
    order = interleave_order + extra_fresh

    scores = dict.fromkeys(order, 0.0)
    for i, s in enumerate(vec_slugs):
        scores[s] += 1.0 / (RRF_K + i + 1)
    for i, s in enumerate(bm_slugs):
        n = notes_by_slug.get(s)
        if n is None:
            continue
        cov = coverage(f"{n.title}\n{' '.join(map(str, n.tags))}\n{n.body}", distilled)
        scores[s] += KW_WEIGHT * (KW_COVERAGE_FLOOR + (1 - KW_COVERAGE_FLOOR) * cov) / (RRF_K + i + 1)

    for i, s in enumerate(fresh):
        scores[s] += FRESH_WEIGHT / (RRF_K + i + 1)

    position = {s: i for i, s in enumerate(order)}
    fused = sorted(order, key=lambda s: (-scores[s], position[s]))
    if weights:
        from memd.usage import bounded_reorder
        fused = bounded_reorder(fused, {s: scores[s] * weights.get(s, 1.0) for s in fused})
    return fused


def _rerank_text(note: Note, hit: _ChunkHit | None = None) -> str:
    """The bounded text the cross-encoder judges a note by.

    Title and opening body, or, when the query's best chunk of the note lies
    beyond the opening, a short lead, the chunk's heading path and the chunk.
    """
    body = note.body[:RERANK_BODY_CHARS]
    if RERANK_CHUNK and hit is not None and hit.ordinal > 0 \
            and RERANK_BODY_CHARS < hit.end <= len(note.body):
        context = next((c.context for c in chunk_body(note.body)
                        if (c.start, c.end) == (hit.start, hit.end)), "")
        lead = note.body[:min(RERANK_LEAD_CHARS, hit.start)].rstrip()
        passage = note.body[hit.start:hit.end][:RERANK_BODY_CHARS]
        body = "\n".join(p for p in (lead, "…" if lead else "", context, passage) if p)
    return f"{note.title}\n{body}" if RERANK_TITLE and note.title else body


def _rerank_head(union: list[str], bm_slugs: list[str], n: int) -> list[str]:
    """The first n fused slugs, guaranteeing KW_SEATS of them to keyword hits.

    Exact identifiers only the BM25 arm can find must reach the reranker even
    when a full page of vector matches outscores them (regression 2026-08-15).
    """
    head = union[:n]
    in_union = set(union)
    bm = set(bm_slugs)
    need = min(KW_SEATS, n) - sum(s in bm for s in head)
    extra = [s for s in bm_slugs if s in in_union and s not in head][:max(0, need)]
    for s in extra:
        # evict the lowest-ranked non-keyword entry
        victim = next((v for v in reversed(head) if v not in bm), None)
        if victim is not None:
            head.remove(victim)
            head.append(s)

    return head


def recall(query: str, profile: str = "amber", k: int = 8, *, cfg: Config,
           include_core: bool = True, host: str | None = None,
           tags: list[str] | None = None, trace: dict | None = None,
           include_archived: bool = False) -> list[Note]:
    start = time.perf_counter()
    try:
        return _recall(query, profile, k, cfg=cfg, include_core=include_core, host=host,
                       tags=tags, trace=trace, include_archived=include_archived)
    finally:
        metrics.observe("memd_recall_arm_duration_seconds", time.perf_counter() - start, arm="total")


def _stage(arm: str, since: float) -> float:
    """Record one recall stage's duration; the time it ended."""
    now = time.perf_counter()
    metrics.observe("memd_recall_arm_duration_seconds", now - since, arm=arm)
    return now


def _recall(query: str, profile: str, k: int, *, cfg: Config, include_core: bool,
            host: str | None, tags: list[str] | None, trace: dict | None,
            include_archived: bool) -> list[Note]:
    # §9.5 hard isolation: derive clone/db from the REQUESTED profile and refuse
    # any cross-profile path BEFORE any open_db/git op. Never trust an ambient
    # cfg whose clone/db could belong to a different profile.
    clone, db_path = guard_paths(profile, cfg.clone, cfg.db)
    cfg = dataclasses.replace(cfg, clone=clone, db=db_path, profile=profile)
    db = open_db(cfg.db, dim=cfg.embed_dim)
    try:
        k = max(1, min(50, int(k)))
        # (A) Cached core candidates; the renderer applies the presentation cap.
        core = _core_set(db)

        # A read never runs the bulk embedder. Saves attempt to publish lexical
        # data before returning; receipts report failures. External Git changes
        # are picked up by the background worker.
        clone_head = git_head_sha(cfg.clone)
        # First use of a local CLI/hook may have no cache and no persistent
        # worker. Seed keyword data only, and never wait behind a Git writer.
        if clone_head and not db.execute("SELECT 1 FROM notes LIMIT 1").fetchone():
            try:
                ensure_lexical(cfg, blocking=False)
                core = _core_set(db)
            except (OSError, ValueError):
                pass
        # An empty sha means git could not be consulted (missing clone, not a repo,
        # no commits, or a hang hitting the 2s timeout). Treat the index as current
        # and serve it: comparing against "" would differ from any real head marker
        # and so trigger a full reindex on EVERY turn precisely when git is broken.
        if clone_head and (lexical_head_in_index(db) != clone_head or pending_vectors(db)):
            request_refresh(cfg)
        from memd.hosts import canonical_host
        wanted_tags = {str(t).casefold() for t in tags or []}
        def in_scope(note):
            return ((not host or canonical_host(note.host) == canonical_host(host))
                    and wanted_tags.issubset({str(t).casefold() for t in note.tags}))
        core = [n for n in core if in_scope(n)]
        if not query.strip():
            metrics.inc("memd_recall_order_total", order="core_only")
            return [dataclasses.replace(n, matched=False) for n in core] if include_core else []

        current = bool(CURRENT_INTENT.search(query))
        distilled = distill(db, query)
        if trace is not None:
            trace["terms"] = list(distilled.terms)

        if trace is not None:
            trace["current"] = current

        scoped = None
        if host or tags:
            # Apply scope before candidate limits so a rare host/tag is not
            # crowded out by more popular notes elsewhere in the corpus.
            scoped = []
            for slug, note_host, metadata in db.execute("SELECT slug,host,metadata FROM notes"):
                data = json.loads(metadata)
                if ((not host or canonical_host(note_host) == canonical_host(host))
                        and wanted_tags.issubset({str(t).casefold() for t in data.get("tags", [])})):
                    scoped.append(slug)

        # (B) vector arm (skipped on embed timeout/5xx/refused).
        embed_query = _query_for_embedding(query, distilled)
        began = time.perf_counter()
        qvec = embed_with_deadline(embed_query, cfg=cfg, ms=cfg.embed_deadline_ms)
        mark = _stage("embed", began)
        if qvec is None:
            # embed_with_deadline hides why; a failure near the deadline was the deadline.
            timed_out = (mark - began) * 1000 >= 0.9 * cfg.embed_deadline_ms
            metrics.inc("memd_recall_fallbacks_total", reason="embed_timeout" if timed_out else "embed_failed")
        chunked = cfg.recall_vectors == "chunks"
        arm = _vector_arm if chunked else _note_arm
        # Archived notes (memd.forget) only with include_archived; the keyword
        # used only when asked, so the arms keep their ordinary call shape.
        extra = {"include_archived": True} if include_archived else {}
        vec_slugs = (arm(db, qvec, **extra) if scoped is None else arm(db, qvec, scoped, **extra)) \
            if qvec is not None else []
        if qvec is not None:
            mark = _stage("vector", mark)
            if not vec_slugs:
                metrics.inc("memd_recall_fallbacks_total", reason="vector_empty")

        # (C) BM25 arm (always on).
        bm_slugs = _bm25_arm(db, distilled, **extra) if scoped is None \
            else _bm25_arm(db, distilled, scoped, **extra)
        mark = _stage("bm25", mark)

        fresh_slugs = (_fresh_arm(db, distilled, **extra) if scoped is None
                       else _fresh_arm(db, distilled, scoped, **extra)) if current else []
        if current:
            _stage("fresh", mark)
        if trace is not None:
            trace["fresh"] = list(fresh_slugs)

        # Core notes compete like every other note; importance must not restrict
        # their retrievability.
        all_slugs = list(dict.fromkeys([*vec_slugs, *bm_slugs, *fresh_slugs]))
        notes_by_slug = _fetch_notes(db, all_slugs, **extra)
        if host or tags:
            notes_by_slug = {slug: n for slug, n in notes_by_slug.items() if in_scope(n)}

        # Learned per-note weights from the usage log: opt-in, bounded (memd.usage).
        weights = None
        if cfg.usage_boost:
            from memd.usage import weights_for
            weights = weights_for(cfg.db)
        union = [s for s in _fuse(vec_slugs, bm_slugs, notes_by_slug, distilled, fresh_slugs=fresh_slugs,
                                  weights=weights) if s in notes_by_slug][:UNION_K]
        if trace is not None and weights:
            trace["usage_weights"] = {s: weights[s] for s in union if s in weights}

        if trace is not None:
            # Diagnostics for the recall eval: "never retrieved" vs "badly ranked".
            trace.update(vec=list(vec_slugs), bm25=list(bm_slugs), pool=list(union))

        candidates = [
            {"slug": s, "body": notes_by_slug[s].body}
            for s in union if s in notes_by_slug
        ]

        # (D) rerank with deadline. Cap candidates and truncate bodies to stay
        # within the reranker's batch limit and latency budget; the returned
        # slugs are looked up in notes_by_slug (full bodies) so the truncated
        # text never leaks into the result set.
        head = set(_rerank_head(union, bm_slugs, RERANK_MAX_CANDIDATES))
        # Every head note is shown by its best chunk, whichever arm found it.
        best = _best_chunks(db, qvec, sorted(head)) \
            if qvec is not None and chunked and RERANK_CHUNK else {}
        if trace is not None:
            trace["chunks"] = {s: [h.ordinal, h.start, h.end] for s, h in best.items()}
        rerank_candidates = [
            {"slug": c["slug"], "body": _rerank_text(notes_by_slug[c["slug"]], best.get(c["slug"]))}
            for c in candidates if c["slug"] in head
        ]
        top_n = len(rerank_candidates) if RERANK_BLEND > 0 else min(k, len(rerank_candidates))
        began = time.perf_counter()
        reranked = rerank(query, rerank_candidates, top_n=top_n, ms=cfg.rerank_deadline_ms, cfg=cfg) \
            if rerank_candidates else []
        if rerank_candidates:
            _stage("rerank", began)
            if reranked is None:
                metrics.inc("memd_recall_fallbacks_total", reason="rerank_failed")
        else:
            metrics.inc("memd_recall_fallbacks_total", reason="rerank_skipped")
        metrics.inc("memd_recall_order_total", order="rerank" if reranked is not None
                    else "vector" if qvec is not None else "bm25")

        if trace is not None:
            trace["reranked"] = reranked is not None
        if reranked is not None:
            ranked_slugs = [c["slug"] for c in reranked]
            if RERANK_BLEND > 0:
                # The cross-encoder and the fused arms disagree in useful ways;
                # neither order alone wins every query category.
                fused_rank = {s: i for i, s in enumerate(union)}
                rr_rank = {s: i for i, s in enumerate(ranked_slugs)}
                ranked_slugs.sort(key=lambda s: -(1 / (BLEND_K + rr_rank[s] + 1)
                                                  + RERANK_BLEND / (BLEND_K + fused_rank.get(s, len(union)) + 1)))
            # Rerank the affordable head, then keep the remaining fused results
            # available. Asking for >10 must not silently stop at the batch cap.
            for slug in union:
                if len(set(ranked_slugs)) >= k:
                    break
                if slug in notes_by_slug and slug not in ranked_slugs:
                    ranked_slugs.append(slug)
        elif qvec is not None:
            # rerank down, embed ok -> vector-distance order (semantic preserved).
            ranked_slugs = [s for s in vec_slugs if s in notes_by_slug][:k]
            # top up with BM25 if vector arm was thin
            for s in bm_slugs:
                if len(ranked_slugs) >= k:
                    break
                if s in notes_by_slug and s not in ranked_slugs:
                    ranked_slugs.append(s)
        else:
            # embed + rerank down -> raw BM25 order.
            ranked_slugs = [s for s in bm_slugs if s in notes_by_slug][:k]

        # Dedup ranked_slugs by slug (first occurrence wins): a malformed rerank
        # response that repeats a candidate index maps the same slug twice, which
        # would otherwise surface the SAME Note more than once in the tail.
        seen_tail: set[str] = set()
        tail: list[Note] = []
        for s in ranked_slugs:
            if len(tail) >= k:
                break
            if s in seen_tail or s not in notes_by_slug:
                continue
            seen_tail.add(s)
            tail.append(dataclasses.replace(notes_by_slug[s], matched=True))
        # NO re-sort here. The ladder above already defines the ordering contract
        # (rerank order, else vector-distance order, else raw BM25), and a
        # `key=lambda n: n.last_used or 0` sort broke it two ways: `last_used` is
        # `str | None`, so one note carrying a timestamp made the key mix str with
        # int and raise TypeError straight out of recall() — failing every turn —
        # while in the non-raising case it silently discarded all three orderings
        # the module docstring promises. Newest-first belongs in the presentation
        # layer, keyed on a normalised comparable.

        if include_core:
            # Dedup core against the tail: a note that matched the query must
            # render once as a query excerpt, not also as a stub. Mark surviving core
            # notes as index entries (matched=False) explicitly.
            tail_slugs = {n.slug for n in tail}
            core_filtered = [
                dataclasses.replace(n, matched=False)
                for n in core
                if n.slug not in tail_slugs
            ]
            return core_filtered + tail
        return tail
    finally:
        db.close()
