"""memd.llm: OpenAI-compatible chat client (respx-mocked, hermetic)."""
import json

import httpx
import pytest
import respx

from memd import llm
from memd.config import Config


def _ok(content="hello"):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


def test_config_reads_llm_env(monkeypatch):
    monkeypatch.setenv("MEMD_LLM_URL", "http://llm.test/")
    monkeypatch.setenv("MEMD_LLM_MODEL", "chat-model")
    monkeypatch.setenv("MEMD_LLM_TIMEOUT_S", "9999")
    cfg = Config.from_env()
    assert cfg.llm_url == "http://llm.test/" and cfg.llm_model == "chat-model"
    assert cfg.llm_timeout_s == 600.0
    assert llm.enabled(cfg)


def test_off_by_default():
    cfg = Config.from_env()
    assert cfg.llm_url is None and not llm.enabled(cfg)
    with pytest.raises(llm.LLMError, match="MEMD_LLM_URL"):
        llm.chat([{"role": "user", "content": "x"}], cfg=cfg)


def test_model_required():
    with pytest.raises(llm.LLMError, match="MEMD_LLM_MODEL"):
        llm.chat([], cfg=Config(llm_url="http://llm.test"))


@pytest.mark.parametrize("base,url", [
    ("http://llm.test", "http://llm.test/v1/chat/completions"),
    ("http://llm.test/v1/", "http://llm.test/v1/chat/completions"),
    ("http://llm.test/openai/v1/chat/completions", "http://llm.test/openai/v1/chat/completions"),
])
def test_completions_url(base, url):
    assert llm.completions_url(base) == url


@respx.mock
def test_chat_sends_openai_shape_with_model_key(monkeypatch):
    monkeypatch.setenv("MEMD_LLM_URL", "http://llm.test")
    monkeypatch.setenv("MEMD_LLM_MODEL", "chat-model")
    monkeypatch.setenv("MEMD_MODEL_API_KEY", "sk-test")
    route = respx.post("http://llm.test/v1/chat/completions").mock(return_value=_ok("hi there"))
    out = llm.chat([{"role": "user", "content": "q"}], cfg=Config.from_env(), max_tokens=50,
                   temperature=0.2, response_format={"type": "json_object"})
    assert out == "hi there"
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body == {"model": "chat-model", "messages": [{"role": "user", "content": "q"}],
                    "max_tokens": 50, "temperature": 0.2, "stream": False,
                    "response_format": {"type": "json_object"}}


@respx.mock
def test_chat_omits_response_format_and_auth_when_unset():
    route = respx.post("http://llm.test/v1/chat/completions").mock(return_value=_ok())
    llm.chat([], cfg=Config(llm_url="http://llm.test", llm_model="m"))
    req = route.calls[0].request
    assert "authorization" not in req.headers
    assert "response_format" not in json.loads(req.content)


@respx.mock
@pytest.mark.parametrize("response", [
    httpx.Response(500, text="down"),
    httpx.Response(401, text="nope"),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"choices": []}),
    httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
    httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]}),
    httpx.Response(200, json=["weird"]),
])
def test_chat_failures_raise_llmerror(response):
    respx.post("http://llm.test/v1/chat/completions").mock(return_value=response)
    with pytest.raises(llm.LLMError):
        llm.chat([], cfg=Config(llm_url="http://llm.test", llm_model="m"))


@respx.mock
def test_chat_timeout_raises_llmerror():
    respx.post("http://llm.test/v1/chat/completions").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(llm.LLMError, match="unreachable"):
        llm.chat([], cfg=Config(llm_url="http://llm.test", llm_model="m", llm_timeout_s=1.0))


def test_timeout_is_bounded():
    t = llm._timeout(120.0)
    assert t.read == 120.0 and t.connect == 5.0 and t.pool == 5.0


@respx.mock
def test_chat_error_status_reports_code_and_body():
    respx.post("http://llm.test/v1/chat/completions").mock(return_value=httpx.Response(503, text="overloaded"))
    with pytest.raises(llm.LLMError, match="HTTP 503: overloaded"):
        llm.chat([], cfg=Config(llm_url="http://llm.test", llm_model="m"))


@respx.mock
def test_chat_deadline_bounds_the_whole_call(monkeypatch):
    """httpx's timeout is per read; a backend trickling bytes must still stop at the deadline."""
    respx.post("http://llm.test/v1/chat/completions").mock(return_value=_ok())
    clock = iter([0.0, 5.0])
    monkeypatch.setattr(llm, "_now", lambda: next(clock))
    with pytest.raises(llm.LLMError, match="exceeded the 2s deadline"):
        llm.chat([], cfg=Config(llm_url="http://llm.test", llm_model="m", llm_timeout_s=2.0))
