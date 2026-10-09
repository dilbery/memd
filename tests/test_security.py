import pytest
from fastapi.testclient import TestClient

import memd.server as srv


@pytest.fixture
def client(monkeypatch):
    # Unauthenticated remote access is refused by default now; these tests
    # exercise the legacy open mode, which is an explicit opt-in.
    monkeypatch.setenv("MEMD_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.setenv("MEMD_TOKEN", "s3cret")

    # Stub the core so no embedding server / reranker / git is touched.
    class _Note:
        def __init__(self):
            self.slug = "n1"

        def to_dict(self):
            return {"slug": "n1", "body": "b"}

    class _SaveResult:
        def to_dict(self):
            return {"slug": "n1", "action": "created"}

    monkeypatch.setattr(srv, "_core_recall", lambda q, profile="amber", k=8: [_Note()])
    monkeypatch.setattr(srv, "_core_save", lambda fact, profile="amber": _SaveResult())
    monkeypatch.setattr(srv, "_health", lambda: {"ok": True, "dim": 768, "head": "abc123"})
    return TestClient(srv.app)


def test_recall_is_open_no_token_required(client):
    r = client.post("/recall", json={"query": "gpu"})
    assert r.status_code == 200
    assert r.json()["notes"][0]["slug"] == "n1"


def test_health_is_open(client, monkeypatch):
    """Open for liveness, but not a free intelligence report.

    A container healthcheck, deploy.sh or a LAN monitor needs the status and
    each check's pass/fail; none needs the note count, the git HEAD commit or
    per-service timings, all of which this endpoint used to hand to anyone who
    could reach the port. (The explicitly open mode the fixture enables serves
    every note anyway, so it keeps the full payload; test it closed.)
    """
    monkeypatch.delenv("MEMD_ALLOW_UNAUTHENTICATED")
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "dim" not in body and "head" not in body
    for check in body["checks"].values():
        assert set(check) <= {"ok", "in_sync"}


def test_save_without_token_is_rejected(client):
    r = client.post("/save", json={"title": "T", "body": "B"})
    assert r.status_code == 401


def test_save_with_wrong_token_is_rejected(client):
    r = client.post(
        "/save",
        json={"title": "T", "body": "B"},
        headers={"Authorization": "Bearer nope"},
    )
    assert r.status_code == 401


def test_save_with_correct_token_succeeds(client):
    r = client.post(
        "/save",
        json={"title": "T", "body": "B"},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert r.status_code == 200
    assert r.json()["action"] == "created"


@pytest.fixture
def per_client_token(monkeypatch, tmp_path):
    """Issue one per-machine token via MEMD_TOKENS_FILE, as onboard.sh does."""
    tokens = tmp_path / "tokens"
    tokens.write_text("gpuhost-pi mem_amber_perclient\n")
    monkeypatch.setenv("MEMD_TOKENS_FILE", str(tokens))
    return "mem_amber_perclient"


def test_save_accepts_a_per_client_token(client, per_client_token):
    """REST must accept the same tokens MCP does.

    onboard.sh gives every machine its own token in MEMD_TOKENS_FILE. When the
    write guard compared only against the master MEMD_TOKEN, those tokens
    worked over MCP but 401'd on /save and /reindex -- which reads as an
    expired token rather than a wrong endpoint.
    """
    r = client.post(
        "/save",
        json={"title": "T", "body": "B"},
        headers={"Authorization": f"Bearer {per_client_token}"},
    )
    assert r.status_code == 200


def test_master_token_still_works_when_a_token_file_exists(client, per_client_token):
    r = client.post(
        "/save",
        json={"title": "T", "body": "B"},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert r.status_code == 200


def test_unknown_token_rejected_even_with_a_token_file(client, per_client_token):
    r = client.post(
        "/save",
        json={"title": "T", "body": "B"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 401


def test_enforced_recall_accepts_a_per_client_token(
    client, per_client_token, monkeypatch
):
    """Flipping MEMD_REQUIRE_RECALL_TOKEN must not blind every machine.

    recall_token_guard shares require_token's accepted set, so enforcement
    accepts per-client tokens too. Enforcing against the master token alone
    would 401 every onboarded client at once -- silently, because the recall
    hook fails open and just returns no memory.
    """
    monkeypatch.setenv("MEMD_REQUIRE_RECALL_TOKEN", "1")
    r = client.post(
        "/recall",
        json={"query": "gpu"},
        headers={"Authorization": f"Bearer {per_client_token}"},
    )
    assert r.status_code == 200
    assert r.json()["notes"][0]["slug"] == "n1"


def test_enforced_recall_rejects_a_missing_token(client, monkeypatch):
    monkeypatch.setenv("MEMD_REQUIRE_RECALL_TOKEN", "1")
    r = client.post("/recall", json={"query": "gpu"})
    assert r.status_code == 401


def test_unauthenticated_remote_recall_is_refused_by_default(monkeypatch):
    """The default must be closed.

    authorize() used to allow any remote caller whenever no control database
    was configured, so one unset MEMD_ADMIN_DB served the whole corpus
    unauthenticated with nothing in the logs to say so. Opening it is now an
    explicit choice, not an accident of configuration.
    """
    monkeypatch.delenv("MEMD_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.delenv("MEMD_ADMIN_DB", raising=False)
    from memd import access
    token = access.remote_request.set(True)
    marker = access.current.set(None)
    try:
        with pytest.raises(PermissionError):
            access.authorize("any-store")
    finally:
        access.remote_request.reset(token)
        access.current.reset(marker)
