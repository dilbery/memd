import httpx
import respx

from memd.config import Config, DEFAULT_EMBED_MODEL
from memd.embed import (
    embed, embed_with_deadline, startup_canary, CanaryError, CANARY_STRING,
)

EMBED_URL = "http://127.0.0.1:8081/v1/embeddings"


def _vec(seed: float, dim: int = 768) -> list[float]:
    return [seed] * dim


def _resp(vectors: list[list[float]]) -> httpx.Response:
    return httpx.Response(
        200, json={"data": [{"embedding": v} for v in vectors]}
    )


@respx.mock
def test_embed_returns_768(monkeypatch):
    monkeypatch.setenv("MEMD_EMBED_URL", "http://127.0.0.1:8081")
    # env_file=None keeps this hermetic — the real ~/.config/memd/env sets
    # MEMD_EMBED_MODEL, which would otherwise decide the assertion below.
    cfg = Config.from_env(env_file=None)
    route = respx.post(EMBED_URL).mock(return_value=_resp([_vec(0.1), _vec(0.2)]))
    out = embed(["hello", "world"], cfg)
    assert route.called
    assert len(out) == 2
    assert all(len(v) == 768 for v in out)
    # the CONFIGURED model id is what goes in the request body
    sent = route.calls.last.request
    assert DEFAULT_EMBED_MODEL.encode() in sent.content


@respx.mock
def test_startup_canary_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMD_EMBED_URL", "http://127.0.0.1:8081")
    cfg = Config.from_env()
    fp_path = tmp_path / "fingerprint.json"
    respx.post(EMBED_URL).mock(return_value=_resp([_vec(0.5)]))
    # First call stores the fingerprint; identical model -> identical vector -> ok.
    startup_canary(cfg, fingerprint_path=fp_path)
    assert fp_path.exists()
    startup_canary(cfg, fingerprint_path=fp_path)  # second call: cosine ~1.0


@respx.mock
def test_startup_canary_wrong_dim(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMD_EMBED_URL", "http://127.0.0.1:8081")
    cfg = Config.from_env()
    respx.post(EMBED_URL).mock(return_value=_resp([_vec(0.5, dim=512)]))
    try:
        startup_canary(cfg, fingerprint_path=tmp_path / "fp.json")
        assert False, "expected CanaryError on dim mismatch"
    except CanaryError as e:
        assert "768" in str(e)


@respx.mock
def test_startup_canary_model_drift(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMD_EMBED_URL", "http://127.0.0.1:8081")
    cfg = Config.from_env()
    fp_path = tmp_path / "fp.json"
    # Store fingerprint as the all-0.5 vector.
    respx.post(EMBED_URL).mock(return_value=_resp([_vec(0.5)]))
    startup_canary(cfg, fingerprint_path=fp_path)
    # Now a same-dim but orthogonal-ish vector -> cosine far below tol -> drift.
    drift = [1.0] + [0.0] * 767
    respx.post(EMBED_URL).mock(return_value=_resp([drift]))
    try:
        startup_canary(cfg, fingerprint_path=fp_path)
        assert False, "expected CanaryError on model drift"
    except CanaryError as e:
        assert "drift" in str(e).lower()


@respx.mock
def test_embed_with_deadline_timeout_returns_none(monkeypatch):
    monkeypatch.setenv("MEMD_EMBED_URL", "http://127.0.0.1:8081")
    cfg = Config.from_env()
    respx.post(EMBED_URL).mock(side_effect=httpx.ReadTimeout("slow backend"))
    assert embed_with_deadline("q", cfg=cfg, ms=50) is None


@respx.mock
def test_embed_with_deadline_5xx_returns_none(monkeypatch):
    monkeypatch.setenv("MEMD_EMBED_URL", "http://127.0.0.1:8081")
    cfg = Config.from_env()
    respx.post(EMBED_URL).mock(return_value=httpx.Response(503))
    assert embed_with_deadline("q", cfg=cfg, ms=200) is None


@respx.mock
def test_embed_with_deadline_success(monkeypatch):
    monkeypatch.setenv("MEMD_EMBED_URL", "http://127.0.0.1:8081")
    cfg = Config.from_env()
    respx.post(EMBED_URL).mock(return_value=_resp([_vec(0.3)]))
    v = embed_with_deadline("q", cfg=cfg, ms=800)
    assert v is not None and len(v) == 768
