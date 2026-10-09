"""OIDC at the REST and MCP surfaces (Plan 2 piece 2, §9b).

Piece 1's cross-tenant tests bound an identity by hand because nothing could
authenticate. These are the same property with the hand removed: a real signed
bearer arrives over HTTP and the store follows it.

Also pinned here, because §9b item 5 depends on it: the static token file keeps
working for the sync job and admin REST. Those callers are UNBOUND, so the
instance lock still confines them (piece 1's review fix), and no person should
hold one on a multi-tenant deployment.
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

from memd.oidc import reset_jwks_cache  # noqa: E402
from memd.server import app  # noqa: E402

ISSUER = "https://auth.example.invalid/application/o/memd/"
JWKS_URI = "https://auth.example.invalid/application/o/memd/jwks/"
RESOURCE = "https://memd.example.invalid"
ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(key):
    from jwt.algorithms import RSAAlgorithm

    d = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    d.update(kid="k1", use="sig", alg="RS256")
    return {"keys": [d]}


def _tok(key, email=ALICE, scope="openid email memd", **over):
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": "s-1", "email": email, "scope": scope,
              "iat": now - 5, "exp": now + 600}
    claims.update(over)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def oidc(tmp_path, monkeypatch, signing_key):
    root = tmp_path / "stores"
    root.mkdir()
    monkeypatch.setenv("MEMD_STORES_ROOT", str(root))
    monkeypatch.setenv("MEMD_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("MEMD_OIDC_JWKS_URI", JWKS_URI)
    monkeypatch.setenv("MEMD_PUBLIC_URL", RESOURCE)
    monkeypatch.setenv("MEMD_TOKEN", "static-admin-token")
    monkeypatch.setenv("MEMD_REQUIRE_RECALL_TOKEN", "1")
    monkeypatch.delenv("MEMD_CLONE", raising=False)
    monkeypatch.delenv("MEMD_DB", raising=False)
    reset_jwks_cache()
    clear_identity()
    yield root
    reset_jwks_cache()
    clear_identity()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _stub_core(monkeypatch):
    """Only the store selection is under test here, not recall itself."""
    from types import SimpleNamespace

    monkeypatch.setattr("memd.server._core_recall",
                        lambda q, profile="amber", k=8, **kw: [])
    monkeypatch.setattr("memd.server._core_save",
                        lambda fact, profile="amber": SimpleNamespace(ok=True, profile=profile))


class TestProtectedResourceRoute:
    def test_the_metadata_is_served_without_a_token(self, oidc, client):
        """A client that cannot authenticate yet has to be able to read this."""
        r = client.get("/.well-known/oauth-protected-resource")
        assert r.status_code == 200
        assert r.json()["authorization_servers"] == [ISSUER]

    def test_it_is_absent_when_oidc_is_off(self, client, monkeypatch):
        monkeypatch.delenv("MEMD_OIDC_ISSUER", raising=False)
        assert client.get("/.well-known/oauth-protected-resource").status_code == 404


class TestUnauthenticatedGetsDiscoverable401:
    def test_a_401_points_at_the_resource_metadata(self, oidc, client):
        """Spec MUST. Without this header the client has nowhere to go."""
        r = client.post("/save", json={"title": "x", "body": "y"})
        assert r.status_code == 401
        wa = r.headers.get("www-authenticate", "")
        assert wa.startswith("Bearer")
        assert "resource_metadata=" in wa
        assert "/.well-known/oauth-protected-resource" in wa


class TestARealBearerSelectsTheStore:
    @respx.mock
    def test_recall_serves_the_token_holders_store(self, oidc, client, signing_key):
        respx.get(JWKS_URI).mock(return_value=httpx.Response(200, json=_jwks(signing_key)))
        r = client.post("/recall", json={"query": "x"},
                        headers={"Authorization": "Bearer " + _tok(signing_key)})
        assert r.status_code == 200
        assert r.json()["profile"] == ALICE

    @respx.mock
    def test_a_second_user_gets_their_own_store(self, oidc, client, signing_key):
        respx.get(JWKS_URI).mock(return_value=httpx.Response(200, json=_jwks(signing_key)))
        r = client.post("/recall", json={"query": "x"},
                        headers={"Authorization": "Bearer " + _tok(signing_key, email=BOB)})
        assert r.json()["profile"] == BOB

    @respx.mock
    def test_the_profile_field_cannot_move_a_token_holder(self, oidc, client, signing_key):
        """Piece 1's property, now driven by a real token rather than a fixture."""
        respx.get(JWKS_URI).mock(return_value=httpx.Response(200, json=_jwks(signing_key)))
        r = client.post("/recall", json={"query": "x", "profile": BOB},
                        headers={"Authorization": "Bearer " + _tok(signing_key)})
        assert r.status_code == 403

    @respx.mock
    def test_a_token_without_the_memd_scope_is_401(self, oidc, client, signing_key):
        respx.get(JWKS_URI).mock(return_value=httpx.Response(200, json=_jwks(signing_key)))
        r = client.post("/recall", json={"query": "x"},
                        headers={"Authorization": "Bearer " + _tok(signing_key, scope="openid email")})
        assert r.status_code == 401

    @respx.mock
    def test_an_expired_token_is_401(self, oidc, client, signing_key):
        respx.get(JWKS_URI).mock(return_value=httpx.Response(200, json=_jwks(signing_key)))
        now = int(time.time())
        r = client.post("/recall", json={"query": "x"},
                        headers={"Authorization": "Bearer " + _tok(signing_key, exp=now - 600)})
        assert r.status_code == 401

    @respx.mock
    def test_identity_does_not_leak_between_requests(self, oidc, client, signing_key):
        """A contextvar left set would serve the previous caller's store."""
        respx.get(JWKS_URI).mock(return_value=httpx.Response(200, json=_jwks(signing_key)))
        assert client.post("/recall", json={"query": "x"},
                           headers={"Authorization": "Bearer " + _tok(signing_key)}
                           ).json()["profile"] == ALICE
        assert client.post("/recall", json={"query": "x"},
                           headers={"Authorization": "Bearer " + _tok(signing_key, email=BOB)}
                           ).json()["profile"] == BOB
        # And an unauthenticated call afterwards must not inherit either.
        assert client.post("/recall", json={"query": "x"}).status_code == 401


