"""Offline recall-quality evaluation harness for `memd.recall`.

Runs `recall()` over a hand-labelled golden set against a scratch copy of the
note corpus. Every embed/rerank call is routed through `install_replay`, so a
run touches the model backends only on cache misses and `--offline` replays a
previous run exactly. `--baseline` gates a ranking change against a stored run.

    python -m memd.recall_eval --golden eval/golden-mine.jsonl --clone CORPUS \
        --db /tmp/eval.db --cache eval-cache.sqlite --baseline eval/baseline-mine.json

The public synthetic set (eval/public) runs without any model service, as CI
does: `--corpus` commits a directory of notes into a scratch clone, `--embedder
hash` stands in a deterministic hashed bag-of-words embedder and `--rerank none`
keeps the fused order. That measures the pipeline (arms, fusion, chunk collapse,
current-state gate, budgets), not semantic quality:

    python -m memd.recall_eval --golden eval/public/golden.jsonl \
        --corpus eval/public/corpus --embedder hash --rerank none \
        --baseline eval/public/baseline.json
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

import memd.index as index_mod
import memd.recall as recall_mod

from memd.config import (
    DEFAULT_EMBED_MODEL, DEFAULT_EMBED_URL, DEFAULT_RERANK_MODEL, DEFAULT_RERANK_URL, Config,
)
from memd.embed import DIM, _TRUNC_CHARS
from memd.index import build_index, head_in_index, open_db, pending_vectors
from memd.query import STOPWORDS
from memd.recall import recall
from memd.store import git_head_sha


class ReplayMiss(RuntimeError):
    """Raised in offline mode when a needed embed/rerank result is not cached."""


_MISSING = object()
METRICS = ("recall@5", "recall@10", "mrr@10", "ndcg@10", "pool")


class ReplayCache:
    """Tiny sqlite key/value store for embed and rerank results.

    The sentinel `_MISSING` distinguishes an absent key from a stored JSON null,
    which matters because `rerank()` legitimately can return `None`.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Several eval runs may share one cache; wait for a writer instead of failing.
        self._db = sqlite3.connect(str(path), timeout=60)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._db.commit()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> object:
        row = self._db.execute(
            "SELECT value FROM kv WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            self.misses += 1
            return _MISSING
        self.hits += 1
        return json.loads(row[0])

    def put(self, key: str, value: object) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)",
            (key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()


def _key(*parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def install_replay(cache: ReplayCache, *, offline: bool) -> Callable[[], None]:
    """Swap the three module attributes that `recall` uses for cached versions.

    Returns an `uninstall()` callable that restores the originals. Wrappers are
    installed on `memd.index.embed` (the build path), and on the
    `embed_with_deadline` / `rerank` names already bound inside
    `memd.recall`'s module namespace (imported there as plain names).
    """
    orig_index_embed = getattr(index_mod, "embed", None)
    orig_recall_embed_with_deadline = getattr(recall_mod, "embed_with_deadline", None)
    orig_recall_rerank = getattr(recall_mod, "rerank", None)

    def cached_embed(texts: list[str], cfg: Config) -> list[list[float]]:
        keys = [_key("embed", cfg.embed_model, t[:5000]) for t in texts]
        found: dict[str, list[float]] = {}
        missing: dict[str, str] = {}          # key -> text, first occurrence wins
        for text, key in zip(texts, keys, strict=True):
            if key in found or key in missing:
                continue
            cached = cache.get(key)
            if cached is _MISSING:
                missing[key] = text
            else:
                found[key] = cached
        if missing:
            if offline:
                raise ReplayMiss(f"{len(missing)} embedding(s) not cached")
            vectors = orig_index_embed(list(missing.values()), cfg)
            for key, vec in zip(missing, vectors, strict=True):
                cache.put(key, vec)
                found[key] = vec
        return [found[key] for key in keys]

    def cached_embed_with_deadline(
        text: str, *, cfg: Config, ms: int = 800
    ) -> list[float] | None:
        # No deadline enforcement on the eval path: a slow-but-successful embed
        # must not silently drop the vector arm and skew the measurement.
        return cached_embed([text], cfg)[0]

    def cached_rerank(
        query: str,
        candidates: list[dict],
        top_n: int = 8,
        ms: int = 400,
        *,
        cfg: Config,
    ) -> list[dict] | None:
        if not candidates:
            return []
        key = _key(
            "rerank",
            cfg.rerank_model,
            query,
            [c["body"] for c in candidates],
            top_n,
        )
        cached = cache.get(key)
        if cached is not _MISSING:
            idxs = cached
            if isinstance(idxs, list):
                return [candidates[i] for i in idxs if 0 <= i < len(candidates)]
        if offline:
            raise ReplayMiss("rerank result not cached")
        result = orig_recall_rerank(query, candidates, top_n=top_n, ms=10000, cfg=cfg)
        if result is None:
            raise RuntimeError("reranker failed during eval (not cached)")
        # Map each returned dict back to its index in `candidates` by identity.
        id_to_index = {id(c): i for i, c in enumerate(candidates)}
        idx_list: list[int] = []
        for c in result:
            idx = id_to_index.get(id(c))
            if idx is not None:
                idx_list.append(idx)
        cache.put(key, idx_list)
        return [candidates[i] for i in idx_list if 0 <= i < len(candidates)]

    def uninstall() -> None:
        if orig_index_embed is not None:
            index_mod.embed = orig_index_embed
        if orig_recall_embed_with_deadline is not None:
            recall_mod.embed_with_deadline = orig_recall_embed_with_deadline
        if orig_recall_rerank is not None:
            recall_mod.rerank = orig_recall_rerank

    index_mod.embed = cached_embed
    recall_mod.embed_with_deadline = cached_embed_with_deadline
    recall_mod.rerank = cached_rerank
    return uninstall


# ---------------------------------------------------------------------------
# Local stand-ins for the model backends (CI has no embedder or reranker).
# ---------------------------------------------------------------------------

HASH_MODEL = "memd-eval-hash-v1"    # replay-cache key namespace for the hash embedder
_WORD = re.compile(r"[0-9a-z]+")


@lru_cache(maxsize=1 << 16)
def _bucket(feature: str, dim: int) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % dim, (1.0 if value >> 63 else -1.0)


def hash_embed(text: str, dim: int = DIM) -> list[float]:
    """A deterministic hashed bag-of-words vector, L2-normalised; a CI stand-in.

    Features: each non-stopword (weight 1), each adjacent word pair (0.5) and each
    character trigram of a word padded with '#' (0.25, so "backup" and "backups"
    share most of their mass). Signed feature hashing into `dim` buckets. Like
    the real embedder, only the first _TRUNC_CHARS characters are seen. It knows
    no synonyms: a paraphrase that shares no words scores near zero, so this
    measures the retrieval pipeline, never semantic quality.
    """
    words = [w for w in _WORD.findall(text[:_TRUNC_CHARS].casefold()) if w not in STOPWORDS]
    vec = [0.0] * dim
    features: list[tuple[str, float]] = [(f"w:{w}", 1.0) for w in words]
    features += [(f"b:{a} {b}", 0.5) for a, b in zip(words, words[1:])]
    for w in words:
        padded = f"#{w}#"
        features += [(f"c:{padded[i:i + 3]}", 0.25) for i in range(len(padded) - 2)]
    for feature, weight in features:
        idx, sign = _bucket(feature, dim)
        vec[idx] += sign * weight
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        # sqlite-vec has no cosine for a zero vector; a fixed unit vector keeps
        # empty texts rankable (last) instead of undefined.
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]


def install_local(*, embedder: str, reranker: str) -> Callable[[], None]:
    """Replace model backends with local stand-ins; returns `uninstall()`.

    embedder "hash": `hash_embed` for the index build and the query vector.
    reranker "none": an identity reranker that keeps the fused order, so the
    fused ranking (and the rerank-head seat logic) is what gets measured; unlike
    a failing reranker it does not drop recall onto its degradation ladder.
    "model" leaves that backend alone.
    """
    saved: list[tuple[object, str, object]] = []

    def patch(module: object, name: str, value: object) -> None:
        saved.append((module, name, getattr(module, name)))
        setattr(module, name, value)

    if embedder == "hash":
        def embed(texts: list[str], cfg: Config) -> list[list[float]]:
            return [hash_embed(t, cfg.embed_dim) for t in texts]

        def embed_with_deadline(text: str, *, cfg: Config, ms: int = 800) -> list[float]:
            return hash_embed(text, cfg.embed_dim)

        patch(index_mod, "embed", embed)
        patch(recall_mod, "embed_with_deadline", embed_with_deadline)
    if reranker == "none":
        def rerank(query: str, candidates: list[dict], top_n: int = 8, ms: int = 400,
                   *, cfg: Config) -> list[dict]:
            return list(candidates)[:top_n]

        patch(recall_mod, "rerank", rerank)

    def uninstall() -> None:
        for module, name, value in reversed(saved):
            setattr(module, name, value)

    return uninstall


_CORPUS_GIT_ENV = {
    "GIT_AUTHOR_NAME": "memd-eval", "GIT_AUTHOR_EMAIL": "eval@example.com",
    "GIT_COMMITTER_NAME": "memd-eval", "GIT_COMMITTER_EMAIL": "eval@example.com",
    "GIT_AUTHOR_DATE": "2026-09-28T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-09-28T00:00:00+00:00",
}


def materialize_corpus(src: Path, clone: Path) -> str:
    """Commit src/*.md into a new git repo at clone; returns its HEAD sha.

    Fixed author, committer and dates make the sha a function of the notes
    alone, so a baseline's corpus_head check works across machines.
    """
    notes = sorted(src.glob("*.md"))
    if not notes:
        raise ValueError(f"no *.md notes in {src}")
    clone.mkdir(parents=True)
    for note in notes:
        shutil.copyfile(note, clone / note.name)
    env = {**os.environ, **_CORPUS_GIT_ENV}
    for args in (["-c", "init.defaultBranch=main", "init", "-q"], ["add", "-A"],
                 ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "eval corpus"]):
        subprocess.run(["git", "-C", str(clone), *args], check=True, env=env,
                       capture_output=True)
    return git_head_sha(clone)


