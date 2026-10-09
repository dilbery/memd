"""OIDC resource-server layer (Plan 2 piece 2, §9b).

memd becomes an OAuth 2.1 resource server. authentik is the authorisation
server, the agent is the client, and every request carries a short-lived
bearer. This module is the FIRST AND ONLY production caller of
`identity.bind_identity`, which is what turns piece 1's substrate into a
working per-user memory.

What is being pinned here, in the order an attacker meets it:

  * a token that is not signed by authentik's key is refused,
  * a token that is signed but expired, not yet valid, or from another issuer
    is refused,
  * a token minted for a DIFFERENT application is refused, via the custom
    `memd` scope, because with per-client registration `aud` alone cannot be
    the check (§9b item 3),
  * a token with no usable `email` claim is refused rather than falling back to
    some default store,
  * and only then does the caller's own store get bound.

Signature verification is exercised against REAL RSA keys generated in the
fixture, not a mocked verifier: "the token was rejected" has to mean the
cryptography rejected it, not that a stub said so.
"""
import json
import time

import httpx
import pytest
import respx

from memd.identity import clear_identity, current_identity

pytest.importorskip("jwt", reason="PyJWT is required for the OIDC layer")
import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from memd.oidc import (  # noqa: E402
    OidcError,
    bearer_identity,
    oidc_enabled,
    protected_resource_metadata,
    reset_jwks_cache,
)

ISSUER = "https://auth.example.invalid/application/o/memd/"
JWKS_URI = "https://auth.example.invalid/application/o/memd/jwks/"
RESOURCE = "https://memd.example.invalid"


def _key(kid: str):
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return kid, k


@pytest.fixture(scope="module")
def keys():
    """Two keys: the real signing key, and one authentik never published."""
    return {"good": _key("good-kid"), "rogue": _key("rogue-kid")}


def _jwk(kid, key):
    from jwt.algorithms import RSAAlgorithm

    d = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    d.update(kid=kid, use="sig", alg="RS256")
    return d


@pytest.fixture(autouse=True)
def _oidc_env(monkeypatch, keys):
    monkeypatch.setenv("MEMD_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("MEMD_OIDC_JWKS_URI", JWKS_URI)
    monkeypatch.setenv("MEMD_PUBLIC_URL", RESOURCE)
    monkeypatch.setenv("MEMD_OIDC_SCOPE", "memd")
    reset_jwks_cache()
    clear_identity()
    yield
    reset_jwks_cache()
    clear_identity()


def _serve_jwks(keys, which=("good",)):
    doc = {"keys": [_jwk(*keys[n]) for n in which]}
    return respx.get(JWKS_URI).mock(return_value=httpx.Response(200, json=doc))


def _token(keys, which="good", **over):
    kid, key = keys[which]
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "ak-uuid-1",
        "email": "Alice@Example.Invalid",
        "scope": "openid email profile memd",
        "iat": now - 5,
        "exp": now + 600,
        "aud": "some-dynamically-registered-client",
    }
    claims.update(over)
    for k in [k for k, v in claims.items() if v is None]:
        del claims[k]
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


class TestEnablement:
    def test_oidc_is_off_when_no_issuer_is_configured(self, monkeypatch):
        monkeypatch.delenv("MEMD_OIDC_ISSUER", raising=False)
        assert oidc_enabled() is False

    def test_oidc_is_on_when_an_issuer_is_configured(self):
        assert oidc_enabled() is True


class TestProtectedResourceMetadata:
    """RFC 9728, required by the MCP authorisation spec."""

    def test_it_names_the_authorisation_server(self):
        doc = protected_resource_metadata()
        assert doc["authorization_servers"] == [ISSUER]

    def test_it_identifies_this_resource(self):
        assert protected_resource_metadata()["resource"] == RESOURCE

    def test_it_advertises_the_custom_scope(self):
        assert "memd" in protected_resource_metadata()["scopes_supported"]

    def test_it_never_advertises_a_registration_endpoint(self):
        """§9b: Claude Code fails validation on a null one and attempts DCR on a
        present one. authentik does not advertise DCR either (verified against
        a live server), so the key must simply be absent."""
        assert "registration_endpoint" not in protected_resource_metadata()


