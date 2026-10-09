import concurrent.futures
import json
import sqlite3
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from memd.registry import Registry, Conflict, ControlError, Denied, principal


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "control" / "control.db"
    monkeypatch.setenv("MEMD_CONTROL_DB", str(path))
    monkeypatch.setenv("MEMD_STORES_ROOT", str(tmp_path / "stores"))
    monkeypatch.setenv("MEMD_REQUIRE_RECALL_TOKEN", "1")
    registry = Registry(); registry.initialize()
    principal.set(None)
    yield registry
    principal.set(None)


def issue(registry, **kwargs):
    fields = dict(actor="admin-sub", operation_id=str(uuid.uuid4()), label="test-job", owner="Operations",
                  purpose="Synthetic test", stores=["alice@example.invalid"], operations=["recall", "read"])
    fields.update(kwargs)
    return registry.issue(**fields)


def test_verifier_only_and_single_use_idempotency(registry):
    op = str(uuid.uuid4()); result = issue(registry, operation_id=op)
    with pytest.raises(Conflict):
        issue(registry, operation_id=op)
    assert len(registry.list()) == len(registry.events()) == 1
    assert result["secret"] not in json.dumps(registry.list()) + json.dumps(registry.events())
    with registry.connection() as db:
        dump = "\n".join(db.iterdump())
    assert result["secret"] not in dump
    assert "verifier" not in json.dumps(registry.list())
    assert registry.authenticate(result["secret"])["id"] == result["token"]["id"]
    assert registry.authenticate(result["secret"] + "x") is None


def test_revocation_never_falls_back_to_legacy_file(registry, tmp_path, monkeypatch):
    old = "legacy-credential-" + "a" * 40
    registry.import_legacy({old:"old-job"})
    path = tmp_path / "tokens"; path.write_text("old-job " + old)
    monkeypatch.setenv("MEMD_TOKENS_FILE", str(path))
    from memd.mcp_http import check_bearer
    assert check_bearer("Bearer " + old) == "old-job"
    row = registry.list()[0]
    registry.revoke(row["id"], actor="test", operation_id=str(uuid.uuid4()), revision=1)
    assert check_bearer("Bearer " + old) is None
    with pytest.raises(Conflict): registry.import_legacy({old:"old-job"})
    with pytest.raises(Denied): registry.authorize(row["id"],"read",None)


def test_rotation_overlap_and_revision(registry):
    original = issue(registry)
    replacement = registry.rotate(original["token"]["id"], actor="a", operation_id=str(uuid.uuid4()), revision=1, overlap_hours=0)
    assert registry.authenticate(original["secret"]) is None
    assert registry.authenticate(replacement["secret"])
    assert replacement["token"]["rotated_from"] == original["token"]["id"]
    with pytest.raises(Conflict): registry.revoke(original["token"]["id"], actor="a", operation_id=str(uuid.uuid4()), revision=1)


@pytest.mark.parametrize("fields", [{"stores":[]},{"stores":["one","two"],"operations":["save"]},
    {"operations":["admin"]},{"days":366},{"days":True},{"owner":""}])
def test_reject_invalid_scope(registry, fields):
    with pytest.raises(ControlError): issue(registry, **fields)
    assert registry.list() == registry.events() == []


def test_scopes_and_rechecks(registry):
    issued = issue(registry); token_id = issued["token"]["id"]
    assert registry.authorize(token_id,"recall",None) == "alice@example.invalid"
    with pytest.raises(Denied): registry.authorize(token_id,"save",None)
    with pytest.raises(Denied): registry.authorize(token_id,"read","bob@example.invalid")
    multi = issue(registry, stores=["one","two"], operations=["stats","reindex"])
    with pytest.raises(Denied): registry.authorize(multi["token"]["id"],"stats",None)
    assert registry.authorize(multi["token"]["id"],"stats","two") == "two"


def test_concurrent_create_and_revoke_are_atomic(registry):
    op = str(uuid.uuid4())
    def attempt(_):
        try: return issue(registry, operation_id=op)
        except Conflict: return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(attempt,range(10)))
    assert sum(r is not None for r in results) == 1
    assert len(registry.list()) == len(registry.events()) == 1
    token = registry.list()[0]
    def revoke(_):
        try: return registry.revoke(token["id"], actor="a", operation_id=str(uuid.uuid4()), revision=1)
        except Conflict: return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(revoke,range(10)))
    assert sum(r is not None for r in results) == 1
    assert len(registry.events()) == 2


def test_audit_failure_rolls_back_change(registry, monkeypatch):
    token = issue(registry)
    def fail(*args, **kwargs): raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(registry,"_audit",fail)
    with pytest.raises(sqlite3.OperationalError): registry.revoke(token["token"]["id"],actor="a",operation_id=str(uuid.uuid4()),revision=1)
    assert registry.authenticate(token["secret"])


def test_backup_restores_revocations_but_no_sessions(registry, tmp_path):
    token = issue(registry)
    registry.revoke(token["token"]["id"],actor="a",operation_id=str(uuid.uuid4()),revision=1)
    cookie = registry.session_create({"kind":"admin"})
    path = tmp_path / "restore.db"; registry.backup(path)
    restored = Registry(path)
    assert restored.authenticate(token["secret"]) is None
    assert restored.session_get(cookie) is None
    assert restored.list() == registry.list()
    with restored.connection() as db: assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_rest_mcp_same_scope_and_no_identity_leak(registry, monkeypatch):
    from memd.server import create_token_app
    from memd.mcp import _call_tool_sync
    from memd.mcp_http import _authenticate
    monkeypatch.setattr("memd.server._core_recall",lambda *a,**k: [])
    monkeypatch.setattr("memd.mcp._core_recall",lambda *a,**k: [])
    monkeypatch.setattr("memd.mcp._cfg_for",lambda p: None)
    token = issue(registry)
    headers = {"Authorization":"Bearer " + token["secret"]}
    with TestClient(create_token_app()) as client:
        result = client.post("/recall",headers=headers,json={"query":"hello"})
        assert result.status_code == 200
        assert result.json()["profile"] == "alice@example.invalid"
        assert client.post("/recall",headers=headers,json={"profile":"bob@example.invalid"}).status_code == 403
        assert client.post("/save",headers=headers,json={"title":"test","body":"test"}).status_code == 403
        assert client.post("/recall",json={}).status_code == 401
        assert _authenticate(headers["Authorization"])
        with pytest.raises(Denied): _call_tool_sync("read",{"profile":"bob@example.invalid","slug":"x"})
        # Revoke after the transport already authenticated this context.
        registry.revoke(token["token"]["id"],actor="a",operation_id=str(uuid.uuid4()),revision=1)
        with pytest.raises(Denied): _call_tool_sync("recall",{"query":"hi"})
        assert client.post("/recall",headers=headers,json={}).status_code == 401


def test_session_idle_absolute_expiry_and_atomic_consumption(registry):
    cookie = registry.session_create({"kind":"login"})
    assert registry.session_consume(cookie) == {"kind":"login"}
    assert registry.session_consume(cookie) is None
    for column in ("touched","expires"):
        cookie = registry.session_create({"kind":"admin"})
        with registry.connection(write=True) as db: db.execute(f"UPDATE sessions SET {column}=?",(time.time()-1000,))
        assert registry.session_get(cookie) is None
