"""The security property, driven through the real transports and the real core.

    User A cannot reach user B's store by ANY request field.

Everything else in Plan 2 is machinery; this is the thing being bought. So it
is tested where an attacker actually is -- at the REST surface, at the MCP tool
surface, and against real git clones through the unmocked recall/save core --
rather than only at the resolver that happens to implement it today.

Identity is bound directly here because piece 1 deliberately ships no identity
source. §9b's OIDC layer is what will bind it, from a validated `email` claim.
Binding it in the test is what lets these assertions outlive that decision: if
OIDC is swapped for something else, these tests still describe the contract the
replacement has to meet.
"""
import dataclasses
import json
import subprocess
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import memd.mcp as mcp
from memd.config import Config, DEFAULT_EMBED_URL, DEFAULT_RERANK_PATH, DEFAULT_RERANK_URL
from memd.identity import bind_identity, clear_identity
from memd.profiles import CrossProfileViolation
from memd.recall import recall as core_recall
from memd.save import save as core_save
from memd.server import app
from memd.store import read_note

EMBED_URL = f"{DEFAULT_EMBED_URL}/v1/embeddings"
RERANK_URL = f"{DEFAULT_RERANK_URL.rstrip('/')}{DEFAULT_RERANK_PATH}"

ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _seed_store(root: Path, store: str, marker: str) -> Path:
    """A real git clone at <root>/<store>/clone with one note only they should see."""
    clone = root / store / "clone"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q")
    _git(clone, "config", "user.email", "test@memd")
    _git(clone, "config", "user.name", "memd-test")
    (clone / f"{marker.lower()}.md").write_text(
        f"---\ntitle: {marker}\nslug: {marker.lower()}\nprofile: {store}\n"
        f"host: any\nimportance: 5\ngrounding: ok\n---\n"
        f"{marker} is private to {store} and must never reach anyone else.\n"
    )
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "seed")
    return clone


@pytest.fixture(autouse=True)
def _identity_is_never_inherited():
    clear_identity()
    yield
    clear_identity()


@pytest.fixture
def two_stores(tmp_path, monkeypatch):
    root = tmp_path / "stores"
    root.mkdir()
    monkeypatch.setenv("MEMD_STORES_ROOT", str(root))
    monkeypatch.delenv("MEMD_CLONE", raising=False)
    monkeypatch.delenv("MEMD_DB", raising=False)
    monkeypatch.setenv("MEMD_TOKEN", "t0ken")
    monkeypatch.setenv("MEMD_LOCAL_HOST", "any")
    _seed_store(root, ALICE, "ALICE-SECRET")
    _seed_store(root, BOB, "BOB-SECRET")
    from memd.refresh import ensure_lexical

    for who in (ALICE, BOB):
        ensure_lexical(_cfg_for(who))
    return root


def _cfg_for(store: str) -> Config:
    import os

    env = dict(os.environ)
    env["MEMD_PROFILE"] = store
    return Config.from_env(env, env_file=None)


