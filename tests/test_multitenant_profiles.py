"""The registry becomes a directory (Plan 2, §9a), and the guard generalises.

§9a's reason for costing this at one to two days: "the deny-by-default guard in
memd/profiles.py already reasons in roots per profile, so it generalises". These
tests hold it to that. The registry stops being a two-row dict and becomes
"whatever lives under MEMD_STORES_ROOT", and the guard has to keep refusing a
sibling store's path -- including a sibling that does not exist on disk yet,
which is the case a registry-enumeration approach silently gets wrong.

The legacy `amber`/`cobalt` rows survive when their MEMD_<PROFILE>_* env is set.
That is deliberate: the pilot container sets MEMD_AMBER_CLONE=/data/clone, and
turning on a stores root must not silently relocate a serving store's data.
"""
import pytest

from memd.profiles import (
    CrossProfileViolation,
    UnknownProfile,
    assert_no_cross_profile,
    guard_paths,
    registered_roots,
    resolve,
)
from memd.stores import InvalidStoreName


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "stores"
    r.mkdir()
    monkeypatch.setenv("MEMD_STORES_ROOT", str(r))
    return r


class TestDynamicResolution:
    def test_a_store_resolves_to_its_own_clone_and_db(self, root):
        cfg = resolve("a@x.com")
        assert cfg["clone_path"] == root / "a@x.com" / "clone"
        assert cfg["db_path"] == root / "a@x.com" / "memd.db"
        assert cfg["profile"] == "a@x.com"

    def test_a_store_that_does_not_exist_yet_still_resolves(self, root):
        """First save creates the directory; resolution must not need it."""
        cfg = resolve("brand.new@x.com")
        assert not cfg["clone_path"].exists()
        assert cfg["clone_path"] == root / "brand.new@x.com" / "clone"

    def test_two_stores_are_disjoint(self, root):
        a, b = resolve("a@x.com"), resolve("b@x.com")
        assert a["clone_path"] != b["clone_path"]
        assert a["db_path"] != b["db_path"]
        assert not str(b["clone_path"]).startswith(str(a["clone_path"]))
        assert not str(a["clone_path"]).startswith(str(b["clone_path"]))

    def test_a_malformed_store_name_never_resolves(self, root):
        for bad in ("../amber", "a@x.com/../b@x.com", ".git", ""):
            with pytest.raises((InvalidStoreName, UnknownProfile)):
                resolve(bad)

    def test_an_explicit_legacy_profile_env_still_wins(self, root, tmp_path, monkeypatch):
        """The pilot sets MEMD_AMBER_CLONE; a stores root must not move it."""
        monkeypatch.setenv("MEMD_AMBER_CLONE", str(tmp_path / "pilot-clone"))
        monkeypatch.setenv("MEMD_AMBER_DB", str(tmp_path / "pilot.db"))
        monkeypatch.setenv("MEMD_AMBER_REPO", "ssh://example.invalid/pilot.git")
        monkeypatch.setenv("MEMD_AMBER_CRED", "MEMD_TOKEN_AMBER")
        cfg = resolve("amber")
        assert cfg["clone_path"] == tmp_path / "pilot-clone"
        assert not str(cfg["clone_path"]).startswith(str(root))

    def test_without_a_stores_root_a_dynamic_name_is_unknown(self, monkeypatch):
        monkeypatch.delenv("MEMD_STORES_ROOT", raising=False)
        with pytest.raises(UnknownProfile):
            resolve("a@x.com")


class TestRegisteredRoots:
    def test_existing_stores_are_enumerated(self, root):
        (root / "a@x.com").mkdir()
        (root / "b@x.com").mkdir()
        roots = registered_roots()
        assert "a@x.com" in roots and "b@x.com" in roots

    def test_a_non_store_entry_under_the_root_is_ignored(self, root):
        (root / "..hidden").mkdir()
        (root / "not a store").mkdir()
        roots = registered_roots()
        assert "..hidden" not in roots and "not a store" not in roots


