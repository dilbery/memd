"""Reranker adapter.

Endpoint path comes from Config.rerank_path: Lemonade's POST /api/v1/reranking
(gerund) by default, LiteLLM's Cohere-shaped POST /v1/rerank on a LiteLLM gateway. Both
take {model, query, documents, top_n} and answer results[].index/relevance_score.
Rules:
  - NEVER threshold relevance_score (unbounded logit); order by it, take top_n.
  - candidates immutable between submit and map-back.
  - range-check returned index against len(candidates); drop out-of-range.
  - hard deadline; None on timeout/5xx/refused (caller falls back per ladder).

Config is threaded as a keyword-only param (`*, cfg`) so the documented
positional call shape `rerank(query, candidates, top_n=8, ms=400)` is preserved.
"""
from __future__ import annotations

import httpx

from memd.config import Config, model_headers
from memd.embed import _deadline_timeout

# A bge-reranker served by llama-server with -b/-ub 2048: each query+doc pair
# must fit ONE 2048-token batch or llama-server rejects the pair and the whole
# rerank returns empty results (HTTP 200 — silent). Dense markdown tokenizes at
# ~2.5 chars/token under XLM-R, so budget ~4700 chars per pair. Calibrated
# against a live server: 1200+3500 passes on the densest memory doc; 2000+4500
# fails.
_QUERY_TRUNC_CHARS = 1200
_DOC_TRUNC_CHARS = 3500


def rerank(
    query: str,
    candidates: list[dict],
    top_n: int = 8,
    ms: int = 400,
    *,
    cfg: Config,
) -> list[dict] | None:
    if not candidates:
        return []
    # Snapshot: the array is immutable for the duration of this call.
    frozen = list(candidates)
    documents = [c["body"][:_DOC_TRUNC_CHARS] for c in frozen]
    url = f"{cfg.rerank_url.rstrip(chr(47))}{cfg.rerank_path}"
    from memd.metrics import backend_call, error_kind
    with backend_call("rerank") as call:
        try:
            resp = httpx.post(
                url,
                json={
                    "model": cfg.rerank_model,
                    "query": query[:_QUERY_TRUNC_CHARS],
                    "documents": documents,
                    "top_n": top_n,
                },
                headers=model_headers(cfg, url),
                timeout=_deadline_timeout(ms),
            )
            resp.raise_for_status()
            results = resp.json()["results"]
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError) as exc:
            call.fail(error_kind(exc))
            return None
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            call.fail(error_kind(exc))
            return None
        out = _map_back(results, frozen, top_n)
        if out is None:
            call.fail("bad_response")
        return out


def _map_back(results, frozen: list[dict], top_n: int) -> list[dict] | None:
    # An HTTP 200 carrying no results is a FAILURE, not "nothing is relevant".
    # llama-server answers 200 with `{"results": []}` when it rejects the pair
    # (see this module's header), and returning [] made the caller's `if reranked
    # is not None` branch succeed with an empty ranking — so recall collapsed to
    # the core set, or to a completely EMPTY list on the include_core=False hook
    # path, without ever engaging the degradation ladder. None is what makes the
    # caller fall back to vector-cosine or BM25 order.
    if not results:
        return None

    # Order by relevance_score desc; NEVER threshold. Range-check every index.
    # `.get(..., 0.0)` because a malformed item lacking the key previously raised
    # KeyError from here — outside the try above — straight out of recall().
    ordered = sorted(results, key=lambda r: r.get("relevance_score") or 0.0,
                     reverse=True)
    out: list[dict] = []
    n = len(frozen)
    for r in ordered:
        idx = r.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= n:
            continue  # stale / out-of-range index dropped, never mis-mapped
        out.append(frozen[idx])
        if len(out) >= top_n:
            break
    # Every index was unusable -> the response told us nothing; degrade instead of
    # silently returning an empty ranking.
    return out or None