def _embed_response(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    texts = payload["input"]
    if isinstance(texts, str):
        texts = [texts]
    return httpx.Response(
        200, json={"data": [{"embedding": [float(len(t) % 7) + 1.0] * 768} for t in texts]}
    )


def _mock_models():
    respx.post(EMBED_URL).mock(side_effect=_embed_response)
    respx.post(RERANK_URL).mock(return_value=httpx.Response(503))  # fall back to BM25


# ---------------------------------------------------------------------------
# The core: a store handed another store's clone must refuse, not serve.
# ---------------------------------------------------------------------------


@respx.mock
def test_a_recall_for_alice_handed_bobs_clone_refuses(two_stores):
    _mock_models()
    cfg = dataclasses.replace(_cfg_for(BOB), profile=ALICE)
    with pytest.raises(CrossProfileViolation):
        core_recall("secret", profile=ALICE, k=5, cfg=cfg)


@respx.mock
def test_a_save_as_bob_into_alices_clone_writes_nothing(two_stores):
    _mock_models()
    cfg = dataclasses.replace(_cfg_for(ALICE), profile=BOB)
    before = sorted(p.name for p in (two_stores / ALICE / "clone").glob("*.md"))
    with pytest.raises(CrossProfileViolation):
        core_save({"title": "intrusion", "body": "should never land"}, profile=BOB, cfg=cfg)
    after = sorted(p.name for p in (two_stores / ALICE / "clone").glob("*.md"))
    assert before == after


@respx.mock
def test_each_store_serves_only_its_own_note(two_stores):
    _mock_models()
    alice = core_recall("secret", profile=ALICE, k=5, cfg=_cfg_for(ALICE))
    bob = core_recall("secret", profile=BOB, k=5, cfg=_cfg_for(BOB))
    alice_text = " ".join(n.to_dict().get("body", "") for n in alice)
    bob_text = " ".join(n.to_dict().get("body", "") for n in bob)
    assert "ALICE-SECRET" in alice_text
    assert "BOB-SECRET" not in alice_text
    assert "BOB-SECRET" in bob_text
    assert "ALICE-SECRET" not in bob_text


@respx.mock
def test_a_bound_caller_writing_normally_lands_in_their_own_store(two_stores):
    _mock_models()
    bind_identity(ALICE)
    from memd.profiles import resolve_profile

    store = resolve_profile(None)
    receipt = core_save(
        {"title": "alice note", "body": "a fact alice saved"},
        profile=store,
        cfg=_cfg_for(store),
    )
    data = receipt.to_dict()
    assert data.get("saved") is True
    # Assert on the receipt's own path: memd's on-disk filename is not the slug
    # (slug "alice-note" becomes "alice_note.md"), and the point of the test is
    # WHICH STORE the write landed in, not the filename convention.
    written = Path(data["path"])
    assert written.parent == two_stores / ALICE / "clone"
    assert written.exists()
    assert not list((two_stores / BOB / "clone").glob("alice*"))


# ---------------------------------------------------------------------------
# REST: no request field moves a bound caller off their own store.
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    # This fixture predates OIDC and selects a synthetic identity by binding it
    # in the test. Reapply that identity at the authentication boundary: real
    # authentication now clears ambient context before verifying each bearer.
    from memd.identity import current_identity
    from memd.server import _authenticate
    def fixture_auth(header):
        intended = current_identity()
        label = _authenticate(header)
        if label and intended:
            bind_identity(intended)
        return label
    monkeypatch.setattr('memd.server._authenticate', fixture_auth)
    return TestClient(app)


AUTH = {"Authorization": "Bearer t0ken"}


class TestRestRefusesCrossTenantRequestFields:
    def test_recall_naming_another_store_is_403(self, two_stores, client):
        bind_identity(ALICE)
        r = client.post("/recall", json={"query": "secret", "profile": BOB}, headers=AUTH)
        assert r.status_code == 403
        assert BOB not in r.text or "own store" in r.text

    def test_save_naming_another_store_is_403(self, two_stores, client):
        bind_identity(ALICE)
        r = client.post(
            "/save", json={"title": "x", "body": "y", "profile": BOB}, headers=AUTH
        )
        assert r.status_code == 403

    def test_read_naming_another_store_is_403(self, two_stores, client):
        bind_identity(ALICE)
        r = client.post(
            "/read", json={"slug": "bob-secret", "profile": BOB}, headers=AUTH
        )
        assert r.status_code == 403

    def test_reindex_naming_another_store_is_403(self, two_stores, client):
        bind_identity(ALICE)
        r = client.post("/reindex", json={"profile": BOB, "pull": False}, headers=AUTH)
        assert r.status_code == 403

    def test_stats_naming_another_store_is_403(self, two_stores, client):
        bind_identity(ALICE)
        r = client.get(f"/stats?profile={BOB}", headers=AUTH)
        assert r.status_code == 403

    def test_a_legacy_profile_name_is_403_for_a_bound_caller(self, two_stores, client):
        bind_identity(ALICE)
        r = client.post("/recall", json={"query": "x", "profile": "amber"}, headers=AUTH)
        assert r.status_code == 403

    def test_a_traversal_shaped_profile_is_refused(self, two_stores, client):
        bind_identity(ALICE)
        r = client.post(
            "/recall", json={"query": "x", "profile": f"../{BOB}"}, headers=AUTH
        )
        assert r.status_code in (400, 403)

    @respx.mock
    def test_an_unbound_caller_is_confined_by_the_instance_lock(
        self, two_stores, client, monkeypatch
    ):
        """No identity bound: a static token must not roam by request field."""
        monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "1")
        monkeypatch.setenv("MEMD_PROFILE", ALICE)
        r = client.post("/recall", json={"query": "secret", "profile": BOB}, headers=AUTH)
        assert r.status_code == 403
        assert "BOB-SECRET" not in r.text

    def test_an_unbound_traversal_is_a_bad_request_not_a_crash(self, two_stores, client):
        r = client.post("/recall", json={"query": "x", "profile": "../etc"}, headers=AUTH)
        assert r.status_code == 400

    @respx.mock
    def test_the_callers_own_store_is_accepted(self, two_stores, client):
        _mock_models()
        bind_identity(ALICE)
        r = client.post("/recall", json={"query": "secret", "profile": ALICE}, headers=AUTH)
        assert r.status_code == 200
        assert r.json()["profile"] == ALICE

    @respx.mock
    def test_omitting_the_field_serves_the_callers_own_store(self, two_stores, client):
        _mock_models()
        bind_identity(ALICE)
        r = client.post("/recall", json={"query": "secret"}, headers=AUTH)
        assert r.status_code == 200
        assert r.json()["profile"] == ALICE
        assert "BOB-SECRET" not in r.text


