"""An administered store's Config carries the same env-derived settings as any
other store, and the model key only travels to operator-configured origins."""
import httpx
import respx

import memd.control as control
from memd.config import Config, model_headers, url_origin
from memd.embed import embed


def _managed(monkeypatch, tmp_path, **settings):
    row = {"config": {"clone_path": str(tmp_path / "clone"), "db_path": str(tmp_path / "m.db"), **settings}}
    monkeypatch.setattr(control, "store", lambda name: row if name == "team" else None)


ENV = {"MEMD_PROFILE": "team", "MEMD_MODEL_API_KEY": "k-secret",
       "MEMD_EMBED_URL": "https://gateway.example.com", "MEMD_RERANK_URL": "https://gateway.example.com",
       "MEMD_RERANK_PATH": "/v1/rerank", "MEMD_EMBED_DIM": "1024", "MEMD_EMBED_DEADLINE_MS": "2500"}


def test_managed_store_keeps_env_model_settings(monkeypatch, tmp_path):
    _managed(monkeypatch, tmp_path)
    cfg = Config.from_env(ENV, env_file=None)
    assert cfg.profile == "team" and cfg.clone == tmp_path / "clone"
    assert cfg.model_api_key == "k-secret" and "k-secret" not in repr(cfg)
    assert (cfg.rerank_path, cfg.embed_dim, cfg.embed_deadline_ms) == ("/v1/rerank", 1024, 2500)
    assert cfg.model_key_origins == ("https://gateway.example.com:443",)


def test_key_goes_only_to_operator_origins(monkeypatch, tmp_path):
    _managed(monkeypatch, tmp_path, embed_url="https://elsewhere.example.org")
    cfg = Config.from_env(ENV, env_file=None)
    assert cfg.embed_url == "https://elsewhere.example.org"
    assert model_headers(cfg, "https://elsewhere.example.org/v1/embeddings") == {}
    assert model_headers(cfg, "https://gateway.example.com/v1/rerank") == {"Authorization": "Bearer k-secret"}
    assert model_headers(cfg) == {}


@respx.mock
def test_embed_to_a_store_specific_host_sends_no_key(monkeypatch, tmp_path):
    _managed(monkeypatch, tmp_path, embed_url="https://elsewhere.example.org")
    cfg = Config.from_env(ENV, env_file=None)
    route = respx.post("https://elsewhere.example.org/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"embedding": [0.1] * cfg.embed_dim}]}))
    embed(["x"], cfg)
    assert "authorization" not in route.calls.last.request.headers


def test_unmanaged_config_still_sends_key_everywhere():
    cfg = Config.from_env({"MEMD_MODEL_API_KEY": "k", "MEMD_EMBED_URL": "http://127.0.0.1:9"}, env_file=None)
    assert cfg.model_key_origins is None
    assert model_headers(cfg) == model_headers(cfg, "http://any.example.net/x") == {"Authorization": "Bearer k"}


def test_url_origin_normalises():
    assert url_origin("HTTPS://Gateway.Example.com/v1") == "https://gateway.example.com:443"
    assert url_origin("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    assert url_origin("ftp://x") is None and url_origin("") is None and url_origin("http://[bad") is None
