"""Where a client DISCOVERS the authorisation server is not what a token's `iss` says.

Found on the first real browser sign-in. authentik gives every OAuth2 provider
its own issuer by default, so a token minted for the `claude-code` client
carried `iss=.../application/o/claude-code/` while memd demanded
`.../application/o/memd/` and refused it. authentik issued a perfectly good
token and memd rejected it.

The fix on the authentik side is `issuer_mode: global`, so every agent
client issues under one issuer. That exposes a second problem here: with the
global issuer, `iss` is `https://auth.../` and **authentik serves no discovery
document at that root** (checked: 404 for both well-known paths), while
`.../application/o/memd/.well-known/openid-configuration` serves one and
reports the global issuer inside it.

So memd needs two settings where it had one:

  * the ISSUER it requires in a token's `iss`,
  * the AUTHORIZATION SERVER it advertises to clients, which must be a URL that
    actually serves discovery.

Conflating them means either tokens are refused or clients cannot discover
anything. The default keeps them equal, so a deployment where the issuer does
serve its own metadata needs no extra configuration.
"""
import pytest

from memd.oidc import (
    authorization_server,
    issuer,
    jwks_uri,
    protected_resource_metadata,
)

GLOBAL_ISS = "https://auth.example.invalid/"
APP_AS = "https://auth.example.invalid/application/o/memd/"


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setenv("MEMD_PUBLIC_URL", "https://memd.example.invalid")
    monkeypatch.delenv("MEMD_OIDC_AUTHORIZATION_SERVER", raising=False)
    monkeypatch.delenv("MEMD_OIDC_JWKS_URI", raising=False)


class TestDefaultsAreUnchanged:
    def test_the_advertised_server_defaults_to_the_issuer(self, monkeypatch):
        monkeypatch.setenv("MEMD_OIDC_ISSUER", APP_AS)
        assert authorization_server() == APP_AS
        assert protected_resource_metadata()["authorization_servers"] == [APP_AS]

    def test_the_jwks_still_derives_from_the_issuer_by_default(self, monkeypatch):
        monkeypatch.setenv("MEMD_OIDC_ISSUER", APP_AS)
        assert jwks_uri() == APP_AS.rstrip("/") + "/jwks/"


class TestTheSplit:
    def test_the_advertised_server_can_differ_from_the_issuer(self, monkeypatch):
        """The live shape: global issuer, per-application discovery."""
        monkeypatch.setenv("MEMD_OIDC_ISSUER", GLOBAL_ISS)
        monkeypatch.setenv("MEMD_OIDC_AUTHORIZATION_SERVER", APP_AS)
        assert issuer() == GLOBAL_ISS
        assert authorization_server() == APP_AS

    def test_clients_are_pointed_at_the_url_that_serves_discovery(self, monkeypatch):
        monkeypatch.setenv("MEMD_OIDC_ISSUER", GLOBAL_ISS)
        monkeypatch.setenv("MEMD_OIDC_AUTHORIZATION_SERVER", APP_AS)
        doc = protected_resource_metadata()
        assert doc["authorization_servers"] == [APP_AS], \
            "advertising the bare global issuer sends clients to a 404"

    def test_the_jwks_is_not_guessed_from_a_bare_global_issuer(self, monkeypatch):
        """`https://auth.../` + `jwks/` is not a real endpoint; it must be explicit."""
        monkeypatch.setenv("MEMD_OIDC_ISSUER", GLOBAL_ISS)
        monkeypatch.setenv("MEMD_OIDC_AUTHORIZATION_SERVER", APP_AS)
        monkeypatch.setenv("MEMD_OIDC_JWKS_URI", APP_AS + "jwks/")
        assert jwks_uri() == APP_AS + "jwks/"

    def test_the_jwks_falls_back_to_the_advertised_server_not_the_issuer(self, monkeypatch):
        """With no explicit JWKS, deriving from the issuer yields a 404 root.

        The authorisation server URL is the one that actually has a `jwks/`
        under it, so it is the better thing to derive from.
        """
        monkeypatch.setenv("MEMD_OIDC_ISSUER", GLOBAL_ISS)
        monkeypatch.setenv("MEMD_OIDC_AUTHORIZATION_SERVER", APP_AS)
        assert jwks_uri() == APP_AS.rstrip("/") + "/jwks/"

    def test_registration_endpoint_is_still_never_advertised(self, monkeypatch):
        monkeypatch.setenv("MEMD_OIDC_ISSUER", GLOBAL_ISS)
        monkeypatch.setenv("MEMD_OIDC_AUTHORIZATION_SERVER", APP_AS)
        assert "registration_endpoint" not in protected_resource_metadata()