# ---------------------------------------------------------------------------
# MCP: the same, through the tool surface.
# ---------------------------------------------------------------------------


def _call(name, args):
    import asyncio

    return asyncio.run(mcp.call_tool(name, args))


class TestMcpRefusesCrossTenantRequestFields:
    def test_recall_naming_another_store_is_an_error(self, two_stores):
        bind_identity(ALICE)
        result = _call("recall", {"query": "secret", "profile": BOB})
        assert result.is_error
        assert "BOB-SECRET" not in json.dumps(result.structured_content)

    def test_save_naming_another_store_is_an_error(self, two_stores):
        bind_identity(ALICE)
        result = _call("save", {"title": "x", "body": "y", "profile": BOB})
        assert result.is_error
        assert not (two_stores / BOB / "clone" / "x.md").exists()

    def test_read_naming_another_store_is_an_error(self, two_stores):
        bind_identity(ALICE)
        result = _call("read", {"slug": "bob-secret", "profile": BOB})
        assert result.is_error

    def test_a_traversal_shaped_profile_is_an_error(self, two_stores):
        bind_identity(ALICE)
        result = _call("recall", {"query": "x", "profile": f"../{BOB}"})
        assert result.is_error

    @respx.mock
    def test_omitting_the_field_serves_the_callers_own_store(self, two_stores):
        _mock_models()
        bind_identity(ALICE)
        result = _call("recall", {"query": "secret"})
        assert not result.is_error
        assert result.structured_content["profile"] == ALICE
        assert "BOB-SECRET" not in json.dumps(result.structured_content)


class TestMcpToolSchema:
    """Stop advertising a field the caller is not allowed to use."""

    def test_profile_is_not_advertised_on_a_multi_tenant_instance(self, two_stores):
        import asyncio

        tools = asyncio.run(mcp.list_tools())
        for tool in tools:
            assert "profile" not in tool.input_schema["properties"], tool.name

    def test_profile_is_still_advertised_on_a_single_tenant_instance(self, monkeypatch):
        import asyncio

        monkeypatch.delenv("MEMD_STORES_ROOT", raising=False)
        tools = asyncio.run(mcp.list_tools())
        for tool in tools:
            assert "profile" in tool.input_schema["properties"], tool.name

    def test_dropping_the_field_does_not_disturb_the_rest_of_the_schema(self, two_stores):
        import asyncio

        tools = asyncio.run(mcp.list_tools())
        recall = next(t for t in tools if t.name == "recall")
        save = next(t for t in tools if t.name == "save")
        assert "query" in recall.input_schema["properties"]
        assert {"title", "body"}.issubset(set(save.input_schema["properties"]))
        for tool in tools:
            assert tool.input_schema["required"] == [], tool.name
