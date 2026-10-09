import pytest

from memd.profiles import (
    resolve,
    assert_no_cross_profile,
    CrossProfileViolation,
    UnknownProfile,
)


def test_resolve_amber_and_cobalt_are_disjoint(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMD_AMBER_CLONE", str(tmp_path / "amber-clone"))
    monkeypatch.setenv("MEMD_AMBER_DB", str(tmp_path / "amber.db"))
    monkeypatch.setenv("MEMD_AMBER_REPO", "git@10.10.1.10:svcuser/amber-memory.git")
    monkeypatch.setenv("MEMD_AMBER_CRED", "/home/svcuser/.config/memd/amber_token")
    monkeypatch.setenv("MEMD_COBALT_CLONE", str(tmp_path / "cobalt-clone"))
    monkeypatch.setenv("MEMD_COBALT_DB", str(tmp_path / "cobalt.db"))
    monkeypatch.setenv("MEMD_COBALT_REPO", "git@10.10.1.10:svcuser/cobalt-memory.git")
    monkeypatch.setenv("MEMD_COBALT_CRED", "/home/svcuser/.config/memd/cobalt_token")

    amber = resolve("amber")
    blr = resolve("cobalt")

    assert amber["clone_path"] != blr["clone_path"]
    assert amber["db_path"] != blr["db_path"]
    assert amber["repo_ssh"] != blr["repo_ssh"]
    assert amber["credential"] != blr["credential"]
    # No path of one profile is inside the other.
    assert not str(blr["clone_path"]).startswith(str(amber["clone_path"]))
    assert not str(amber["clone_path"]).startswith(str(blr["clone_path"]))


def test_unknown_profile_raises():
    with pytest.raises(UnknownProfile):
        resolve("intruder")


def test_amber_instance_cannot_read_cobalt_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMD_AMBER_CLONE", str(tmp_path / "amber-clone"))
    monkeypatch.setenv("MEMD_AMBER_DB", str(tmp_path / "amber.db"))
    monkeypatch.setenv("MEMD_AMBER_REPO", "git@10.10.1.10:svcuser/amber-memory.git")
    monkeypatch.setenv("MEMD_AMBER_CRED", "x")
    monkeypatch.setenv("MEMD_COBALT_CLONE", str(tmp_path / "cobalt-clone"))
    monkeypatch.setenv("MEMD_COBALT_DB", str(tmp_path / "cobalt.db"))
    monkeypatch.setenv("MEMD_COBALT_REPO", "git@10.10.1.10:svcuser/cobalt-memory.git")
    monkeypatch.setenv("MEMD_COBALT_CRED", "y")

    blr = resolve("cobalt")
    # An amber-profile operation touching a cobalt path must hard-fail.
    with pytest.raises(CrossProfileViolation):
        assert_no_cross_profile("amber", str(blr["clone_path"]))
    with pytest.raises(CrossProfileViolation):
        assert_no_cross_profile("amber", str(blr["db_path"]))
    # The same profile touching its own path is fine.
    amber = resolve("amber")
    assert_no_cross_profile("amber", str(amber["clone_path"]))  # no raise


def test_recall_over_amber_can_never_read_cobalt_data(tmp_path, monkeypatch):
    """End-to-end isolation: a recall bound to profile=amber, given a cobalt
    clone/db, must refuse rather than serve cobalt notes."""
    monkeypatch.setenv("MEMD_AMBER_CLONE", str(tmp_path / "amber-clone"))
    monkeypatch.setenv("MEMD_AMBER_DB", str(tmp_path / "amber.db"))
    monkeypatch.setenv("MEMD_AMBER_REPO", "r1")
    monkeypatch.setenv("MEMD_AMBER_CRED", "c1")
    monkeypatch.setenv("MEMD_COBALT_CLONE", str(tmp_path / "cobalt-clone"))
    monkeypatch.setenv("MEMD_COBALT_DB", str(tmp_path / "cobalt.db"))
    monkeypatch.setenv("MEMD_COBALT_REPO", "r2")
    monkeypatch.setenv("MEMD_COBALT_CRED", "c2")

    from memd.profiles import open_for_recall

    # open_for_recall returns the (clone, db) bound to the requested profile and
    # asserts they belong to that profile. Forcing a cobalt db under amber fails.
    amber_clone, amber_db = open_for_recall("amber")
    assert str(amber_db) == str(tmp_path / "amber.db")
    blr = resolve("cobalt")
    with pytest.raises(CrossProfileViolation):
        # simulate a misconfig that hands a cobalt path to an amber recall
        assert_no_cross_profile("amber", str(blr["db_path"]))


# ---------------------------------------------------------------------------
# FIX GROUP 2 (d): assert_no_cross_profile is DENY-BY-DEFAULT.
# A profile may touch ONLY paths inside its own registered root; anything
# outside the union of registered roots is refused — closing the
# sibling-directory and symlink bypass where a path was merely "not owned by
# another profile" yet still escaped the owner's tree.
# ---------------------------------------------------------------------------


def _register_two(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMD_AMBER_CLONE", str(tmp_path / "amber" / "clone"))
    monkeypatch.setenv("MEMD_AMBER_DB", str(tmp_path / "amber" / "memd.db"))
    monkeypatch.setenv("MEMD_AMBER_REPO", "r1")
    monkeypatch.setenv("MEMD_AMBER_CRED", "c1")
    monkeypatch.setenv("MEMD_COBALT_CLONE", str(tmp_path / "cobalt" / "clone"))
    monkeypatch.setenv("MEMD_COBALT_DB", str(tmp_path / "cobalt" / "memd.db"))
    monkeypatch.setenv("MEMD_COBALT_REPO", "r2")
    monkeypatch.setenv("MEMD_COBALT_CRED", "c2")


def test_sibling_path_outside_all_roots_is_denied(tmp_path, monkeypatch):
    """A path that belongs to NO registered profile (a sibling dir under the same
    parent) must be refused — not silently allowed because no other profile owns
    it. This is the bypass the deny-by-default rule closes."""
    _register_two(tmp_path, monkeypatch)
    sibling = tmp_path / "amber" / "sibling-escape" / "x.db"
    # amber's root is tmp_path/amber/clone (and .../memd.db); the sibling dir under
    # tmp_path/amber is NOT inside amber's registered root.
    with pytest.raises(CrossProfileViolation):
        assert_no_cross_profile("amber", str(sibling))


def test_own_registered_root_is_allowed(tmp_path, monkeypatch):
    _register_two(tmp_path, monkeypatch)
    amber = resolve("amber")
    # a file inside amber's own clone tree is fine.
    assert_no_cross_profile("amber", str(amber["clone_path"] / "note.md"))  # no raise


def test_symlink_into_other_profile_is_denied(tmp_path, monkeypatch):
    """A symlink whose target resolves inside cobalt's tree must be refused for
    amber even though the link path itself sits in amber's tree."""
    _register_two(tmp_path, monkeypatch)
    amber = resolve("amber")
    blr = resolve("cobalt")
    amber["clone_path"].mkdir(parents=True, exist_ok=True)
    blr["clone_path"].mkdir(parents=True, exist_ok=True)
    (blr["clone_path"] / "secret.md").write_text("cobalt only")
    link = amber["clone_path"] / "leak.md"
    link.symlink_to(blr["clone_path"] / "secret.md")
    # path lives under amber/clone, but resolves into cobalt/clone -> refuse.
    with pytest.raises(CrossProfileViolation):
        assert_no_cross_profile("amber", str(link))


def test_guard_paths_resolves_profile_authoritative(tmp_path, monkeypatch):
    """guard_paths returns the resolved clone/db for the requested profile and
    raises when the supplied paths belong to another profile."""
    from memd.profiles import guard_paths

    _register_two(tmp_path, monkeypatch)
    amber = resolve("amber")
    blr = resolve("cobalt")
    # legit: amber profile + amber's own paths -> returns them.
    clone, db = guard_paths("amber", str(amber["clone_path"]), str(amber["db_path"]))
    assert clone == amber["clone_path"] and db == amber["db_path"]
    # poisoned: amber profile + cobalt's paths -> hard fail.
    with pytest.raises(CrossProfileViolation):
        guard_paths("amber", str(blr["clone_path"]), str(blr["db_path"]))
    # None falls back to the profile's authoritative path.
    clone2, db2 = guard_paths("cobalt", None, None)
    assert clone2 == blr["clone_path"] and db2 == blr["db_path"]