class TestSiblingStoresAreRefused:
    """The security property, at the path layer."""

    def test_one_store_may_not_touch_an_existing_siblings_clone(self, root):
        (root / "b@x.com" / "clone").mkdir(parents=True)
        with pytest.raises(CrossProfileViolation):
            assert_no_cross_profile("a@x.com", str(root / "b@x.com" / "clone"))

    def test_one_store_may_not_touch_a_siblings_note(self, root):
        (root / "b@x.com" / "clone").mkdir(parents=True)
        (root / "b@x.com" / "clone" / "secret.md").write_text("private")
        with pytest.raises(CrossProfileViolation):
            assert_no_cross_profile(
                "a@x.com", str(root / "b@x.com" / "clone" / "secret.md")
            )

    def test_one_store_may_not_touch_a_siblings_db(self, root):
        (root / "b@x.com").mkdir()
        with pytest.raises(CrossProfileViolation):
            assert_no_cross_profile("a@x.com", str(root / "b@x.com" / "memd.db"))

    def test_a_sibling_that_does_not_exist_yet_is_still_refused(self, root):
        """The rule is structural, not an enumeration of what is on disk.

        Enumerating only existing directories would leave a not-yet-created
        store owned by nobody, so the refusal would depend on whether the
        victim had ever saved a note.
        """
        assert not (root / "victim@x.com").exists()
        with pytest.raises(CrossProfileViolation):
            assert_no_cross_profile("a@x.com", str(root / "victim@x.com" / "clone"))

    def test_the_stores_root_itself_is_not_touchable(self, root):
        with pytest.raises(CrossProfileViolation):
            assert_no_cross_profile("a@x.com", str(root))

    def test_a_legacy_profile_may_not_reach_into_the_stores_root(self, root, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMD_AMBER_CLONE", str(tmp_path / "pilot-clone"))
        monkeypatch.setenv("MEMD_AMBER_DB", str(tmp_path / "pilot.db"))
        monkeypatch.setenv("MEMD_AMBER_REPO", "r")
        monkeypatch.setenv("MEMD_AMBER_CRED", "c")
        with pytest.raises(CrossProfileViolation):
            assert_no_cross_profile("amber", str(root / "b@x.com" / "clone"))

    def test_a_store_may_not_reach_a_legacy_profiles_clone(self, root, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMD_AMBER_CLONE", str(tmp_path / "pilot-clone"))
        monkeypatch.setenv("MEMD_AMBER_DB", str(tmp_path / "pilot.db"))
        monkeypatch.setenv("MEMD_AMBER_REPO", "r")
        monkeypatch.setenv("MEMD_AMBER_CRED", "c")
        with pytest.raises(CrossProfileViolation):
            assert_no_cross_profile("a@x.com", str(tmp_path / "pilot-clone"))

    def test_anything_outside_every_root_is_refused(self, root, tmp_path):
        with pytest.raises(CrossProfileViolation):
            assert_no_cross_profile("a@x.com", str(tmp_path / "elsewhere" / "x.md"))


class TestOwnStoreIsAllowed:
    def test_a_store_may_touch_its_own_clone(self, root):
        assert_no_cross_profile("a@x.com", str(root / "a@x.com" / "clone"))

    def test_a_store_may_touch_a_note_inside_its_own_clone(self, root):
        assert_no_cross_profile(
            "a@x.com", str(root / "a@x.com" / "clone" / "note.md")
        )

    def test_a_store_may_touch_its_own_db(self, root):
        assert_no_cross_profile("a@x.com", str(root / "a@x.com" / "memd.db"))

    def test_the_first_save_into_a_new_store_is_allowed(self, root):
        assert not (root / "new@x.com").exists()
        assert_no_cross_profile("new@x.com", str(root / "new@x.com" / "clone"))


class TestGuardPaths:
    def test_a_store_gets_its_own_paths_back(self, root):
        clone, db = guard_paths("a@x.com", None, None)
        assert clone == root / "a@x.com" / "clone"
        assert db == root / "a@x.com" / "memd.db"

    def test_a_clone_belonging_to_another_store_is_refused(self, root):
        with pytest.raises(CrossProfileViolation):
            guard_paths("a@x.com", root / "b@x.com" / "clone", root / "a@x.com" / "memd.db")

    def test_a_db_belonging_to_another_store_is_refused(self, root):
        with pytest.raises(CrossProfileViolation):
            guard_paths("a@x.com", root / "a@x.com" / "clone", root / "b@x.com" / "memd.db")

    def test_a_symlink_into_a_sibling_store_is_refused(self, root, tmp_path):
        """Resolution happens before the check, so a link is not a way through."""
        (root / "b@x.com" / "clone").mkdir(parents=True)
        link = tmp_path / "shortcut"
        try:
            link.symlink_to(root / "b@x.com" / "clone", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        with pytest.raises(CrossProfileViolation):
            assert_no_cross_profile("a@x.com", str(link))
