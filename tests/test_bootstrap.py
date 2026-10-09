"""Per-store clone bootstrap: the link that joins pieces 1, 2 and 3.

Piece 1 resolves an identity to `<root>/<store>/{clone,memd.db}`. Piece 2 binds
that identity from a bearer. Piece 3 creates the Forgejo repo. Nothing created
the CLONE, so the first save by a newly authenticated user would have written
into a directory with no git repository in it.

`deploy/entrypoint.py` wires exactly one clone at startup from MEMD_GIT_REMOTE,
which is right for a single-store deployment and cannot work for N users who
appear one at a time. So the clone is created lazily, on first use, by whoever
resolves the store.

Two traps this pins deliberately:

  * `store.clone_lock` MKDIRS `<clone>/.git` to hold its lock file, so
    "`.git` exists" is NOT a usable test for "this is a repository". The check
    has to be `.git/HEAD`, and the bootstrap lock has to live outside `.git`.
  * A store whose remote is unreachable must still work locally. Forgejo being
    down should degrade to "saved locally, sync pending", which is what save()
    already reports, not "this user has no memory today".
"""
import subprocess
import threading

import pytest

from memd.store_bootstrap import ensure_store
from memd.config import Config


def _cfg(root, store):
    import os

    env = dict(os.environ)
    env["MEMD_PROFILE"] = store
    env["MEMD_STORES_ROOT"] = str(root)
    return Config.from_env(env, env_file=None)


def _git(clone, *args):
    return subprocess.run(["git", "-C", str(clone), *args],
                          capture_output=True, text=True).stdout.strip()


def _is_repo(clone) -> bool:
    """A real repository, not merely a directory called .git."""
    return (clone / ".git" / "HEAD").exists()


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "stores"
    r.mkdir()
    monkeypatch.setenv("MEMD_STORES_ROOT", str(r))
    monkeypatch.delenv("MEMD_GIT_REMOTE", raising=False)
    monkeypatch.delenv("MEMD_STORES_REPO_TEMPLATE", raising=False)
    monkeypatch.setenv("MEMD_GIT_AUTHOR_NAME", "memd service")
    monkeypatch.setenv("MEMD_GIT_AUTHOR_EMAIL", "memd@example.invalid")
    return r


class TestFirstUse:
    def test_a_new_store_gets_a_real_git_repository(self, root):
        cfg = _cfg(root, "alice@x.com")
        assert ensure_store(cfg) is True
        assert _is_repo(root / "alice@x.com" / "clone")

    def test_it_has_a_commit_so_saves_have_a_parent(self, root):
        cfg = _cfg(root, "alice@x.com")
        ensure_store(cfg)
        clone = root / "alice@x.com" / "clone"
        assert _git(clone, "rev-parse", "--verify", "HEAD")

    def test_the_author_identity_is_set(self, root):
        cfg = _cfg(root, "alice@x.com")
        ensure_store(cfg)
        clone = root / "alice@x.com" / "clone"
        assert _git(clone, "config", "user.name") == "memd service"
        assert _git(clone, "config", "user.email") == "memd@example.invalid"

    def test_the_branch_is_main(self, root):
        cfg = _cfg(root, "alice@x.com")
        ensure_store(cfg)
        clone = root / "alice@x.com" / "clone"
        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    def test_the_db_directory_exists_too(self, root):
        cfg = _cfg(root, "alice@x.com")
        ensure_store(cfg)
        assert (root / "alice@x.com").is_dir()


class TestIdempotence:
    def test_a_second_call_does_nothing(self, root):
        cfg = _cfg(root, "alice@x.com")
        assert ensure_store(cfg) is True
        assert ensure_store(cfg) is False

    def test_it_does_not_disturb_existing_content(self, root):
        cfg = _cfg(root, "alice@x.com")
        ensure_store(cfg)
        clone = root / "alice@x.com" / "clone"
        (clone / "note.md").write_text("a saved fact")
        subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(clone), "commit", "-qm", "note"],
                       check=True, capture_output=True)
        head = _git(clone, "rev-parse", "HEAD")
        ensure_store(cfg)
        assert _git(clone, "rev-parse", "HEAD") == head
        assert (clone / "note.md").read_text() == "a saved fact"

    def test_a_lock_file_left_by_clone_lock_is_not_mistaken_for_a_repo(self, root):
        """store.clone_lock mkdirs <clone>/.git. That must not look bootstrapped."""
        from memd.store import clone_lock

        clone = root / "alice@x.com" / "clone"
        clone.mkdir(parents=True)
        with clone_lock(clone):
            pass
        assert (clone / ".git").is_dir() and not _is_repo(clone)
        assert ensure_store(_cfg(root, "alice@x.com")) is True
        assert _is_repo(clone)


