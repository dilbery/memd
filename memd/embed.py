"""Embedder backed by an OpenAI-compatible embeddings endpoint (for example
Lemonade Server or llama.cpp), 768-dim by default.

The endpoint honours the configured model id; `Config.embed_url`/`embed_model`
carry it.

NOTE: CANARY_STRING is a fingerprint INPUT, not documentation. The fingerprint
file records the text it was made from, so editing the string re-fingerprints
once instead of reporting model drift.

Contract:
  embed(texts) -> list[list[float]]   (dim MUST be 768)
  startup_canary() -> raises CanaryError on dim!=768 OR cosine-fingerprint drift
  embed_with_deadline(text, ms=800) -> list[float] | None (None on timeout/5xx/refused)

Config is threaded as a keyword-only param (`*, cfg`) so the documented
positional call shape `embed_with_deadline(text, ms=800)` is preserved.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import httpx

from memd.config import Config, model_headers

DIM = 768
_BULK_CHUNK = 16        # embeds per request — small enough to slot between other decodes
_BULK_TIMEOUT = 60.0    # generous per-chunk timeout (background reindex, not the hot path)
_BULK_RETRIES = 4       # retry a timed-out chunk: the model server's GPU slot pool is shared
# llama-server refuses embedding inputs past the model's n_ctx_train (2048 for
# many 768-dim embedding models). 5000 chars stays ≤2048 tokens
# even for dense code-heavy notes (worst observed ~2.4 chars/token). Only the
# vector sees the head of a long note; FTS indexes the full body either way.
_TRUNC_CHARS = 5000
# Floor for the halving retry path: stop retrying once the clamp drops below
# this many chars — a note this short that still overflows is pathological, not
# just token-dense, so further halving would only hide data.
_MIN_CLAMP_CHARS = 256
CANARY_STRING = "memd canary: embed 768 fingerprint v2"
COSINE_TOL = 0.999  # same model embeds the canary string near-identically


class CanaryError(RuntimeError):
    """Embedding dim or model-identity drift detected at startup."""


class EmbedBackendError(RuntimeError):
    """Backend answered HTTP 200 but with an error body instead of vectors
    (Lemonade wraps llama-server errors this way)."""


def validate_vectors(vectors, expected: int, dim: int = DIM) -> None:
    """Reject partial, malformed, and non-finite batches before cache writes.

    `dim` is the configured vector width; it defaults to the module constant so
    existing callers and tests keep the 768 contract."""
    if not isinstance(vectors, list) or len(vectors) != expected:
        raise EmbedBackendError(f"expected {expected} embeddings, got "
                                f"{len(vectors) if isinstance(vectors, list) else 'invalid data'}")
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dim:
            raise CanaryError(f"embed dim {len(vector) if isinstance(vector, list) else 'invalid'} != {dim}")
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) for v in vector):
            raise EmbedBackendError("embedding contains a non-finite or non-numeric value")


def _post_embeddings(
    texts: list[str], cfg: Config, timeout: float, clamp: int = _TRUNC_CHARS
) -> list[list[float]]:
    from memd.metrics import backend_call
    with backend_call("embed"):
        return _post_embeddings_once(texts, cfg, timeout, clamp)


def _post_embeddings_once(
    texts: list[str], cfg: Config, timeout: float, clamp: int
) -> list[list[float]]:
    url = f"{cfg.embed_url}/v1/embeddings"
    resp = httpx.post(
        url,
        json={"model": cfg.embed_model, "input": [t[:clamp] for t in texts]},
        headers=model_headers(cfg, url),
        timeout=timeout,
    )
    # Raw llama.cpp returns a genuine HTTP 4xx for over-long embedding inputs,
    # unlike Lemonade which returns 200 with an error body (handled below).
    # Normalize a 4xx into EmbedBackendError so the caller's halve-and-retry
    # path treats it as an input-too-long signal. 5xx (backend down) is left
    # to surface unchanged — that's not an overflow condition.
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        if 400 <= e.response.status_code <= 499:
            body_snippet = str(e.response.text)[:200]
            raise EmbedBackendError(
                f"embeddings backend 4xx {e.response.status_code}: {body_snippet}"
            ) from e
        raise
    body = resp.json()
    if not isinstance(body, dict) or "data" not in body:
        raise EmbedBackendError(f"embeddings backend error: {str(body)[:200]}")
    rows = body["data"]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise EmbedBackendError("embeddings data must be a list of objects")
    # Some older local endpoints omit every index and return input order. If
    # indices are provided, require a complete permutation and honor it.
    if any("index" in row for row in rows):
        indices = [row.get("index") for row in rows]
        if any(type(i) is not int for i in indices) or sorted(indices) != list(range(len(texts))):
            raise EmbedBackendError("embedding indices must identify each input exactly once")
        rows = sorted(rows, key=lambda row: row["index"])
    vectors = [row.get("embedding") for row in rows]
    validate_vectors(vectors, len(texts), dim=cfg.embed_dim)
    return vectors


def embed(texts: list[str], cfg: Config) -> list[list[float]]:
    """Bulk embed (reindex path). Embeds in small chunks and retries a chunk
    that times out, with backoff — so a shared embedding backend that is busy
    with other clients SLOWS the reindex instead of failing it. The hot-path query embed uses
    embed_with_deadline (800ms, no retry); only this background path tolerates
    contention.
    """
    out: list[list[float]] = []
    for i in range(0, len(texts), _BULK_CHUNK):
        chunk = texts[i : i + _BULK_CHUNK]
        vectors: list[list[float]] | None = None
        clamp = _TRUNC_CHARS
        for attempt in range(_BULK_RETRIES):
            try:
                vectors = _post_embeddings(chunk, cfg, timeout=_BULK_TIMEOUT, clamp=clamp)
                break
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt == _BULK_RETRIES - 1:
                    raise
                time.sleep(1.0 * (attempt + 1))  # back off, let the shared GPU slots drain
            except EmbedBackendError as e:
                # A pathologically token-dense note can still overflow the
                # model's context window at the default clamp — halve and retry.
                # Lemonade signals overflow with an HTTP 200 plus an error body
                # containing "context size"; raw llama.cpp signals it with an
                # HTTP 400 whose body lacks that phrase. Match either signal.
                msg = str(e).lower()
                is_overflow = any(
                    token in msg for token in ("context size", "400", "too large", "exceed", "n_ctx")
                )
                if not is_overflow or attempt == _BULK_RETRIES - 1:
                    raise
                clamp //= 2
                if clamp < _MIN_CLAMP_CHARS:
                    # Already at the floor — halving further would only hide
                    # real data instead of solving the overflow.
                    raise
        assert vectors is not None
        validate_vectors(vectors, len(chunk), dim=cfg.embed_dim)
        out.extend(vectors)
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def startup_canary(cfg: Config, fingerprint_path: Path) -> None:
    """Assert dim==768 AND the canary string still embeds to the stored vector.

    First run persists the fingerprint. Later runs compare cosine; on dim
    mismatch or drift below COSINE_TOL we raise (caller reindexes/fails fast).
    """
    vectors = _post_embeddings([CANARY_STRING], cfg, timeout=30.0)
    vec = vectors[0]
    if len(vec) != cfg.embed_dim:
        raise CanaryError(f"startup canary: dim {len(vec)} != {cfg.embed_dim}")

    record = json.dumps({"canary": CANARY_STRING, "vector": vec})
    if not fingerprint_path.exists():
        fingerprint_path.parent.mkdir(parents=True, exist_ok=True)
        fingerprint_path.write_text(record)
        return

    stored = json.loads(fingerprint_path.read_text())
    # A bare list is the v1 format, made from a different canary text.
    if not isinstance(stored, dict) or stored.get("canary") != CANARY_STRING:
        fingerprint_path.write_text(record)
        return
    stored = stored.get("vector") or []
    if len(stored) != cfg.embed_dim:
        # stored fingerprint itself is stale -> re-fingerprint
        fingerprint_path.write_text(record)
        return
    sim = _cosine(vec, stored)
    if sim < COSINE_TOL:
        raise CanaryError(
            f"embed model drift: cosine {sim:.4f} < tol {COSINE_TOL}; "
            "reindex required (same-dim model swap suspected)"
        )


def _deadline_timeout(ms: int) -> "httpx.Timeout":
    """An httpx timeout whose phases sum to roughly `ms`, not `ms` EACH.

    `httpx.Timeout(0.8)` sets connect=read=write=pool=0.8 independently, so a
    single POST could spend ~2.4s inside an "800ms" deadline (and ~2.7s for the
    900ms rerank) — the per-turn recall path's real worst case was ~5.1s of
    network, not the ~1.7s its docstring claims. Connect and pool are clamped
    hard so only the read phase can consume the budget.
    """
    total = ms / 1000.0
    return httpx.Timeout(total, connect=min(0.15, total), pool=min(0.05, total))


def embed_with_deadline(text: str, *, cfg: Config, ms: int = 800) -> list[float] | None:
    """Embed one text under a hard deadline. None on timeout/5xx/refused."""
    try:
        vectors = _post_embeddings([text], cfg, timeout=_deadline_timeout(ms))
    except (httpx.HTTPError, EmbedBackendError, CanaryError, ValueError):
        # Validation runs inside _post_embeddings, before the length check
        # below. Wrong dimensions or malformed JSON must degrade just like an
        # unavailable model; bulk/index callers still receive those failures.
        return None
    if not vectors or len(vectors[0]) != cfg.embed_dim:
        return None
    return vectors[0]
