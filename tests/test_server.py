"""Server surface tests against the SINGLE shipped app (token-guarded /save).

The prior dual-auth `create_app()` (X-Memd-Token) factory was removed in
fix-group 4c; the only app is the module-level `srv.app` (Bearer MEMD_TOKEN).
These drive that app, faking ONLY the in-process core seam (_core_recall /
_core_save / _health) so no embedding server / reranker / git is touched. The auth path is
real.
"""
from fastapi.testclient import TestClient

import memd.server as server_mod
from memd.store import Note


def _note(slug, body="b"):
    return Note(
        slug=slug, path=f"{slug}.md", title=slug.title(), profile="amber",
        host="gpuhost", importance=3, last_used=None, superseded_by=None,
        tags=[], grounding="ok", body=body, git_blob="abc",
    )


def _client(monkeypatch):
    # Unauthenticated remote access is refused by default now; these tests
    # exercise the legacy open mode, which is an explicit opt-in.
    monkeypatch.setenv("MEMD_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.setenv("MEMD_TOKEN", "test-token")
    return TestClient(server_mod.app)


_FULL_HEALTH = {
    "ok": True, "status": "ok", "dim": 768, "head": "deadbeef", "app_commit": "abc123",
    "checks": {"index": {"ok": True, "notes": 42, "detail": "notes=42"},
               "git": {"ok": True, "head": "deadbeef", "in_sync": True, "detail": "ok"},
               "embed": {"ok": False, "dim": None, "ms": 0, "detail": "embed error: host x"}},
}


def test_health_without_a_token_is_pass_fail_only(monkeypatch):
    monkeypatch.setattr(server_mod, "_health", lambda: _FULL_HEALTH)
    monkeypatch.delenv("MEMD_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.setenv("MEMD_TOKEN", "test-token")
    r = TestClient(server_mod.app).get("/health")
    assert r.status_code == 200
    # deploy.sh and memd-doctor read exactly these fields without a token.
    assert r.json() == {
        "ok": True, "status": "ok", "app_commit": "abc123",
        "checks": {"index": {"ok": True}, "git": {"ok": True, "in_sync": True},
                   "embed": {"ok": False}},
    }


def test_health_detail_with_a_token_or_in_open_mode(monkeypatch):
    monkeypatch.setattr(server_mod, "_health", lambda: _FULL_HEALTH)
    monkeypatch.delenv("MEMD_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.setenv("MEMD_TOKEN", "test-token")
    c = TestClient(server_mod.app)
    assert c.get("/health", headers={"Authorization": "Bearer test-token"}).json() == _FULL_HEALTH
    assert "dim" not in c.get("/health", headers={"Authorization": "Bearer wrong"}).json()
    # An explicitly open deployment already serves every note; hiding the
    # health detail from it protects nothing.
    assert _client(monkeypatch).get("/health").json() == _FULL_HEALTH


def test_recall_endpoint(monkeypatch):
    monkeypatch.setattr(
        server_mod, "_core_recall",
        lambda query, profile="amber", k=8: [_note("repo-hosting-policy"), _note("x")],
    )
    c = _client(monkeypatch)
    r = c.post("/recall", json={"query": "forgejo", "k": 5})
    assert r.status_code == 200
    slugs = [n["slug"] for n in r.json()["notes"]]
    assert "repo-hosting-policy" in slugs


def test_save_requires_token(monkeypatch):
    called = {"n": 0}

    def fake_save(fact, profile="amber"):
        called["n"] += 1
        from memd.save import SaveResult
        return SaveResult("brand-new", "created", False, "ok", "brand-new.md", saved=True)

    monkeypatch.setattr(server_mod, "_core_save", fake_save)
    c = _client(monkeypatch)
    # missing token -> 401, save NOT called
    r = c.post("/save", json={"title": "Brand New", "body": "x"})
    assert r.status_code == 401
    assert called["n"] == 0
    # correct token -> 200
    r2 = c.post(
        "/save",
        json={"title": "Brand New", "body": "x"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r2.status_code == 200
    assert r2.json()["slug"] == "brand-new"
    assert called["n"] == 1


def test_save_wrong_token_rejected(monkeypatch):
    monkeypatch.setattr(server_mod, "_core_save", lambda *a, **k: None)
    c = _client(monkeypatch)
    r = c.post(
        "/save", json={"title": "X", "body": "y"},
        headers={"Authorization": "Bearer WRONG"},
    )
    assert r.status_code == 401


def test_reindex_requires_token(monkeypatch):
    monkeypatch.setattr(
        server_mod, "_core_reindex",
        lambda profile="amber", pull=True: {"ok": True, "profile": profile, "notes": 0, "head": None, "pulled": pull},
    )
    c = _client(monkeypatch)
    r = c.post("/reindex", json={"title": "x"})
    assert r.status_code == 401


def test_reindex_endpoint(monkeypatch):
    called = {"profile": None, "pull": None}

    def fake_reindex(profile="amber", pull=True):
        called["profile"] = profile
        called["pull"] = pull
        return {"ok": True, "profile": "amber", "notes": 5, "head": "abc123", "pulled": True}

    monkeypatch.setattr(server_mod, "_core_reindex", fake_reindex)
    c = _client(monkeypatch)

    # default args
    r = c.post("/reindex", json={}, headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "profile": "amber", "notes": 5, "head": "abc123", "pulled": True}
    assert called["profile"] == "amber"
    assert called["pull"] is True

    # explicit pull=false
    r2 = c.post("/reindex", json={"pull": False}, headers={"Authorization": "Bearer test-token"})
    assert r2.status_code == 200
    assert called["pull"] is False


def test_reindex_wrong_token_rejected(monkeypatch):
    monkeypatch.setattr(
        server_mod, "_core_reindex",
        lambda profile="amber", pull=True: {"ok": True, "profile": profile, "notes": 0, "head": None, "pulled": pull},
    )
    c = _client(monkeypatch)
    r = c.post(
        "/reindex",
        json={},
        headers={"Authorization": "Bearer WRONG"},
    )
    assert r.status_code == 401
