"""Slug + BM25 dedup over a note corpus. In-memory FTS5 — no external service.

SQLite bm25() returns NEGATIVE scores where more-negative == more-relevant. A
strong near-match is one whose bm25 score is at or below STRONG_THRESHOLD.

bm25()'s magnitude scales with the number of matched query terms, so a raw
floor (e.g. STRONG_THRESHOLD) flags any long note as a duplicate of whatever
ranks second, regardless of content. A real-world corpus shows unrelated
long notes scoring around -0.3/terms while true duplicates sit near -3.2/terms.
We therefore also require the per-term normalisation score
(score / n_terms) to pass STRONG_PER_TERM, and keep STRONG_THRESHOLD as a
raw floor that the score must beat in absolute terms too.
"""
from __future__ import annotations

import re
import sqlite3

# Raw bm25 floor: score must be at least this negative in absolute terms.
# Kept for backwards compatibility with tests/modules that import it; the
# real guard is STRONG_PER_TERM below.
STRONG_THRESHOLD = -3.0
# bm25() magnitude scales with the number of matched query terms, so a raw
# threshold flags any long note as a duplicate of whatever ranks second.
# Normalise by term count. Measured on a real-world corpus: true duplicates sit
# near -3.2/term, unrelated notes no lower than -0.29/term.
STRONG_PER_TERM = -2.0
# On a tiny index (e.g. one existing note) bm25's IDF term collapses toward 0,
# so magnitude alone can't flag an obvious near-duplicate. As a corpus-size-safe
# backstop, also treat a returned hit as strong when it shares a high fraction of
# the candidate's distinct tokens. Unrelated notes never reach this (they produce
# no FTS rows at all), so this cannot create spurious pairs on the real corpus.
# The backstop only applies on a SMALL corpus, which is its stated purpose: with
# one indexed note bm25() returns -0.000 (IDF fully collapsed) so overlap is the
# only usable signal. On a real corpus bm25-per-term is reliable AND unrelated
# pairs were measured up to 0.531 overlap -- letting overlap alone fire there is
# what wrongly superseded an unrelated note at 0.506. So above
# SMALL_CORPUS notes, a match must satisfy the bm25 rule.
STRONG_OVERLAP = 0.5
SMALL_CORPUS = 25

_TOKEN = re.compile(r"[A-Za-z0-9_.:/@-]+")


def _tokens(note) -> set:
    return {t.lower() for t in _TOKEN.findall(f"{note.title} {note.body}") if len(t) > 1}


def _overlap(candidate, other) -> float:
    ct = _tokens(candidate)
    if not ct:
        return 0.0
    return len(ct & _tokens(other)) / len(ct)


def _query_text(note) -> str:
    toks = _TOKEN.findall(f"{note.title} {note.body}")
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(toks) if len(t) > 1)


def build_bm25(notes: list) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE fts USING fts5(slug UNINDEXED, title, body, "
        "tokenize=\"unicode61 tokenchars '-_.:/@'\")"
    )
    conn.executemany(
        "INSERT INTO fts(slug, title, body) VALUES (?,?,?)",
        [(n.slug, n.title, n.body) for n in notes],
    )
    conn.commit()
    return conn


def best_match(conn: sqlite3.Connection, notes: list, candidate):
    """Strongest BM25 near-match for `candidate` among indexed notes, or None.

    Self-matches (same slug) are skipped so a note never dedups against itself.
    """
    by_slug = {n.slug: n for n in notes}
    q = _query_text(candidate)
    if not q:
        return None
    # number of OR-joined terms in the issued query (min 1)
    n_terms = max(1, len(q.split(" OR ")))
    rows = conn.execute(
        "SELECT slug, bm25(fts) AS score FROM fts WHERE fts MATCH ? "
        "ORDER BY score LIMIT 5",
        (q,),
    ).fetchall()
    for slug, score in rows:
        if slug == candidate.slug:
            continue
        other = by_slug.get(slug)
        bm25_strong = (
            score <= STRONG_THRESHOLD and (score / n_terms) <= STRONG_PER_TERM
        )
        overlap_strong = (
            len(notes) <= SMALL_CORPUS
            and _overlap(candidate, other) >= STRONG_OVERLAP
        )
        if bm25_strong or overlap_strong:
            return other
    return None


def find_duplicates(notes: list) -> list[tuple]:
    """All (a, b) duplicate pairs: identical slug OR strong BM25 near-match.

    Each pair is recorded once, ordered by path (a.path < b.path).
    """
    pairs: list[tuple] = []
    seen_pairs: set[frozenset] = set()

    # 1) slug collisions
    by_slug: dict[str, object] = {}
    for n in notes:
        if n.slug in by_slug:
            first = by_slug[n.slug]
            lo, hi = sorted((first, n), key=lambda x: x.path)
            key = frozenset((lo.path, hi.path))
            if key not in seen_pairs:
                seen_pairs.add(key)
                pairs.append((lo, hi))
        else:
            by_slug[n.slug] = n

    # 2) strong BM25 near-matches across distinct slugs
    for i, cand in enumerate(notes):
        others = [n for j, n in enumerate(notes) if j != i and n.slug != cand.slug]
        if not others:
            continue
        sub = build_bm25(others)
        m = best_match(sub, others, cand)
        sub.close()
        if m is None:
            continue
        lo, hi = sorted((cand, m), key=lambda x: x.path)
        key = frozenset((lo.path, hi.path))
        if key not in seen_pairs:
            seen_pairs.add(key)
            pairs.append((lo, hi))

    return pairs