def load_golden(path: Path, *, reviewed_only: bool = False) -> list[dict]:
    """Read a JSONL golden set; raise `ValueError` naming the offending line.

    Rows exported by `mem-usage export-golden` carry `source: "usage"` and
    `needs_review: true` (plus informational `reads`/`last_seen`); they load like
    any other row. `reviewed_only` drops rows still marked `needs_review`.
    """
    rows: list[dict] = []
    seen_ids: set[str] = set()
    with path.open(encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {lineno}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"line {lineno}: row is not a JSON object")
            rid = row.get("id")
            if not isinstance(rid, str) or not rid:
                raise ValueError(f"line {lineno}: missing or empty 'id'")
            if rid in seen_ids:
                raise ValueError(f"line {lineno}: duplicate id {rid!r}")
            seen_ids.add(rid)
            query = row.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError(f"line {lineno}: missing or empty 'query'")
            category = row.get("category")
            if not isinstance(category, str) or not category:
                raise ValueError(f"line {lineno}: missing or empty 'category'")
            gold = row.get("gold")
            if not isinstance(gold, list) or not gold:
                raise ValueError(f"line {lineno}: 'gold' must be a non-empty list")
            for gi, item in enumerate(gold):
                if not isinstance(item, dict):
                    raise ValueError(f"line {lineno}: gold[{gi}] is not an object")
                slug = item.get("slug")
                grade = item.get("grade")
                if not isinstance(slug, str) or not slug:
                    raise ValueError(f"line {lineno}: gold[{gi}] missing 'slug'")
                if grade not in (1, 2):
                    raise ValueError(
                        f"line {lineno}: gold[{gi}] 'grade' must be 1 or 2"
                    )
            if "source" in row and not isinstance(row["source"], str):
                raise ValueError(f"line {lineno}: 'source' must be a string")
            if "needs_review" in row and not isinstance(row["needs_review"], bool):
                raise ValueError(f"line {lineno}: 'needs_review' must be true or false")
            if reviewed_only and row.get("needs_review"):
                continue
            row.setdefault("split", "dev")
            rows.append(row)
    return rows


