import json

import httpx
import pytest

from memd.integrations.hermes_memd_provider import MemdHttpProvider


def _provider(transport, store=None, token="tok"):
    client = httpx.Client(transport=transport, base_url="http://127.0.0.1:8077")
    return MemdHttpProvider(client=client, token=token, fallback_checkout=store)


def test_recall_calls_memd_http():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/recall"
        assert json.loads(request.content)["query"] == "gpu"
        return httpx.Response(200, json={"notes": [{"slug": "n1", "body": "the answer"}]})

    p = _provider(httpx.MockTransport(handler))
    res = p.handle_tool_call("memory_recall", {"query": "gpu", "top_k": 3})
    assert "the answer" in res


def test_save_sends_bearer_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/save"
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json={"slug": "s1", "action": "created"})

    p = _provider(httpx.MockTransport(handler))
    res = p.handle_tool_call(
        "memory_save", {"title": "T", "content": "B", "type": "project", "description": "d"}
    )
    assert "created" in res


def test_recall_falls_back_to_grep_when_memd_down(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("memd down")

    (tmp_path / "n.md").write_text(
        "---\ntitle: T\nslug: n\n---\nmodelserver guard recovery procedure\n", encoding="utf-8"
    )
    p = _provider(httpx.MockTransport(handler), store=str(tmp_path))
    res = p.handle_tool_call("memory_recall", {"query": "modelserver"})
    assert "modelserver guard recovery" in res


def test_save_is_skipped_quietly_when_memd_down(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("memd down")

    p = _provider(httpx.MockTransport(handler), store=str(tmp_path))
    res = p.handle_tool_call("memory_save", {"title": "T", "content": "B"})
    # Degraded mode is read-only: save queues/skips, never crashes the session.
    assert "queued" in res.lower() or "skipped" in res.lower() or "unavailable" in res.lower()
