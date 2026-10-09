"""Browser entry point, private note listing, and existing MCP dispatch."""
from fastapi.testclient import TestClient
import pytest

from memd import server, refresh
from memd.index import open_db


def test_dashboard_assets_and_generic_help():
    client = TestClient(server.app)
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "Memory index" in page.text and "Onboarding" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    for name in ("app.js", "style.css", "icon.svg", "health.js"):
        response = client.get("/ui/assets/" + name)
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/ui/assets/server.py").status_code == 404
    help_text = client.get("/help").text
    for personal in ("Amber", "Cobalt", "apphost", "gpuhost", "10.10.1.10"):
        assert personal not in page.text + help_text


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_notes_require_auth_before_opening_store(monkeypatch, headers):
    monkeypatch.setenv("MEMD_TOKEN", "test-token")
    monkeypatch.setattr(server, "open_db", lambda *a: pytest.fail("unauthorized database access"))
    assert TestClient(server.app).get("/ui/notes", headers=headers).status_code == 401


def test_notes_paginate_and_exclude_other_profiles_and_superseded(config, monkeypatch):
    refresh.ensure_lexical(config)
    db = open_db(config.db)
    db.execute("UPDATE notes SET superseded_by='replacement' WHERE slug='vmhost-proxmox-vm'")
    db.execute("INSERT INTO notes(slug,path,title,profile,body,git_blob) "
               "VALUES('private-other','private.md','Private','cobalt','secret','x')")
    db.commit(); db.close()
    client = TestClient(server.app)
    headers = {"Authorization": "Bearer " + config.token}
    first = client.get("/ui/notes?limit=1", headers=headers)
    second = client.get("/ui/notes?limit=1&offset=1", headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    a, b = first.json(), second.json()
    assert a["total"] == b["total"] == 2
    assert a["notes"][0]["slug"] != b["notes"][0]["slug"]
    assert {a["notes"][0]["slug"], b["notes"][0]["slug"]} == {"repo-hosting-policy", "gpuhost-inference-tuning"}
    assert client.get("/ui/notes?offset=999", headers=headers).json()["notes"] == []
    for query in ("offset=-1", "limit=0", "limit=101"):
        assert client.get("/ui/notes?" + query, headers=headers).status_code == 422
    monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "1")
    assert client.get("/ui/notes?profile=cobalt", headers=headers).status_code == 403


def test_root_mcp_and_legacy_path_still_dispatch(monkeypatch):
    monkeypatch.setenv("MEMD_TOKEN", "test-token")
    monkeypatch.setenv("MEMD_STARTUP_REFRESH", "0")
    with TestClient(server.create_token_app()) as client:
        for path in ("/", "/mcp/"):
            response = client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                   headers={"Authorization": "Bearer test-token",
                                            "Accept": "application/json, text/event-stream"})
            assert response.status_code == 200
            assert {t["name"] for t in response.json()["result"]["tools"]} >= {"recall", "read", "save"}
            assert client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
