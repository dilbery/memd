"""Bulk reindex embed must chunk + retry so a shared embedding backend (busy with
other clients) SLOWS the reindex instead of failing it. Found live: a single 30s
request for ~117 notes timed out under contention."""
import json

import httpx
import respx

from memd import embed as embed_mod
from memd.config import Config


def _emb(n):
    return {"data": [{"embedding": [0.01] * 768} for _ in range(n)]}


@respx.mock
def test_bulk_embed_chunks_into_small_requests():
    cfg = Config(embed_url="http://embed.test", embed_model="test-embed-model")
    seen = []

    def _resp(request):
        n = len(json.loads(request.content)["input"])
        seen.append(n)
        return httpx.Response(200, json=_emb(n))

    respx.post("http://embed.test/v1/embeddings").mock(side_effect=_resp)
    out = embed_mod.embed(["t"] * 20, cfg)
    assert len(out) == 20
    assert seen == [16, 4]  # 20 split into chunks of 16, never one giant request


@respx.mock
def test_bulk_embed_retries_a_timed_out_chunk(monkeypatch):
    monkeypatch.setattr(embed_mod.time, "sleep", lambda *a: None)  # no real backoff in test
    cfg = Config(embed_url="http://embed.test", embed_model="test-embed-model")
    calls = {"n": 0}

    def _resp(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("backend busy")
        n = len(json.loads(request.content)["input"])
        return httpx.Response(200, json=_emb(n))

    respx.post("http://embed.test/v1/embeddings").mock(side_effect=_resp)
    out = embed_mod.embed(["a", "b"], cfg)
    assert len(out) == 2
    assert calls["n"] == 2  # timed out once, backed off, retried, succeeded
