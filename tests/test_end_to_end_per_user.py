"""The whole thing, end to end: a bearer arrives and a new user gets memory.

This is the acceptance test for Plan 2. Nothing is stubbed except the model
endpoints and authentik's JWKS. In particular the recall/save CORE is real, the
git repositories are real, and no store exists on disk when the test starts.

    A user who has never used memd before presents a token,
    saves a fact, recalls it, and cannot see anyone else's.

If this passes, pieces 1, 2 and 3 join up.
"""
import json
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from memd.identity import clear_identity

pytest.importorskip("jwt")
import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from memd.config import DEFAULT_EMBED_URL, DEFAULT_RERANK_PATH, DEFAULT_RERANK_URL  # noqa: E402
from memd.oidc import reset_jwks_cache  # noqa: E402
from memd.server import app  # noqa: E402

ISSUER = "https://auth.example.invalid/application/o/memd/"
JWKS_URI = "https://auth.example.invalid/application/o/memd/jwks/"
EMBED_URL = f"{DEFAULT_EMBED_URL}/v1/embeddings"
RERANK_URL = f"{DEFAULT_RERANK_URL}{DEFAULT_RERANK_PATH}"
ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(k):
    from jwt.algorithms import RSAAlgorithm

    d = json.loads(RSAAlgorithm.to_jwk(k.public_key()))
    d.update(kid="k1", use="sig", alg="RS256")
    return {"keys": [d]}


def _tok(k, email):
    now = int(time.time())
    return jwt.encode(
        {"iss": ISSUER, "sub": "s-" + email, "email": email,
         "scope": "openid email memd", "iat": now - 5, "exp": now + 600},
        k, algorithm="RS256", headers={"kid": "k1"})


def _mock_backends(k):
    respx.get(JWKS_URI).mock(return_value=httpx.Response(200, json=_jwks(k)))
    respx.post(EMBED_URL).mock(
        side_effect=lambda r: httpx.Response(200, json={"data": [
            {"embedding": [1.0] * 768}
            for _ in ([json.loads(r.content)["input"]]
                      if isinstance(json.loads(r.content)["input"], str)
                      else json.loads(r.content)["input"])]}))
    respx.post(RERANK_URL).mock(return_value=httpx.Response(503))


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A memd configured exactly as the per-user deployment will be.

    Note what is NOT set: MEMD_ENFORCE_PROFILE. The multi-tenant deployment
    runs unlocked so the onboarding job can address any store. And no store
    exists on disk.
    """
    root = tmp_path / "stores"
    root.mkdir()
    monkeypatch.setenv("MEMD_STORES_ROOT", str(root))
    monkeypatch.setenv("MEMD_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("MEMD_OIDC_JWKS_URI", JWKS_URI)
    monkeypatch.setenv("MEMD_PUBLIC_URL", "https://memd.example.invalid")
    monkeypatch.setenv("MEMD_REQUIRE_RECALL_TOKEN", "1")
    monkeypatch.setenv("MEMD_LOCAL_HOST", "any")
    monkeypatch.setenv("MEMD_GIT_AUTHOR_NAME", "memd service")
    monkeypatch.setenv("MEMD_GIT_AUTHOR_EMAIL", "memd@example.invalid")
    monkeypatch.delenv("MEMD_CLONE", raising=False)
    monkeypatch.delenv("MEMD_DB", raising=False)
    monkeypatch.delenv("MEMD_ENFORCE_PROFILE", raising=False)
    monkeypatch.delenv("MEMD_STORES_REPO_TEMPLATE", raising=False)
    reset_jwks_cache()
    clear_identity()
    yield root
    reset_jwks_cache()
    clear_identity()


@pytest.fixture
def client():
    return TestClient(app)


@respx.mock
def test_a_brand_new_user_saves_and_recalls_their_own_memory(hub, client, key):
    _mock_backends(key)
    alice = {"Authorization": "Bearer " + _tok(key, ALICE)}

    assert not (hub / ALICE).exists(), "precondition: Alice has no store"

    saved = client.post(
        "/save",
        json={"title": "qos dscp", "body": "The backup plane uses DSCP 26 for bulk traffic and 48 for control."},
        headers=alice,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json().get("saved") is True

    # The store was created on demand, in the right place, as a real repository.
    assert (hub / ALICE / "clone" / ".git" / "HEAD").exists()

    got = client.post("/recall", json={"query": "DSCP"}, headers=alice)
    assert got.status_code == 200
    assert got.json()["profile"] == ALICE
    assert "DSCP 26" in got.text


@respx.mock
def test_a_second_user_cannot_see_the_first_users_note(hub, client, key):
    _mock_backends(key)
    alice = {"Authorization": "Bearer " + _tok(key, ALICE)}
    bob = {"Authorization": "Bearer " + _tok(key, BOB)}

    client.post("/save", json={"title": "alice secret",
                               "body": "ALICE-ONLY-MARKER lives here"}, headers=alice)

    got = client.post("/recall", json={"query": "ALICE-ONLY-MARKER"}, headers=bob)
    assert got.status_code == 200
    assert got.json()["profile"] == BOB
    assert "ALICE-ONLY-MARKER" not in got.text

    # And Bob's store is a separate repository, created for him alone.
    assert (hub / BOB / "clone" / ".git" / "HEAD").exists()
    assert not list((hub / BOB / "clone").glob("*alice*"))


@respx.mock
def test_bob_cannot_read_alices_note_by_naming_her_store(hub, client, key):
    _mock_backends(key)
    alice = {"Authorization": "Bearer " + _tok(key, ALICE)}
    bob = {"Authorization": "Bearer " + _tok(key, BOB)}

    client.post("/save", json={"title": "alice secret",
                               "body": "ALICE-ONLY-MARKER lives here"}, headers=alice)

    for payload in ({"query": "x", "profile": ALICE},
                    {"query": "x", "profile": ALICE.upper() + " "},
                    {"query": "x", "profile": "../" + ALICE}):
        r = client.post("/recall", json=payload, headers=bob)
        assert r.status_code in (400, 403), payload
        assert "ALICE-ONLY-MARKER" not in r.text


@respx.mock
def test_an_unauthenticated_caller_creates_no_store_at_all(hub, client, key):
    _mock_backends(key)
    before = sorted(p.name for p in hub.iterdir())
    r = client.post("/save", json={"title": "x", "body": "y"})
    assert r.status_code == 401
    assert sorted(p.name for p in hub.iterdir()) == before