def _gold_map(row: dict) -> dict[str, int]:
    return {g["slug"]: int(g["grade"]) for g in row["gold"]}


def recall_at(ranked: list[str], gold: dict[str, int], k: int) -> float:
    if not gold:
        return 0.0
    top = set(ranked[:k])
    return len(top & gold.keys()) / len(gold)


def mrr_at(ranked: list[str], gold: dict[str, int], k: int = 10) -> float:
    for i, slug in enumerate(ranked[:k]):
        if slug in gold:
            return 1.0 / (i + 1)
    return 0.0


def _dcg(grades_in_order: list[int], k: int) -> float:
    total = 0.0
    for i, g in enumerate(grades_in_order[:k]):
        if g <= 0:
            continue
        total += (2 ** g - 1) / math.log2(i + 2)
    return total


def ndcg_at(ranked: list[str], gold: dict[str, int], k: int = 10) -> float:
    if not gold:
        return 0.0
    actual = [gold.get(s, 0) for s in ranked[:k]]
    ideal = sorted(gold.values(), reverse=True)[:k]
    dcg = _dcg(actual, k)
    idcg = _dcg(ideal, k)
    if idcg <= 0:
        return 0.0
    return dcg / idcg


def pool_recall(pool: list[str], gold: dict[str, int]) -> float:
    if not gold:
        return 0.0
    return len(set(pool) & gold.keys()) / len(gold)