class TestIsolation:
    def test_bootstrapping_one_store_does_not_create_another(self, root):
        ensure_store(_cfg(root, "alice@x.com"))
        assert not (root / "bob@x.com").exists()

    def test_two_stores_are_separate_repositories(self, root):
        ensure_store(_cfg(root, "alice@x.com"))
        ensure_store(_cfg(root, "bob@x.com"))
        a = _git(root / "alice@x.com" / "clone", "rev-parse", "--git-dir")
        b = _git(root / "bob@x.com" / "clone", "rev-parse", "--git-dir")
        assert (root / "alice@x.com" / "clone" / ".git" / "HEAD").exists()
        assert (root / "bob@x.com" / "clone" / ".git" / "HEAD").exists()
        assert a and b


class TestSingleTenantIsUntouched:
    def test_it_is_a_no_op_without_a_stores_root(self, tmp_path, monkeypatch):
        """entrypoint.py owns the clone in a single-store deployment."""
        monkeypatch.delenv("MEMD_STORES_ROOT", raising=False)
        monkeypatch.setenv("MEMD_CLONE", str(tmp_path / "clone"))
        monkeypatch.setenv("MEMD_DB", str(tmp_path / "memd.db"))
        monkeypatch.setenv("MEMD_PROFILE", "amber")
        monkeypatch.setenv("MEMD_AMBER_CLONE", str(tmp_path / "clone"))
        monkeypatch.setenv("MEMD_AMBER_DB", str(tmp_path / "memd.db"))
        cfg = Config.from_env(env_file=None)
        assert ensure_store(cfg) is False
        assert not (tmp_path / "clone" / ".git" / "HEAD").exists()


class TestRemoteHandling:
    def test_an_unreachable_remote_still_yields_a_usable_local_store(self, root, monkeypatch):
        """Forgejo being down must not mean this user has no memory today.

        save() already reports `synced: false` when a push fails; that is the
        right degradation. Refusing to create the store would turn a sync
        outage into a total outage for every new user.
        """
        monkeypatch.setenv(
            "MEMD_STORES_REPO_TEMPLATE",
            "ssh://git@unreachable.invalid:22/memory/{store}.git")
        cfg = _cfg(root, "alice@x.com")
        assert ensure_store(cfg) is True
        clone = root / "alice@x.com" / "clone"
        assert _is_repo(clone)
        # The repo SLUG, not the store name: "@" is not legal in a Forgejo
        # repository name, so the two cannot be the same string.
        assert _git(clone, "remote", "get-url", "origin").endswith(
            "/memory/u-alice-at-x.com.git")

    def test_no_remote_configured_means_no_origin(self, root):
        ensure_store(_cfg(root, "alice@x.com"))
        clone = root / "alice@x.com" / "clone"
        assert _git(clone, "remote") == ""

    def test_the_deploy_key_is_wired_per_clone(self, root, monkeypatch, tmp_path):
        """core.sshCommand is per-clone git config, which is what makes a
        per-store deploy key possible without changing memd's core."""
        key = tmp_path / "id_ed25519"
        key.write_text("x")
        monkeypatch.setenv("MEMD_SSH_KEY", str(key))
        monkeypatch.setenv(
            "MEMD_STORES_REPO_TEMPLATE",
            "ssh://git@unreachable.invalid:22/memory/{store}.git")
        ensure_store(_cfg(root, "alice@x.com"))
        cmd = _git(root / "alice@x.com" / "clone", "config", "core.sshCommand")
        assert str(key) in cmd
        assert "IdentitiesOnly=yes" in cmd


class TestRefusals:
    def test_a_nonempty_non_git_directory_is_refused(self, root):
        """Mirrors entrypoint.py. Something else owns that directory."""
        clone = root / "alice@x.com" / "clone"
        clone.mkdir(parents=True)
        (clone / "stranger.txt").write_text("not ours")
        with pytest.raises(Exception):
            ensure_store(_cfg(root, "alice@x.com"))

    def test_a_store_name_that_does_not_validate_is_refused(self, root):
        import os

        env = dict(os.environ)
        env["MEMD_PROFILE"] = "../escape"
        env["MEMD_STORES_ROOT"] = str(root)
        with pytest.raises(Exception):
            ensure_store(Config.from_env(env, env_file=None))


class TestConcurrency:
    def test_two_callers_racing_produce_one_repository(self, root):
        """Two requests from the same user arriving together is the normal case
        for an agent that recalls and saves in the same breath."""
        cfg = _cfg(root, "alice@x.com")
        errors = []

        def go():
            try:
                ensure_store(cfg)
            except Exception as exc:  # pragma: no cover - only on a real race bug
                errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        clone = root / "alice@x.com" / "clone"
        assert _is_repo(clone)
        assert _git(clone, "rev-parse", "--verify", "HEAD")