class TestStaticTokensStillWork:
    """§9b item 5: the sync job and admin REST keep the token file."""

    @respx.mock
    def test_the_static_token_is_still_accepted(self, oidc, client, signing_key):
        """A static caller must NAME the store it means.

        It is unbound, so there is no identity to infer one from. On a
        multi-tenant instance that is the honest shape: the sync job always
        knows whose store it is initialising.
        """
        respx.get(JWKS_URI).mock(return_value=httpx.Response(200, json=_jwks(signing_key)))
        r = client.post("/recall", json={"query": "x", "profile": ALICE},
                        headers={"Authorization": "Bearer static-admin-token"})
        assert r.status_code == 200
        assert r.json()["profile"] == ALICE

    def test_an_unbound_caller_naming_no_store_is_refused_not_guessed(self, oidc, client):
        """No identity, no `profile`, no MEMD_PROFILE: there is no right answer.

        Refusing beats defaulting. A default here would silently serve one
        person's memory to whatever service happened to call without arguments.
        """
        r = client.post("/recall", json={"query": "x"},
                        headers={"Authorization": "Bearer static-admin-token"})
        assert r.status_code == 400

    @respx.mock
    def test_a_static_token_binds_no_identity_so_it_may_name_a_store(
        self, oidc, client, signing_key, monkeypatch
    ):
        """The onboarding job has to initialise an arbitrary user's store.

        It is unbound, so piece 1's rules apply: on an UNLOCKED instance it may
        name any valid store.
        """
        monkeypatch.delenv("MEMD_ENFORCE_PROFILE", raising=False)
        respx.get(JWKS_URI).mock(return_value=httpx.Response(200, json=_jwks(signing_key)))
        r = client.post("/recall", json={"query": "x", "profile": "newstarter@x.com"},
                        headers={"Authorization": "Bearer static-admin-token"})
        assert r.status_code == 200
        assert r.json()["profile"] == "newstarter@x.com"

    def test_validating_a_static_token_never_touches_the_jwks(self, oidc, client):
        """No respx mock here at all: the netguard fails the test if it tries.

        The sync job runs every five minutes; making it fetch a JWKS each time
        would be pointless load on authentik and a needless failure mode.
        """
        r = client.post("/recall", json={"query": "x", "profile": ALICE},
                        headers={"Authorization": "Bearer static-admin-token"})
        assert r.status_code == 200

    @respx.mock
    def test_a_bad_token_is_still_refused(self, oidc, client, signing_key):
        respx.get(JWKS_URI).mock(return_value=httpx.Response(200, json=_jwks(signing_key)))
        r = client.post("/recall", json={"query": "x"},
                        headers={"Authorization": "Bearer neither-a-jwt-nor-known"})
        assert r.status_code == 401


class TestMcpTransport:
    @respx.mock
    def test_the_mcp_asgi_app_binds_the_token_holder(self, oidc, signing_key):
        import asyncio

        from memd.identity import current_identity
        from memd.mcp_http import build_mcp_app

        respx.get(JWKS_URI).mock(return_value=httpx.Response(200, json=_jwks(signing_key)))
        asgi, _ = build_mcp_app()
        seen = {}

        async def drive():
            scope = {"type": "http", "method": "POST", "path": "/mcp",
                     "headers": [(b"authorization",
                                  ("Bearer " + _tok(signing_key)).encode())]}

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(msg):
                seen.setdefault("status", msg.get("status"))
                seen.setdefault("identity", current_identity())

            try:
                await asgi(scope, receive, send)
            except Exception:
                seen.setdefault("identity", current_identity())

        asyncio.run(drive())
        assert seen.get("identity") == ALICE

    @respx.mock
    def test_the_mcp_asgi_app_401s_with_the_discovery_header(self, oidc, signing_key):
        import asyncio

        from memd.mcp_http import build_mcp_app

        respx.get(JWKS_URI).mock(return_value=httpx.Response(200, json=_jwks(signing_key)))
        asgi, _ = build_mcp_app()
        out = {}

        async def drive():
            scope = {"type": "http", "method": "POST", "path": "/mcp",
                     "headers": [(b"authorization", b"Bearer rubbish")]}

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(msg):
                if msg.get("type") == "http.response.start":
                    out["status"] = msg["status"]
                    out["headers"] = {k.decode().lower(): v.decode()
                                      for k, v in msg.get("headers", [])}

            await asgi(scope, receive, send)

        asyncio.run(drive())
        assert out["status"] == 401
        assert "resource_metadata=" in out["headers"].get("www-authenticate", "")