def _mean(xs: list[float]) -> float:
    if not xs:
        return 0.0
    return sum(xs) / len(xs)


def prepare_index(cfg: Config) -> str:
    """Open the db, reuse the index if current, else rebuild it. Returns head sha."""
    db = open_db(cfg.db)
    try:
        corpus_head = git_head_sha(cfg.clone)
        current = head_in_index(db)
        pending = pending_vectors(db)
        if corpus_head and current == corpus_head and pending == 0:
            print("index reused", file=sys.stderr)
        else:
            build_index(db, cfg)
            print("index built", file=sys.stderr)
        return corpus_head
    finally:
        db.close()


def evaluate(golden: list[dict], cfg: Config, *, k: int = 10) -> dict:
    """Run every golden query through `recall()` and collect per-query metrics."""
    per_query: dict[str, dict] = {}
    for row in golden:
        trace: dict = {}
        notes = recall(
            row["query"], "amber", k=k, cfg=cfg, include_core=False, trace=trace
        )
        ranked = [n.slug for n in notes]
        pool = list(trace.get("pool", []))
        gold = _gold_map(row)
        rec = {
            "id": row["id"],
            "category": row["category"],
            "split": row.get("split", "dev"),
            "ranked": ranked,
            "pool_size": len(pool),
            "reranked": trace.get("reranked"),
            "recall@5": round(recall_at(ranked, gold, 5), 6),
            "recall@10": round(recall_at(ranked, gold, 10), 6),
            "mrr@10": round(mrr_at(ranked, gold, 10), 6),
            "ndcg@10": round(ndcg_at(ranked, gold, 10), 6),
            "pool": round(pool_recall(pool, gold), 6),
        }
        per_query[row["id"]] = rec
    return {"k": k, "per_query": per_query}


def summarize(per_query: dict, *, split: str = "all") -> dict:
    """Aggregate metrics across all (or one split's) queries."""
    rows = [
        rec
        for rec in per_query.values()
        if split == "all" or rec.get("split") == split
    ]
    n = len(rows)
    if n == 0:
        return {"n": 0, "overall": {}, "by_category": {}}
    overall = {
        m: round(_mean([float(rec[m]) for rec in rows]), 4) for m in METRICS
    }
    by_category: dict[str, dict] = {}
    cats: dict[str, list[dict]] = {}
    for rec in rows:
        cats.setdefault(rec["category"], []).append(rec)
    for cat in sorted(cats):
        cat_rows = cats[cat]
        entry: dict = {"n": len(cat_rows)}
        for m in METRICS:
            entry[m] = round(_mean([float(r[m]) for r in cat_rows]), 4)
        by_category[cat] = entry
    return {"n": n, "overall": overall, "by_category": by_category}


