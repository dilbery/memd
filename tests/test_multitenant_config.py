"""Config must not hand a per-user store somebody else's clone.

This is not a request-field attack, so it sits outside the request-field
isolation property, but it is the likeliest way to get a cross-tenant leak in
practice, because a typical single-tenant deployment sets exactly the variables
involved. From such a compose file:

    MEMD_AMBER_CLONE: /data/clone
    MEMD_AMBER_DB:    /data/memd.db
    MEMD_CLONE:       /data/clone
    MEMD_DB:          /data/memd.db

`Config.from_env` does `clone = merged.get("MEMD_CLONE") or p_clone`, so the
global override wins over the per-profile path. On a single-tenant instance
both name the same directory and it is harmless. Turn on MEMD_STORES_ROOT
without removing those two lines and every identity resolves to /data/clone:
one shared store wearing per-user names, silently, with the path guard content
because the override was handed to it as an allowed root.

So: when a stores root is configured, the global single-store overrides are not
consulted at all.
"""
import pytest

from memd.config import Config
from memd.identity import bind_identity, clear_identity
from memd.profiles import CrossProfileViolation, guard_paths


@pytest.fixture(autouse=True)
def _clean_identity():
    clear_identity()
    yield
    clear_identity()


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "stores"
    r.mkdir()
    monkeypatch.setenv("MEMD_STORES_ROOT", str(r))
    return r


class TestGlobalOverridesAreIgnoredWhenMultiTenant:
    def test_a_store_gets_its_own_clone_not_the_global_override(self, root, tmp_path):
        env = {
            "MEMD_STORES_ROOT": str(root),
            "MEMD_PROFILE": "a@x.com",
            "MEMD_CLONE": str(tmp_path / "shared" / "clone"),
            "MEMD_DB": str(tmp_path / "shared" / "memd.db"),
        }
        cfg = Config.from_env(env, env_file=None)
        assert cfg.clone == root / "a@x.com" / "clone"
        assert cfg.db == root / "a@x.com" / "memd.db"

    def test_two_stores_do_not_collapse_onto_one_clone(self, root, tmp_path):
        shared = str(tmp_path / "shared" / "clone")
        base = {
            "MEMD_STORES_ROOT": str(root),
            "MEMD_CLONE": shared,
            "MEMD_DB": str(tmp_path / "shared" / "memd.db"),
        }
        a = Config.from_env({**base, "MEMD_PROFILE": "a@x.com"}, env_file=None)
        b = Config.from_env({**base, "MEMD_PROFILE": "b@x.com"}, env_file=None)
        assert a.clone != b.clone
        assert a.db != b.db

    def test_single_tenant_deploy_variables_do_not_leak_into_a_user_store(self, root, tmp_path):
        """The exact shape of a single-tenant compose file, plus a stores root."""
        pilot = tmp_path / "data"
        env = {
            "MEMD_STORES_ROOT": str(root),
            "MEMD_PROFILE": "a@x.com",
            "MEMD_ENFORCE_PROFILE": "1",
            "MEMD_AMBER_CLONE": str(pilot / "clone"),
            "MEMD_AMBER_DB": str(pilot / "memd.db"),
            "MEMD_CLONE": str(pilot / "clone"),
            "MEMD_DB": str(pilot / "memd.db"),
        }
        cfg = Config.from_env(env, env_file=None)
        assert cfg.clone == root / "a@x.com" / "clone"
        assert str(cfg.clone) != str(pilot / "clone")

    def test_the_guard_still_refuses_the_override_if_one_is_forced_through(self, root, tmp_path):
        """Defence in depth: even handed the override directly, guard_paths refuses."""
        (root / "b@x.com" / "clone").mkdir(parents=True)
        with pytest.raises(CrossProfileViolation):
            guard_paths("a@x.com", root / "b@x.com" / "clone", root / "a@x.com" / "memd.db")


class TestSingleTenantOverridesStillWork:
    """No stores root: MEMD_CLONE/MEMD_DB behave exactly as shipped."""

    def test_the_global_override_still_wins(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MEMD_STORES_ROOT", raising=False)
        env = {
            "MEMD_PROFILE": "amber",
            "MEMD_CLONE": str(tmp_path / "explicit" / "clone"),
            "MEMD_DB": str(tmp_path / "explicit" / "memd.db"),
        }
        cfg = Config.from_env(env, env_file=None)
        assert cfg.clone == tmp_path / "explicit" / "clone"
        assert cfg.db == tmp_path / "explicit" / "memd.db"


class TestConfigResolvesDynamicStores:
    """The third copy of the registry lived inline in Config._profile_paths."""

    def test_a_store_name_yields_paths_rather_than_none(self, root):
        cfg = Config.from_env(
            {"MEMD_STORES_ROOT": str(root), "MEMD_PROFILE": "a@x.com"}, env_file=None
        )
        # Before the fix the inline {"amber":..., "cobalt":...} map returned
        # (None, None) for any other name, so the core fell back to the CWD.
        assert cfg.clone is not None and cfg.db is not None
        assert cfg.profile == "a@x.com"

    def test_a_bound_identity_reaches_its_own_config_through_the_mcp_seam(self, root):
        import memd.mcp as mcp

        bind_identity("a@x.com")
        cfg = mcp._cfg_for("a@x.com")
        assert cfg.clone == root / "a@x.com" / "clone"
        assert cfg.db == root / "a@x.com" / "memd.db"