class TestSignatureIsReallyChecked:
    @respx.mock
    def test_a_valid_token_yields_the_lower_cased_email(self, keys):
        _serve_jwks(keys)
        assert bearer_identity("Bearer " + _token(keys)) == "alice@example.invalid"

    @respx.mock
    def test_a_token_signed_by_an_unpublished_key_is_refused(self, keys):
        """The rogue key is never in the JWKS, so this must fail on signature."""
        _serve_jwks(keys, which=("good",))
        with pytest.raises(OidcError):
            bearer_identity("Bearer " + _token(keys, which="rogue"))

    @respx.mock
    def test_a_tampered_payload_is_refused(self, keys):
        _serve_jwks(keys)
        tok = _token(keys)
        head, payload, sig = tok.split(".")
        import base64

        body = json.loads(base64.urlsafe_b64decode(payload + "=="))
        body["email"] = "bob@example.invalid"
        forged = base64.urlsafe_b64encode(
            json.dumps(body).encode()).decode().rstrip("=")
        with pytest.raises(OidcError):
            bearer_identity(f"Bearer {head}.{forged}.{sig}")

    @respx.mock
    def test_an_unsigned_alg_none_token_is_refused(self, keys):
        """The classic bypass: swap the algorithm for 'none'."""
        _serve_jwks(keys)
        tok = jwt.encode({"iss": ISSUER, "email": "a@b.com", "scope": "memd"},
                         key="", algorithm="none")
        with pytest.raises(OidcError):
            bearer_identity("Bearer " + tok)


