"""The store name and its Forgejo repo name must be derived in ONE place.

Found by deploying it. The onboarding job created `memory/u-alice`, and
memd independently wired the clone's remote to
`memory/alice@corp.example.com.git`. Both were internally consistent
and they disagreed, so every save reported `synced: false` forever and no user's
memory would ever have reached Forgejo. Nothing failed loudly; it just quietly
never synced.

The cause is that a store name and a repo name CANNOT be the same string: `@` is
legal in a store directory and not in a Forgejo repository name. Two independent
derivations of "the repo for this store" is therefore a standing invitation to
this exact bug, so there is now one function and both sides import it.
"""
import pytest

from memd.stores import InvalidStoreName, repo_slug, store_name


@pytest.fixture(autouse=True)
def _local_domain(monkeypatch):
    monkeypatch.setenv("MEMD_STORES_LOCAL_DOMAIN", "corp.example.com")


class TestSlug:
    def test_our_own_domain_becomes_u_localpart(self):
        assert repo_slug("alice@corp.example.com") == "u-alice"

    def test_the_slug_never_contains_an_at_sign(self):
        """The whole reason this function exists: @ is not a legal repo name."""
        for email in ("a@b.com", "first.last@corp.example.com",
                      "x+tag@external.example.org"):
            assert "@" not in repo_slug(email), email

    def test_it_is_a_single_path_segment(self):
        for email in ("a@b.com", "x+tag@external.example.org"):
            s = repo_slug(email)
            assert "/" not in s and "\\" not in s and not s.startswith(".")

    def test_case_is_normalised(self):
        assert repo_slug("Alice@Corp.Example.Com") == "u-alice"

    def test_plus_addressing_and_dots_survive_legibly(self):
        assert repo_slug("first.last+memd@corp.example.com") == \
            "u-first.last-memd"

    def test_an_external_domain_keeps_the_domain_so_it_cannot_collide(self):
        ours = repo_slug("sam@corp.example.com")
        theirs = repo_slug("sam@external.example.com")
        assert ours != theirs

    def test_a_malformed_store_name_is_refused(self):
        for bad in ("../escape", "", ".", "a/b@c.com"):
            with pytest.raises(InvalidStoreName):
                repo_slug(bad)

    def test_it_accepts_a_legacy_profile_name(self):
        assert repo_slug("amber") == "u-amber"

    def test_without_a_local_domain_every_store_keeps_its_domain(self, monkeypatch):
        monkeypatch.delenv("MEMD_STORES_LOCAL_DOMAIN")
        assert repo_slug("alice@corp.example.com") == "u-alice-at-corp.example.com"


class TestOneDerivationOnly:
    def test_the_remote_memd_wires_uses_the_slug_not_the_store_name(self, monkeypatch):
        """The regression. The remote must name the repo the sync job creates."""
        from memd.store_bootstrap import store_remote

        monkeypatch.setenv(
            "MEMD_STORES_REPO_TEMPLATE",
            "ssh://git@git.example.com:22/memory/{store}.git")
        remote = store_remote("alice@corp.example.com")
        assert remote.endswith("/memory/u-alice.git"), remote
        assert "@corp.example.com.git" not in remote

    def test_no_remote_configured_stays_empty(self, monkeypatch):
        from memd.store_bootstrap import store_remote

        monkeypatch.delenv("MEMD_STORES_REPO_TEMPLATE", raising=False)
        assert store_remote("a@b.com") == ""

    def test_the_store_directory_is_still_the_email(self, monkeypatch, tmp_path):
        """The DIRECTORY stays the identity; only the repo name is derived.

        Keeping the directory equal to the email is what makes the filesystem,
        the audit trail and the token all read the same.
        """
        from memd.stores import store_paths

        monkeypatch.setenv("MEMD_STORES_ROOT", str(tmp_path))
        clone, _ = store_paths("alice@corp.example.com")
        assert clone.parent.name == "alice@corp.example.com"

    def test_slug_and_store_name_agree_on_what_is_valid(self):
        """Anything store_name accepts, repo_slug must also handle."""
        for email in ("a@b.com", "amber", "first.last+x@sub.example.org",
                      "z_9@corp.example.com"):
            assert repo_slug(store_name(email))
