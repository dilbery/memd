"""MEMD_OIDC_ADVERTISED_SCOPES: what memd advertises, never what it requires.

The RFC 9728 document is how a client decides what to ask the authorisation
server for. authentik issues a refresh token only when `offline_access` was
asked for, so advertising one scope was half the reason every agent session
needed a browser after ten minutes.

What these tests are really guarding is the separation. The advertised list must
be able to grow without the check growing with it, and the required scope must
never be able to fall out of the advertised list, because a client that is never
told to ask for it can never get in.
"""
import pytest

from memd.oidc import (
    advertised_scopes,
    protected_resource_metadata,
    required_scope,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(
        "MEMD_OIDC_ISSUER", "https://auth.example.invalid/application/o/memd/"
    )
    monkeypatch.setenv("MEMD_PUBLIC_URL", "https://memd.example.invalid")
    monkeypatch.delenv("MEMD_OIDC_SCOPE", raising=False)
    monkeypatch.delenv("MEMD_OIDC_ADVERTISED_SCOPES", raising=False)
    yield


class TestTheDefault:
    def test_it_advertises_offline_access_as_well_as_memd(self):
        assert advertised_scopes() == ["memd", "offline_access"]

    def test_the_document_advertises_both(self):
        supported = protected_resource_metadata()["scopes_supported"]
        assert "memd" in supported
        assert "offline_access" in supported

    def test_the_required_scope_is_untouched(self):
        """The whole point: advertising more does not require more."""
        assert required_scope() == "memd"

    def test_the_default_follows_the_required_scope(self, monkeypatch):
        """Not a literal. A renamed required scope must stay advertised."""
        monkeypatch.setenv("MEMD_OIDC_SCOPE", "memd-next")
        assert advertised_scopes() == ["memd-next", "offline_access"]
        assert required_scope() == "memd-next"


class TestTheOverride:
    def test_an_explicit_list_is_honoured(self, monkeypatch):
        monkeypatch.setenv("MEMD_OIDC_ADVERTISED_SCOPES", "memd offline_access email")
        assert advertised_scopes() == ["memd", "offline_access", "email"]

    def test_the_required_scope_is_added_back_if_omitted(self, monkeypatch):
        """A list without the required scope tells clients to ask for the one
        thing that cannot authenticate them. It is repaired, not obeyed."""
        monkeypatch.setenv("MEMD_OIDC_ADVERTISED_SCOPES", "offline_access")
        assert advertised_scopes() == ["memd", "offline_access"]

    def test_duplicates_collapse(self, monkeypatch):
        monkeypatch.setenv(
            "MEMD_OIDC_ADVERTISED_SCOPES", "memd memd offline_access offline_access"
        )
        assert advertised_scopes() == ["memd", "offline_access"]

    def test_an_empty_value_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("MEMD_OIDC_ADVERTISED_SCOPES", "   ")
        assert advertised_scopes() == ["memd", "offline_access"]
