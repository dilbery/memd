"""The caller stamp must survive the TRANSPORT, not just a direct save() call.

tests/test_actor.py sets the contextvar in the test's own thread and calls save()
directly, which proves nothing about the request path. FastAPI runs a synchronous
dependency and a synchronous endpoint in two different threadpool workers, and each
run_in_threadpool copies the context from the event loop task, never from the previous
worker. A contextvar set inside a sync dependency is therefore invisible to the
endpoint, and the stamp silently disappears over HTTP while every unit test passes.
That is exactly what happened on the first live save into a shared team store.
"""
import subprocess

import pytest
from fastapi.testclient import TestClient

import memd.server as server_mod
from memd import actor


@pytest.fixture
def store(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    subprocess.run(["git", "-C", str(clone), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-q", "--allow-empty", "-m", "init"],
                   check=True)
    monkeypatch.setenv("MEMD_AMBER_CLONE", str(clone))
    monkeypatch.setenv("MEMD_AMBER_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("MEMD_CLONE", str(clone))
    monkeypatch.setenv("MEMD_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("MEMD_LOCAL_HOST", "any")
    monkeypatch.setenv("MEMD_PROFILE", "amber")
    monkeypatch.setenv("MEMD_TOKEN", "x" * 40)
    monkeypatch.setenv("MEMD_EMBED_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("MEMD_RERANK_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("MEMD_STARTUP_REFRESH", "0")
    actor.set_actor("")
    return clone


def _last_commit(clone):
    return subprocess.run(["git", "-C", str(clone), "log", "-1", "--format=%B"],
                          capture_output=True, text=True, check=True).stdout


def test_rest_save_stamps_the_token_label(store):
    """The label check_bearer returns must reach the note and the commit."""
    from memd.store import read_note
    # A fresh app per test: the module-level one carries a StreamableHTTPSessionManager
    # whose run() may only be entered once per process.
    with TestClient(server_mod.create_token_app()) as client:
        response = client.post(
            "/save",
            headers={"Authorization": "Bearer " + "x" * 40},
            json={"title": "Transport stamp", "body": "saved over REST"},
        )
    assert response.status_code == 200, response.text
    slug = response.json()["slug"]
    assert read_note(store, slug).saved_by == "legacy"
    assert "Saved-By: legacy" in _last_commit(store)


def test_rest_save_without_a_token_is_still_refused(store):
    with TestClient(server_mod.create_token_app()) as client:
        response = client.post("/save", json={"title": "No token", "body": "nope"})
    assert response.status_code == 401


def test_mcp_save_stamps_the_token_label(store):
    """The MCP transport runs the core under asyncio.to_thread, which copies context."""
    from memd.store import read_note
    with TestClient(server_mod.create_token_app()) as client:
        response = client.post(
            "/mcp/",
            headers={
                "Authorization": "Bearer " + "x" * 40,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "save",
                           "arguments": {"title": "MCP stamp", "body": "saved over MCP"}},
            },
        )
    assert response.status_code == 200, response.text
    note = read_note(store, "mcp-stamp")
    assert note is not None, "the MCP save did not land"
    assert note.saved_by == "legacy"
    assert "Saved-By: legacy" in _last_commit(store)
