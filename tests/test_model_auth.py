"""Model calls carry a bearer when MEMD_MODEL_API_KEY is set, and nothing otherwise."""
import httpx
import respx

from memd.config import Config, model_headers
from memd.embed import _post_embeddings
from memd.rerank import rerank


def _cfg(**kw):
    base = dict(embed_url="http://models.test", rerank_url="http://models.test")
    base.update(kw)
    return Config(**base)


def test_model_headers_empty_without_key():
    assert model_headers(_cfg()) == {}


def test_model_headers_bearer_with_key():
    assert model_headers(_cfg(model_api_key="sk-abc")) == {"Authorization": "Bearer sk-abc"}


def test_config_reads_model_api_key_from_env():
    cfg = Config.from_env({"MEMD_MODEL_API_KEY": "sk-env"}, env_file=None)
    assert cfg.model_api_key == "sk-env"


def test_config_model_api_key_absent_is_none():
    assert Config.from_env({}, env_file=None).model_api_key is None


@respx.mock
def test_embeddings_send_bearer():
    route = respx.post("http://models.test/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 768}]}))
    _post_embeddings(["x"], _cfg(model_api_key="sk-e"), timeout=5.0)
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-e"


@respx.mock
def test_embeddings_send_no_auth_header_without_key():
    route = respx.post("http://models.test/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 768}]}))
    _post_embeddings(["x"], _cfg(), timeout=5.0)
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
def test_rerank_sends_bearer():
    route = respx.post("http://models.test/api/v1/reranking").mock(
        return_value=httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 1.0}]}))
    out = rerank("q", [{"slug": "a", "body": "a"}], top_n=1, ms=500, cfg=_cfg(model_api_key="sk-r"))
    assert out and out[0]["slug"] == "a"
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-r"
