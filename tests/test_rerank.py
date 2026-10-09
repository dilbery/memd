import httpx
import respx

from memd.config import Config
from memd.rerank import rerank

RERANK_URL = "http://127.0.0.1:8000/api/v1/reranking"


def _cands():
    return [
        {"slug": "a", "body": "alpha body"},
        {"slug": "b", "body": "bravo body"},
        {"slug": "c", "body": "charlie body"},
    ]


@respx.mock
def test_rerank_orders_by_score(monkeypatch):
    monkeypatch.setenv("MEMD_RERANK_URL", "http://127.0.0.1:8000")
    cfg = Config.from_env()
    # logits unbounded; index 2 best, then 0, then 1
    respx.post(RERANK_URL).mock(return_value=httpx.Response(200, json={
        "results": [
            {"index": 2, "relevance_score": 9.1},
            {"index": 0, "relevance_score": -1.2},
            {"index": 1, "relevance_score": -6.8},
        ]
    }))
    out = rerank("q", _cands(), top_n=8, ms=400, cfg=cfg)
    assert [c["slug"] for c in out] == ["c", "a", "b"]


@respx.mock
def test_rerank_respects_top_n(monkeypatch):
    monkeypatch.setenv("MEMD_RERANK_URL", "http://127.0.0.1:8000")
    cfg = Config.from_env()
    respx.post(RERANK_URL).mock(return_value=httpx.Response(200, json={
        "results": [
            {"index": 0, "relevance_score": 5.0},
            {"index": 1, "relevance_score": 4.0},
            {"index": 2, "relevance_score": 3.0},
        ]
    }))
    out = rerank("q", _cands(), top_n=2, ms=400, cfg=cfg)
    assert len(out) == 2
    assert [c["slug"] for c in out] == ["a", "b"]


@respx.mock
def test_rerank_never_thresholds_negative_scores(monkeypatch):
    monkeypatch.setenv("MEMD_RERANK_URL", "http://127.0.0.1:8000")
    cfg = Config.from_env()
    # ALL scores negative; nothing must be dropped on a threshold.
    respx.post(RERANK_URL).mock(return_value=httpx.Response(200, json={
        "results": [
            {"index": 0, "relevance_score": -2.0},
            {"index": 1, "relevance_score": -3.0},
            {"index": 2, "relevance_score": -4.0},
        ]
    }))
    out = rerank("q", _cands(), top_n=8, ms=400, cfg=cfg)
    assert len(out) == 3  # none thresholded away


@respx.mock
def test_rerank_drops_out_of_range_index(monkeypatch):
    monkeypatch.setenv("MEMD_RERANK_URL", "http://127.0.0.1:8000")
    cfg = Config.from_env()
    respx.post(RERANK_URL).mock(return_value=httpx.Response(200, json={
        "results": [
            {"index": 99, "relevance_score": 9.0},   # out of range -> dropped
            {"index": 1, "relevance_score": 1.0},
        ]
    }))
    out = rerank("q", _cands(), top_n=8, ms=400, cfg=cfg)
    assert [c["slug"] for c in out] == ["b"]  # bogus index never mapped


@respx.mock
def test_rerank_timeout_returns_none(monkeypatch):
    monkeypatch.setenv("MEMD_RERANK_URL", "http://127.0.0.1:8000")
    cfg = Config.from_env()
    respx.post(RERANK_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    assert rerank("q", _cands(), top_n=8, ms=50, cfg=cfg) is None


@respx.mock
def test_rerank_5xx_returns_none(monkeypatch):
    monkeypatch.setenv("MEMD_RERANK_URL", "http://127.0.0.1:8000")
    cfg = Config.from_env()
    respx.post(RERANK_URL).mock(return_value=httpx.Response(503))
    assert rerank("q", _cands(), top_n=8, ms=400, cfg=cfg) is None
