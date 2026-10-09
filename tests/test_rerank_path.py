"""The reranker endpoint path is configurable: Lemonade by default, LiteLLM on a gateway."""
import httpx
import respx

from memd.config import Config
from memd.rerank import rerank


def test_default_path_is_lemonade():
    assert Config().rerank_path == "/api/v1/reranking"


def test_env_overrides_path():
    cfg = Config.from_env({"MEMD_RERANK_PATH": "/v1/rerank"}, env_file=None)
    assert cfg.rerank_path == "/v1/rerank"


@respx.mock
def test_rerank_posts_to_configured_path():
    route = respx.post("http://models.test/v1/rerank").mock(
        return_value=httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.9}]}))
    cfg = Config(rerank_url="http://models.test", rerank_path="/v1/rerank")
    out = rerank("q", [{"slug": "a", "body": "a"}], top_n=1, ms=500, cfg=cfg)
    assert route.called and out[0]["slug"] == "a"


@respx.mock
def test_trailing_slash_on_url_does_not_double_up():
    route = respx.post("http://models.test/v1/rerank").mock(
        return_value=httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.9}]}))
    cfg = Config(rerank_url="http://models.test/", rerank_path="/v1/rerank")
    rerank("q", [{"slug": "a", "body": "a"}], top_n=1, ms=500, cfg=cfg)
    assert route.called


@respx.mock
def test_cohere_shaped_response_maps_back_by_index():
    """LiteLLM follows the Cohere shape: same results[].index and relevance_score."""
    respx.post("http://models.test/v1/rerank").mock(
        return_value=httpx.Response(200, json={"results": [
            {"index": 1, "relevance_score": 9.1}, {"index": 0, "relevance_score": 0.2}]}))
    cfg = Config(rerank_url="http://models.test", rerank_path="/v1/rerank")
    out = rerank("q", [{"slug": "first", "body": "a"}, {"slug": "second", "body": "b"}],
                 top_n=2, ms=500, cfg=cfg)
    assert [c["slug"] for c in out] == ["second", "first"]