class TestClaimsAreChecked:
    @respx.mock
    def test_an_expired_token_is_refused(self, keys):
        _serve_jwks(keys)
        now = int(time.time())
        with pytest.raises(OidcError):
            bearer_identity("Bearer " + _token(keys, exp=now - 600, iat=now - 1200))

    @respx.mock
    def test_a_token_expired_within_the_clock_skew_leeway_is_still_accepted(self, keys):
        """Documents the leeway rather than leaving it implicit.

        A few seconds of allowance is for NTP skew between authentik and memd.
        It is deliberately small: a generous leeway quietly extends the life of
        a session that has been revoked.
        """
        from memd.oidc import CLOCK_SKEW_LEEWAY_SECONDS

        _serve_jwks(keys)
        now = int(time.time())
        just_expired = now - max(1, CLOCK_SKEW_LEEWAY_SECONDS // 2)
        assert bearer_identity(
            "Bearer " + _token(keys, exp=just_expired, iat=now - 600)
        ) == "alice@example.invalid"

    @respx.mock
    def test_the_leeway_is_not_generous(self, keys):
        from memd.oidc import CLOCK_SKEW_LEEWAY_SECONDS

        assert CLOCK_SKEW_LEEWAY_SECONDS <= 30

    @respx.mock
    def test_a_token_from_another_issuer_is_refused(self, keys):
        _serve_jwks(keys)
        with pytest.raises(OidcError):
            bearer_identity("Bearer " + _token(keys, iss="https://evil.invalid/"))

    @respx.mock
    def test_a_token_without_the_memd_scope_is_refused(self, keys):
        """§9b item 3: the practical audience check.

        A token authentik minted for any other application is signed by the same
        key and carries the same issuer. The custom scope is what separates
        them, because with per-client registration every client has its own
        client_id so `aud` cannot be the check.
        """
        _serve_jwks(keys)
        with pytest.raises(OidcError):
            bearer_identity("Bearer " + _token(keys, scope="openid email profile"))

    @respx.mock
    def test_a_scope_that_merely_contains_memd_as_a_substring_is_refused(self, keys):
        _serve_jwks(keys)
        with pytest.raises(OidcError):
            bearer_identity("Bearer " + _token(keys, scope="openid memdxyz"))

    @respx.mock
    def test_a_token_with_no_email_is_refused(self, keys):
        _serve_jwks(keys)
        with pytest.raises(OidcError):
            bearer_identity("Bearer " + _token(keys, email=None))

    @respx.mock
    def test_an_email_that_is_not_a_usable_store_name_is_refused(self, keys):
        _serve_jwks(keys)
        with pytest.raises(OidcError):
            bearer_identity("Bearer " + _token(keys, email="../etc/passwd"))

    @respx.mock
    def test_a_missing_or_malformed_header_is_refused(self, keys):
        _serve_jwks(keys)
        for bad in (None, "", "Basic abc", "Bearer", "Bearer   ", "token abc"):
            with pytest.raises(OidcError):
                bearer_identity(bad)


class TestJwksCaching:
    @respx.mock
    def test_the_jwks_is_fetched_once_and_reused(self, keys):
        route = _serve_jwks(keys)
        for _ in range(5):
            bearer_identity("Bearer " + _token(keys))
        assert route.call_count == 1

    @respx.mock
    def test_an_unknown_kid_triggers_exactly_one_refetch(self, keys):
        """Key rotation: authentik starts signing with a kid we have not seen.

        Without a refetch every request fails until the process restarts. With
        an unconditional refetch, a garbage kid becomes a denial-of-service
        amplifier against authentik, so it must refetch at most once.
        """
        route = respx.get(JWKS_URI).mock(side_effect=[
            httpx.Response(200, json={"keys": [_jwk(*keys["good"])]}),
            httpx.Response(200, json={"keys": [_jwk(*keys["good"]),
                                               _jwk(*keys["rogue"])]}),
        ])
        bearer_identity("Bearer " + _token(keys))          # caches
        assert route.call_count == 1
        assert bearer_identity("Bearer " + _token(keys, which="rogue")) \
            == "alice@example.invalid"                      # refetch finds it
        assert route.call_count == 2

    @respx.mock
    def test_the_first_refetch_is_allowed_on_a_freshly_booted_host(self, keys):
        """`_last_refetch` must be a None sentinel, not 0.0.

        time.monotonic() counts from an arbitrary origin (uptime on Linux), so
        with 0.0 meaning "never" the cooldown reads as "refetched at boot" and
        the first rotation inside the first minute of uptime is refused. This
        showed up as a flaky test before it could show up as a real outage.
        """
        import memd.oidc as oidc_mod

        assert oidc_mod._last_refetch is None
        route = respx.get(JWKS_URI).mock(side_effect=[
            httpx.Response(200, json={"keys": [_jwk(*keys["good"])]}),
            httpx.Response(200, json={"keys": [_jwk(*keys["good"]),
                                               _jwk(*keys["rogue"])]}),
        ])
        bearer_identity("Bearer " + _token(keys))
        # Pretend the process started moments ago; the refetch must still happen.
        oidc_mod._last_refetch = None
        assert bearer_identity("Bearer " + _token(keys, which="rogue"))             == "alice@example.invalid"
        assert route.call_count == 2

    @respx.mock
    def test_a_repeatedly_unknown_kid_does_not_refetch_every_time(self, keys):
        route = _serve_jwks(keys, which=("good",))
        for _ in range(4):
            with pytest.raises(OidcError):
                bearer_identity("Bearer " + _token(keys, which="rogue"))
        assert route.call_count <= 2, "unknown kid must not hammer the JWKS"

    @respx.mock
    def test_an_unreachable_jwks_refuses_rather_than_admitting(self, keys):
        respx.get(JWKS_URI).mock(return_value=httpx.Response(503))
        with pytest.raises(OidcError):
            bearer_identity("Bearer " + _token(keys))


class TestIdentityIsBoundNotJustReturned:
    @respx.mock
    def test_nothing_is_bound_by_a_failed_validation(self, keys):
        _serve_jwks(keys)
        with pytest.raises(OidcError):
            bearer_identity("Bearer " + _token(keys, scope="openid"))
        assert current_identity() is None