def bootstrap_ci(
    deltas: list[float], *, iters: int = 2000, seed: int = 20260927
) -> tuple[float, float]:
    """Percentile 95% CI of the mean via resampling with replacement."""
    n = len(deltas)
    if n == 0:
        return (0.0, 0.0)
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(iters):
        sample = [deltas[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(0.025 * iters)]
    hi = means[int(0.975 * iters) - 1] if iters > 1 else means[0]
    return (lo, hi)


def compare(current: dict, baseline: dict, *, split: str = "all") -> dict:
    """Paired per-query comparison of two `evaluate()` outputs."""
    cur_map = current["per_query"]
    base_map = baseline["per_query"]
    common_ids = [
        qid
        for qid in cur_map
        if qid in base_map
        and (
            split == "all"
            or cur_map[qid].get("split") == split
        )
        and (
            split == "all"
            or base_map[qid].get("split") == split
        )
    ]
    n = len(common_ids)
    if n == 0:
        return {
            "n": 0,
            "deltas": {
                m: {"mean": 0.0, "ci": [0.0, 0.0]} for m in METRICS
            },
            "category_ndcg": {},
            "verdict": "NEUTRAL",
            "losers": [],
            "winners": [],
        }

    paired: dict[str, list[float]] = {m: [] for m in METRICS}
    for qid in common_ids:
        c = cur_map[qid]
        b = base_map[qid]
        for m in METRICS:
            paired[m].append(float(c[m]) - float(b[m]))

    deltas: dict[str, dict] = {}
    for m in METRICS:
        mean = _mean(paired[m])
        lo, hi = bootstrap_ci(paired[m])
        deltas[m] = {"mean": round(mean, 4), "ci": [round(lo, 4), round(hi, 4)]}

    cat_deltas: dict[str, float] = {}
    cats: dict[str, list[float]] = {}
    for qid in common_ids:
        c = cur_map[qid]
        b = base_map[qid]
        cats.setdefault(c["category"], []).append(
            float(c["ndcg@10"]) - float(b["ndcg@10"])
        )
    for cat, ds in cats.items():
        cat_deltas[cat] = round(_mean(ds), 4)

    verdict = "NEUTRAL"
    big_fail = (
        deltas["recall@10"]["mean"] <= -0.01
        or deltas["mrr@10"]["mean"] <= -0.01
        or deltas["ndcg@10"]["mean"] <= -0.01
        or any(
            len(cats[cat]) >= 3 and d <= -0.03
            for cat, d in cat_deltas.items()
        )
    )
    if big_fail:
        verdict = "FAIL"
    elif deltas["ndcg@10"]["mean"] >= 0.02 and deltas["ndcg@10"]["ci"][0] > 0:
        verdict = "IMPROVED"

    losers: list[dict] = []
    winners: list[dict] = []
    for qid in common_ids:
        c = cur_map[qid]
        b = base_map[qid]
        drop = float(b["ndcg@10"]) - float(c["ndcg@10"])
        if drop > 0.2:
            losers.append(
                {
                    "id": qid,
                    "before": round(float(b["ndcg@10"]), 4),
                    "after": round(float(c["ndcg@10"]), 4),
                    "before_top3": b["ranked"][:3],
                    "after_top3": c["ranked"][:3],
                }
            )
        elif drop < -0.2:
            winners.append(
                {
                    "id": qid,
                    "before": round(float(b["ndcg@10"]), 4),
                    "after": round(float(c["ndcg@10"]), 4),
                    "before_top3": b["ranked"][:3],
                    "after_top3": c["ranked"][:3],
                }
            )
    losers.sort(key=lambda r: r["before"] - r["after"], reverse=True)
    winners.sort(key=lambda r: r["after"] - r["before"], reverse=True)

    return {
        "n": n,
        "deltas": deltas,
        "category_ndcg": cat_deltas,
        "verdict": verdict,
        "losers": losers[:15],
        "winners": winners[:15],
    }


def format_summary(summary: dict, title: str) -> str:
    """Fixed-width table of per-category + overall metrics (3 dp)."""
    header = (
        f"{'category':<22}"
        f"{'n':>5}  "
        f"{'recall@5':>9} {'recall@10':>10} {'mrr@10':>8} "
        f"{'ndcg@10':>9} {'pool':>8}"
    )
    lines = [title, header, "-" * len(header)]
    by_cat = summary.get("by_category", {})
    for cat in sorted(by_cat):
        e = by_cat[cat]
        lines.append(
            f"{cat:<22}{e['n']:>5}  "
            f"{e['recall@5']:>9.3f} {e['recall@10']:>10.3f} "
            f"{e['mrr@10']:>8.3f} {e['ndcg@10']:>9.3f} {e['pool']:>8.3f}"
        )
    ov = summary.get("overall", {})
    if ov:
        lines.append(
            f"{'ALL':<22}{summary.get('n', 0):>5}  "
            f"{ov['recall@5']:>9.3f} {ov['recall@10']:>10.3f} "
            f"{ov['mrr@10']:>8.3f} {ov['ndcg@10']:>9.3f} {ov['pool']:>8.3f}"
        )
    return "\n".join(lines)


def _fmt_top3(slugs: list[str]) -> str:
    parts = [s if len(s) <= 60 else s[:57] + "..." for s in slugs[:3]]
    return ", ".join(parts)


def format_compare(cmp: dict) -> str:
    lines = [f"verdict: {cmp['verdict']}  (n={cmp['n']})"]
    for m in METRICS:
        d = cmp["deltas"][m]
        lines.append(f"  {m:<10} {d['mean']:>+8.3f}  [{d['ci'][0]:+.3f}, {d['ci'][1]:+.3f}]")
    lines.append("  category ndcg@10 deltas:")
    for cat in sorted(cmp.get("category_ndcg", {})):
        d = cmp["category_ndcg"][cat]
        lines.append(f"    {cat:<22} {d:>+8.3f}")
    if cmp.get("losers"):
        lines.append("  losers (ndcg drop > 0.2):")
        for r in cmp["losers"]:
            lines.append(
                f"    {r['id']:<12} {r['before']:.3f} -> {r['after']:.3f}"
                f"  before: {_fmt_top3(r['before_top3'])}"
                f"  after: {_fmt_top3(r['after_top3'])}"
            )
    if cmp.get("winners"):
        lines.append("  winners (ndcg gain > 0.2):")
        for r in cmp["winners"]:
            lines.append(
                f"    {r['id']:<12} {r['before']:.3f} -> {r['after']:.3f}"
                f"  before: {_fmt_top3(r['before_top3'])}"
                f"  after: {_fmt_top3(r['after_top3'])}"
            )
    return "\n".join(lines)


def format_markdown(summary: dict, title: str, cmp: dict | None = None) -> str:
    """The summary (and comparison) as GitHub-flavoured Markdown, for a CI step summary."""
    lines = [f"### {title}", "",
             "| category | n | recall@5 | recall@10 | mrr@10 | ndcg@10 | pool |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    rows = [(cat, e) for cat, e in sorted(summary.get("by_category", {}).items())]
    if summary.get("overall"):
        rows.append(("**all**", {"n": summary["n"], **summary["overall"]}))
    for cat, e in rows:
        lines.append(f"| {cat} | {e['n']} | " + " | ".join(f"{e[m]:.3f}" for m in METRICS) + " |")
    if cmp is not None:
        lines += ["", f"Against the baseline: **{cmp['verdict']}** (n={cmp['n']})", "",
                  "| metric | delta | 95% CI |", "|---|---:|---|"]
        for m in METRICS:
            d = cmp["deltas"][m]
            lines.append(f"| {m} | {d['mean']:+.3f} | [{d['ci'][0]:+.3f}, {d['ci'][1]:+.3f}] |")
        changed = {c: d for c, d in cmp.get("category_ndcg", {}).items() if d}
        if changed:
            lines += ["", "ndcg@10 change by category: " + ", ".join(
                f"{c} {d:+.3f}" for c, d in sorted(changed.items()))]
    return "\n".join(lines) + "\n"


def _print_query_detail(qid: str, golden: list[dict], cfg: Config) -> None:
    row = next((r for r in golden if r["id"] == qid), None)
    if row is None:
        print(f"[show] no such query id: {qid}", file=sys.stderr)
        return
    print(f"\n== {qid} ==")
    print(f"query: {row['query']}")
    gold = _gold_map(row)
    print(f"gold:  {json.dumps(row['gold'], ensure_ascii=False)}")
    trace: dict = {}
    notes = recall(row["query"], "amber", k=10, cfg=cfg, include_core=False, trace=trace)
    ranked = [n.slug for n in notes]
    print("ranked:")
    for i, slug in enumerate(ranked, 1):
        grade = gold.get(slug)
        mark = f"\u2713 {grade}" if grade else "\u2715"
        print(f"  {i:>2}. {mark}  {slug}")
    for field in ("vec", "bm25", "pool"):
        items = trace.get(field, [])
        print(f"{field}: {json.dumps(list(items[:20]))}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--clone", type=Path, default=None,
                        help="git clone of the corpus (or use --corpus)")
    parser.add_argument(
        "--corpus", type=Path, default=None,
        help="directory of *.md notes, committed into a scratch clone (eval/public/corpus)",
    )
    parser.add_argument("--db", type=Path, default=None,
                        help="index database (default: a scratch file with --corpus)")
    parser.add_argument("--cache", type=Path, default=None,
                        help="replay cache; required when a model backend is used")
    parser.add_argument(
        "--embedder", choices=["model", "hash"], default="model",
        help="hash: deterministic local stand-in (CI); measures the pipeline, not semantics",
    )
    parser.add_argument(
        "--rerank", choices=["model", "none"], default="model",
        help="none: keep the fused order (identity reranker)",
    )
    parser.add_argument(
        "--markdown", type=Path, default=None,
        help="append the results as Markdown (e.g. $GITHUB_STEP_SUMMARY)",
    )
    parser.add_argument("--embed-url", default=DEFAULT_EMBED_URL)
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    parser.add_argument("--rerank-url", default=DEFAULT_RERANK_URL)
    parser.add_argument("--rerank-model", default=DEFAULT_RERANK_MODEL)
    parser.add_argument(
        "--recall-vectors", choices=["chunks", "notes"], default="chunks",
        help="vector arm to evaluate (MEMD_RECALL_VECTORS); both use the same index",
    )
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument(
        "--split", choices=["all", "dev", "test", "fresh"], default="all"
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--reviewed-only", action="store_true",
        help="skip rows still marked needs_review (mem-usage export-golden candidates)",
    )
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--write-baseline", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--show",
        action="append",
        default=[],
        dest="show",
        metavar="ID",
    )
    args = parser.parse_args(argv)
    if (args.clone is None) == (args.corpus is None):
        parser.error("give exactly one of --clone and --corpus")
    if args.clone is not None and args.db is None:
        parser.error("--clone needs --db")
    uses_model = args.embedder == "model" or args.rerank == "model"
    if uses_model and args.cache is None:
        parser.error("--cache is required unless --embedder hash --rerank none")

    with tempfile.TemporaryDirectory(prefix="memd-eval-") as scratch:
        if args.corpus is not None:
            try:
                materialize_corpus(args.corpus.resolve(), Path(scratch) / "clone")
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                print(f"error: could not build the corpus clone: {exc}", file=sys.stderr)
                return 2
            args.clone = Path(scratch) / "clone"
            if args.db is None:
                args.db = Path(scratch) / "eval.db"
        return _run(args, uses_model)


def _run(args: argparse.Namespace, uses_model: bool) -> int:
    clone = args.clone.resolve()
    db = args.db.resolve()
    golden_path = args.golden.resolve()

    os.environ["MEMD_AMBER_CLONE"] = str(clone)
    os.environ["MEMD_AMBER_DB"] = str(db)

    cfg = Config.from_env(env={}, env_file=None)
    cfg = dataclasses.replace(
        cfg,
        clone=clone,
        db=db,
        profile="amber",
        embed_url=args.embed_url,
        embed_model=args.embed_model,
        rerank_url=args.rerank_url,
        rerank_model=args.rerank_model,
        recall_vectors=args.recall_vectors,
    )
    if args.embedder == "hash":
        cfg = dataclasses.replace(cfg, embed_model=HASH_MODEL)
    setup = {"embedder": args.embedder, "rerank": args.rerank,
             "recall_vectors": args.recall_vectors}

    uninstall_local = install_local(embedder=args.embedder, reranker=args.rerank)
    cache = ReplayCache(args.cache.resolve()) if uses_model else None
    uninstall_replay = install_replay(cache, offline=args.offline) if cache else (lambda: None)

    def uninstall() -> None:
        uninstall_replay()
        uninstall_local()

    try:
        try:
            golden = load_golden(golden_path, reviewed_only=args.reviewed_only)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        golden_sha = hashlib.sha256(golden_path.read_bytes()).hexdigest()
        try:
            corpus_head = prepare_index(cfg)
            results = evaluate(golden, cfg, k=args.k)
        except ReplayMiss as exc:
            print(f"replay miss: {exc}", file=sys.stderr)
            return 2
        except (ValueError,) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        summary = summarize(results["per_query"], split=args.split)
        print(format_summary(summary, f"RECALL EVAL ({args.split})"))
        if args.split == "all":
            for sub in ("dev", "test", "fresh"):
                sub_sum = summarize(results["per_query"], split=sub)
                if sub_sum["n"] > 0:
                    print()
                    print(format_summary(sub_sum, f"RECALL EVAL ({sub})"))

        cmp_result: dict | None = None
        if args.baseline is not None:
            try:
                with args.baseline.open(encoding="utf-8") as f:
                    baseline = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"error: could not read baseline: {exc}", file=sys.stderr)
                return 2
            if baseline.get("corpus_head") != corpus_head:
                print(
                    f"warning: baseline corpus_head {baseline.get('corpus_head')!r} "
                    f"!= current {corpus_head!r}",
                    file=sys.stderr,
                )
            if baseline.get("golden_sha256") != golden_sha:
                print(
                    "warning: baseline golden_sha256 differs from current golden file",
                    file=sys.stderr,
                )
            if baseline.get("setup", setup) != setup:
                print(
                    f"warning: baseline setup {baseline.get('setup')} != current {setup}",
                    file=sys.stderr,
                )
            cmp_result = compare(results, baseline, split=args.split)
            print()
            print(format_compare(cmp_result))

        if args.markdown is not None:
            title = f"Recall eval: {golden_path.parent.name}/{golden_path.name} ({args.split})"
            with args.markdown.open("a", encoding="utf-8") as f:
                f.write(format_markdown(summary, title, cmp_result))

        if args.show:
            for qid in args.show:
                _print_query_detail(qid, golden, cfg)

        if args.write_baseline is not None:
            out = dict(results)
            out["corpus_head"] = corpus_head
            out["golden_sha256"] = golden_sha
            out["setup"] = setup
            args.write_baseline.parent.mkdir(parents=True, exist_ok=True)
            with args.write_baseline.open("w", encoding="utf-8") as f:
                json.dump(out, f, indent=2, ensure_ascii=False)
                f.write("\n")

        if args.json is not None:
            payload = {
                "results": results,
                "summary": summary,
                "corpus_head": corpus_head,
            }
            if cmp_result is not None:
                payload["compare"] = cmp_result
            args.json.parent.mkdir(parents=True, exist_ok=True)
            with args.json.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.write("\n")

        if cache is not None:
            print(
                f"cache: hits={cache.hits} misses={cache.misses}",
                file=sys.stderr,
            )
    finally:
        uninstall()
        if cache is not None:
            cache.close()

    if cmp_result is not None and cmp_result.get("verdict") == "FAIL":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
