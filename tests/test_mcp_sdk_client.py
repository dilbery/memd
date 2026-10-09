"""The HTTP MCP surface, driven by the real MCP SDK client in both protocol eras.

The other HTTP tests post hand-written JSON-RPC. This one lets the SDK negotiate:
"legacy" runs the initialize handshake, "2026-07-28" sends per-request envelopes
with no handshake, and the SDK routes the two eras through different server code.
Both must authenticate the bearer, stamp the caller on saves, validate arguments
against the advertised schema and serve the root and /mcp/ mounts alike.
"""
import subprocess

import anyio
import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

import memd.index as index_mod
import memd.mcp as mcp_mod
import memd.server as server_mod
from memd import actor

TOKEN = "x" * 40


@pytest.fixture
def store(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    subprocess.run(["git", "-C", str(clone), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    monkeypatch.setenv("MEMD_AMBER_CLONE", str(clone))
    monkeypatch.setenv("MEMD_AMBER_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("MEMD_CLONE", str(clone))
    monkeypatch.setenv("MEMD_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("MEMD_LOCAL_HOST", "any")
    monkeypatch.setenv("MEMD_PROFILE", "amber")
    monkeypatch.setenv("MEMD_TOKEN", TOKEN)
    monkeypatch.setenv("MEMD_EMBED_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("MEMD_RERANK_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("MEMD_STARTUP_REFRESH", "0")
    monkeypatch.setattr(index_mod, "embed", lambda texts, cfg: [[1.0] * 768 for _ in texts])
    monkeypatch.setattr(mcp_mod, "_core_recall", lambda *args, **kwargs: [])
    actor.set_actor("")
    return clone


def _client(app, token):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://testserver",
                              headers={"Authorization": "Bearer " + token})


@pytest.mark.parametrize("path", ["/", "/mcp/"])
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
def test_sdk_client_round_trip(store, path, mode):
    from memd.store import read_note
    app = server_mod.create_token_app()
    seen = {}

    async def main():
        async with app.router.lifespan_context(app):
            async with _client(app, TOKEN) as http, Client(
                    streamable_http_client("http://testserver" + path, http_client=http), mode=mode) as client:
                seen["version"] = client.protocol_version
                seen["tools"] = [tool.name for tool in (await client.list_tools()).tools]
                seen["save"] = await client.call_tool("save", {"title": "Era " + mode, "body": "via the sdk"})
                seen["recall"] = await client.call_tool("recall", {"q": "via the sdk"})
                seen["invalid"] = await client.call_tool("recall", {"k": [1]})
            async with _client(app, "wrong") as http:
                seen["refused"] = await http.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                                  headers={"Accept": "application/json, text/event-stream"})

    anyio.run(main)
    assert (seen["version"] == "2026-07-28") == (mode != "legacy"), seen["version"]
    assert seen["tools"] == ["recall", "save", "timeline", "ask", "read", "propose", "publish"]
    save = seen["save"]
    assert not save.is_error and save.structured_content["saved"] is True
    assert read_note(store, save.structured_content["slug"]).saved_by == "legacy"
    recall = seen["recall"]
    assert not recall.is_error
    assert recall.content[0].text == recall.structured_content["text"]
    invalid = seen["invalid"]
    assert invalid.is_error and invalid.content[0].text.startswith("Input validation error:")
    assert seen["refused"].status_code == 401
    assert seen["refused"].headers["www-authenticate"].startswith("Bearer")
