"""Store-name validation: the path boundary for per-user stores (Plan 2, §9a).

A store name arrives from an authenticated identity (§9b: the lower-cased
`email` claim). It becomes ONE path segment under MEMD_STORES_ROOT. Everything
that could make it more than one segment, or make it escape the root, is
refused here rather than being caught later by the path guard, so there is a
single place to reason about the boundary.

These tests are adversarial on purpose: the security property Plan 2 has to
hold is that no request field, and no shape of identity string, lets one user
reach another user's store.
"""
import pytest

from memd.stores import InvalidStoreName, store_name, store_paths, stores_root


class TestStoreNameAccepts:
    def test_a_plain_email_survives_unchanged(self):
        assert store_name("alice@corp.example.com") == "alice@corp.example.com"

    def test_case_is_normalised_so_one_person_gets_one_store(self):
        # §9b: "email claim lower-cased is the store name". Two spellings of the
        # same identity must not silently create two stores.
        assert store_name("Alice@Corp.Example.Com") == "alice@corp.example.com"

    def test_surrounding_whitespace_is_stripped(self):
        assert store_name("  alice@corp.example.com \n") == "alice@corp.example.com"

    def test_the_legacy_profile_names_are_still_valid_names(self):
        # amber/cobalt remain resolvable so the shipped suite and the pilot
        # deployment keep working.
        assert store_name("amber") == "amber"
        assert store_name("cobalt") == "cobalt"

    def test_plus_addressing_and_hyphens_are_allowed(self):
        assert store_name("first.last+memd@sub-domain.example.com") == \
            "first.last+memd@sub-domain.example.com"


class TestStoreNameRefuses:
    @pytest.mark.parametrize("bad", [
        "",
        "   ",
        ".",
        "..",
        "../amber",
        "amber/../cobalt",
        "a@b.com/../../etc",
        "a@b.com/clone",
        "..\\amber",
        "amber\\..\\cobalt",
        "/etc/passwd",
        "\\\\server\\share",
        "C:/Windows",
        "c:amber",
        ".git",
        ".hidden",
        "a@b.com\x00x",
        "a@b.com\nx",
        "a@b.com\tx",
        "a b@c.com",
        "~root",
        "a@b.com/",
        "a" * 300,
    ])
    def test_refused(self, bad):
        with pytest.raises(InvalidStoreName):
            store_name(bad)

    def test_none_is_refused_rather_than_coerced(self):
        with pytest.raises(InvalidStoreName):
            store_name(None)


class TestStorePaths:
    def test_paths_sit_directly_under_the_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMD_STORES_ROOT", str(tmp_path / "stores"))
        clone, db = store_paths("alice@corp.example.com")
        assert clone == tmp_path / "stores" / "alice@corp.example.com" / "clone"
        assert db == tmp_path / "stores" / "alice@corp.example.com" / "memd.db"

    def test_two_identities_never_share_a_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMD_STORES_ROOT", str(tmp_path / "stores"))
        a_clone, a_db = store_paths("a@x.com")
        b_clone, b_db = store_paths("b@x.com")
        assert a_clone != b_clone and a_db != b_db
        assert a_clone.parent != b_clone.parent

    def test_the_store_directory_need_not_exist_yet(self, tmp_path, monkeypatch):
        """First save for a new user creates it; resolution must not require it."""
        monkeypatch.setenv("MEMD_STORES_ROOT", str(tmp_path / "stores"))
        clone, _ = store_paths("brand.new@x.com")
        assert not clone.exists()
        assert clone.parent.parent == (tmp_path / "stores").resolve()

    def test_every_resolved_store_stays_a_direct_child_of_the_root(self, tmp_path, monkeypatch):
        """Belt and braces behind the validator: resolve() must not escape."""
        root = tmp_path / "stores"
        monkeypatch.setenv("MEMD_STORES_ROOT", str(root))
        for name in ("a@x.com", "amber", "z.z+z@sub.example.org"):
            clone, db = store_paths(name)
            assert clone.parent.parent == root.resolve()
            assert db.parent.parent == root.resolve()


class TestStoresRoot:
    def test_unset_root_means_single_tenant_and_yields_none(self, monkeypatch):
        monkeypatch.delenv("MEMD_STORES_ROOT", raising=False)
        assert stores_root() is None

    def test_store_paths_without_a_root_is_a_programming_error(self, monkeypatch):
        monkeypatch.delenv("MEMD_STORES_ROOT", raising=False)
        with pytest.raises(RuntimeError):
            store_paths("a@x.com")
