"""Identity-to-store selection (Plan 2, §9a): the store follows the caller.

The property Plan 2 exists to provide: once a request carries an authenticated
identity, the store it reads and writes is that identity's store, and NO
request field can change it. `profile` on the tools becomes at best a
redundant restatement of the caller's own store and at worst a refusal.

`bind_identity` is a SEAM, not an identity source. Piece 1 ships no way for a
caller to set it; the OIDC resource-server layer (§9b) is what will call it,
with the validated `email` claim. That is why these tests bind it directly:
they pin the substrate's behaviour independently of how identity arrives.

Deliberately NOT tested here, because it must never be built: a mapping from a
static token label to a store. Labels stay client attribution (memd/actor.py).
Label-to-store is Option A in §9a, marked "fallback only", and building it
would be a trap.
"""
import pytest

from memd.identity import bind_identity, clear_identity, current_identity
from memd.profiles import ProfileMismatch, UnknownProfile, resolve_profile
from memd.stores import InvalidStoreName


@pytest.fixture
def multitenant(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMD_STORES_ROOT", str(tmp_path / "stores"))
    yield tmp_path / "stores"
    clear_identity()


@pytest.fixture(autouse=True)
def _no_identity_leaks_between_tests():
    clear_identity()
    yield
    clear_identity()


class TestBindIdentity:
    def test_nothing_is_bound_by_default(self):
        assert current_identity() is None

    def test_binding_normalises_the_identity_to_a_store_name(self, multitenant):
        bind_identity("Alice@Corp.Example.Com")
        assert current_identity() == "alice@corp.example.com"

    def test_binding_a_malformed_identity_is_refused_not_sanitised(self, multitenant):
        with pytest.raises(InvalidStoreName):
            bind_identity("../amber")
        # A refused bind must leave nothing behind for the next call to inherit.
        assert current_identity() is None

    def test_clear_identity_unbinds(self, multitenant):
        bind_identity("a@x.com")
        clear_identity()
        assert current_identity() is None


class TestBoundIdentitySelectsTheStore:
    def test_omitted_profile_resolves_to_the_callers_own_store(self, multitenant):
        bind_identity("a@x.com")
        assert resolve_profile(None) == "a@x.com"

    def test_a_profile_equal_to_the_callers_own_store_is_allowed(self, multitenant):
        bind_identity("a@x.com")
        assert resolve_profile("a@x.com") == "a@x.com"

    def test_a_differently_cased_profile_still_matches_the_caller(self, multitenant):
        bind_identity("a@x.com")
        assert resolve_profile("A@X.COM") == "a@x.com"

    def test_another_users_store_is_refused(self, multitenant):
        bind_identity("a@x.com")
        with pytest.raises(ProfileMismatch):
            resolve_profile("b@x.com")

    def test_a_legacy_profile_name_is_refused_for_a_bound_caller(self, multitenant):
        """amber is a store like any other here; a bound caller cannot hop to it."""
        bind_identity("a@x.com")
        with pytest.raises(ProfileMismatch):
            resolve_profile("amber")

    def test_a_malformed_profile_field_is_refused_not_ignored(self, multitenant):
        bind_identity("a@x.com")
        with pytest.raises((ProfileMismatch, InvalidStoreName)):
            resolve_profile("../a@x.com")

    def test_the_instance_lock_cannot_override_a_bound_identity(self, multitenant, monkeypatch):
        """MEMD_ENFORCE_PROFILE is a single-tenant concept.

        A single-store deployment sets it. If the lock still won when a store root is
        configured, every authenticated user would be served the locked store,
        which is the exact opposite of the property being built.
        """
        monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "1")
        monkeypatch.setenv("MEMD_PROFILE", "amber")
        bind_identity("a@x.com")
        assert resolve_profile(None) == "a@x.com"
        with pytest.raises(ProfileMismatch):
            resolve_profile("amber")


class TestUnboundCallers:
    """The sync job, admin REST and the CLI have no OIDC identity."""

    def test_an_unbound_caller_may_name_any_valid_store(self, multitenant):
        # Piece 3's onboarding job needs this to initialise a new user's store.
        assert resolve_profile("newstarter@x.com") == "newstarter@x.com"

    def test_an_unbound_caller_is_refused_a_malformed_store_as_unknown(self, multitenant):
        """UnknownProfile specifically, not a bare ValueError.

        memd/server.py maps UnknownProfile to 400 and ProfileMismatch to 403,
        and catches nothing else. An InvalidStoreName escaping here becomes a
        500 with a traceback for what is plainly a bad request.
        """
        with pytest.raises(UnknownProfile):
            resolve_profile("../../etc")

    def test_an_unbound_caller_falls_back_to_the_configured_profile(self, multitenant, monkeypatch):
        monkeypatch.setenv("MEMD_PROFILE", "amber")
        assert resolve_profile(None) == "amber"

    def test_the_instance_lock_still_confines_an_unbound_caller(self, multitenant, monkeypatch):
        """A static token must not be able to roam once a stores root is set.

        A single-store deployment may run MEMD_ENFORCE_PROFILE=1 with per-person
        static tokens active. Those callers are UNBOUND: they carry a token,
        not an identity. If the lock only supplied a default and stopped
        applying the moment a `profile` field was present, then flipping
        MEMD_STORES_ROOT on that container would let any static token address
        any store by request field -- cross-tenant access by an authenticated
        caller, in a configuration that exists in practice.
        """
        monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "1")
        monkeypatch.setenv("MEMD_PROFILE", "amber")
        assert resolve_profile(None) == "amber"
        assert resolve_profile("amber") == "amber"
        with pytest.raises(ProfileMismatch):
            resolve_profile("b@x.com")

    def test_an_unlocked_multitenant_instance_still_lets_an_admin_name_a_store(
        self, multitenant, monkeypatch
    ):
        """Piece 3's onboarding job needs this; it runs without the lock."""
        monkeypatch.delenv("MEMD_ENFORCE_PROFILE", raising=False)
        monkeypatch.setenv("MEMD_PROFILE", "amber")
        assert resolve_profile("newstarter@x.com") == "newstarter@x.com"


class TestSingleTenantIsUnchanged:
    """No MEMD_STORES_ROOT means the shipped behaviour, byte for byte."""

    def test_unknown_profile_still_raises_without_a_store_root(self, monkeypatch):
        monkeypatch.delenv("MEMD_STORES_ROOT", raising=False)
        with pytest.raises(UnknownProfile):
            resolve_profile("a@x.com")

    def test_the_lock_still_wins_without_a_store_root(self, monkeypatch):
        monkeypatch.delenv("MEMD_STORES_ROOT", raising=False)
        monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "1")
        monkeypatch.setenv("MEMD_PROFILE", "cobalt")
        assert resolve_profile(None) == "cobalt"
        assert resolve_profile("cobalt") == "cobalt"
        with pytest.raises(ProfileMismatch):
            resolve_profile("amber")
