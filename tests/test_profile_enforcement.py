"""Per-instance profile enforcement (opt-in hard isolation).

When MEMD_ENFORCE_PROFILE is truthy, an instance is locked to its MEMD_PROFILE:
it serves that profile only and 403s any request for a different profile. When
unset, behaviour is unchanged (default 'amber', any profile accepted).
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from memd.server import app, _resolve_profile
from memd.config import DEFAULT_PROFILE


@pytest.fixture(autouse=True)
def mock_core(monkeypatch):
    """Replace the core seams so no real index/git/embedding is touched."""
    # Unauthenticated remote access is refused by default now; these tests
    # exercise the legacy open mode, which is an explicit opt-in.
    monkeypatch.setenv("MEMD_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.setattr(
        "memd.server._core_recall",
        lambda query, profile="amber", k=8, **kw: [],
    )
    # Mirror the real SaveResult contract: _maybe_dict() uses .to_dict()/vars(),
    # so return an object (SimpleNamespace has __dict__), not a bare dict.
    monkeypatch.setattr(
        "memd.server._core_save",
        lambda fact, profile="amber": SimpleNamespace(ok=True, profile=profile),
    )
    monkeypatch.setattr(
        "memd.server._core_reindex",
        lambda profile="amber", pull=True: {"ok": True, "profile": profile},
    )
    monkeypatch.setenv("MEMD_TOKEN", "t0ken")


@pytest.fixture
def client():
    return TestClient(app)


class TestProfileEnforcementIntegration:
    def test_unlocked_allows_any_profile(self, client, monkeypatch):
        monkeypatch.delenv("MEMD_ENFORCE_PROFILE", raising=False)
        assert client.post("/recall", json={"query": "x", "profile": "cobalt"}).status_code == 200
        assert client.post("/recall", json={"query": "x"}).status_code == 200

    def test_locked_to_cobalt_allows_only_cobalt(self, client, monkeypatch):
        monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "1")
        monkeypatch.setenv("MEMD_PROFILE", "cobalt")

        assert client.post("/recall", json={"query": "x", "profile": "cobalt"}).status_code == 200
        # omitted -> resolves to the locked profile
        assert client.post("/recall", json={"query": "x"}).status_code == 200

        resp = client.post("/recall", json={"query": "x", "profile": "amber"})
        assert resp.status_code == 403
        assert "instance locked to profile 'cobalt'" in resp.json()["detail"]

        headers = {"Authorization": "Bearer t0ken"}
        assert client.post(
            "/save", json={"title": "a", "body": "b", "profile": "amber"}, headers=headers
        ).status_code == 403
        assert client.post(
            "/save", json={"title": "a", "body": "b", "profile": "cobalt"}, headers=headers
        ).status_code == 200

    def test_truthy_whitespace_and_case(self, client, monkeypatch):
        monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "  TRUE  ")
        monkeypatch.setenv("MEMD_PROFILE", "cobalt")
        assert client.post("/recall", json={"query": "x", "profile": "amber"}).status_code == 403


class TestResolveProfileUnit:
    def test_unlocked(self, monkeypatch):
        monkeypatch.delenv("MEMD_ENFORCE_PROFILE", raising=False)
        assert _resolve_profile(None) == DEFAULT_PROFILE
        assert _resolve_profile("cobalt") == "cobalt"

    def test_locked_to_cobalt(self, monkeypatch):
        monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "1")
        monkeypatch.setenv("MEMD_PROFILE", "cobalt")
        assert _resolve_profile(None) == "cobalt"
        assert _resolve_profile("cobalt") == "cobalt"
        with pytest.raises(Exception) as exc_info:
            _resolve_profile("amber")
        assert exc_info.value.status_code == 403
        assert "instance locked to profile 'cobalt'" in str(exc_info.value.detail)
