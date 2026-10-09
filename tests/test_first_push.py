"""A brand-new store's FIRST push must work, against an empty remote.

Found on the live system. A new user's repo is created empty (memd owns its
content, so an auto-init README would make the first push a non-fast-forward),
and their store is bootstrapped locally with no upstream set. `save()` then ran

    git pull --rebase --autostash

which fails with "There is no tracking information for the current branch", so
the push it was about to do never happened. Every save reported `synced: false`
and the note stayed on one host forever. Nothing failed loudly.

This is the whole hands-off path for a new starter, so it is worth a test that
uses real git against a real bare remote rather than a mock: the failure was in
git's behaviour, not in memd's logic, and a mock would have been written to
match whatever memd already believed.
"""
import subprocess
from pathlib import Path

import pytest

from memd.save import _pull_rebase_push


def _git(repo: Path, *args: str, check: bool = True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True)


@pytest.fixture
def store_and_remote(tmp_path):
    """A freshly bootstrapped store with an EMPTY bare remote and no upstream.

    Exactly the shape the onboarding job plus memd's bootstrap produce.
    """
    remote = tmp_path / "u-newstarter.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q", "-b", "main")
    _git(clone, "config", "user.email", "memd@example.invalid")
    _git(clone, "config", "user.name", "memd service")
    _git(clone, "commit", "--allow-empty", "-q", "-m", "Initialise memory store")
    _git(clone, "remote", "add", "origin", str(remote))
    return clone, remote


def _remote_head(remote: Path) -> str:
    r = _git(remote, "rev-parse", "--verify", "-q", "main", check=False)
    return r.stdout.strip()


class TestFirstPush:
    def test_the_precondition_holds_no_upstream_and_empty_remote(self, store_and_remote):
        clone, remote = store_and_remote
        r = _git(clone, "rev-parse", "--abbrev-ref", "main@{upstream}", check=False)
        assert r.returncode != 0, "fixture should have no upstream"
        assert _remote_head(remote) == "", "fixture remote should be empty"

    def test_the_first_push_publishes_the_store(self, store_and_remote):
        """The regression: this raised, so nothing was ever published."""
        clone, remote = store_and_remote
        _pull_rebase_push(clone)
        assert _remote_head(remote) == _git(clone, "rev-parse", "HEAD").stdout.strip()

    def test_it_sets_the_upstream_so_later_saves_can_rebase(self, store_and_remote):
        clone, _ = store_and_remote
        _pull_rebase_push(clone)
        r = _git(clone, "rev-parse", "--abbrev-ref", "main@{upstream}", check=False)
        assert r.returncode == 0 and r.stdout.strip() == "origin/main"

    def test_a_second_save_still_publishes(self, store_and_remote):
        """Once linked, the normal pull-rebase-push path must keep working."""
        clone, remote = store_and_remote
        _pull_rebase_push(clone)
        (clone / "note.md").write_text("a second fact")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-q", "-m", "second")
        _pull_rebase_push(clone)
        assert _remote_head(remote) == _git(clone, "rev-parse", "HEAD").stdout.strip()

    def test_it_still_integrates_remote_commits(self, store_and_remote):
        """The rebase must not be skipped once an upstream exists.

        Someone correcting a note in Forgejo is a supported workflow, so a push
        that ignored the remote would silently drop their edit.
        """
        clone, remote = store_and_remote
        _pull_rebase_push(clone)

        other = clone.parent / "other"
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
        _git(other, "config", "user.email", "someone@example.invalid")
        _git(other, "config", "user.name", "Someone")
        (other / "edited-in-forgejo.md").write_text("corrected by hand")
        _git(other, "add", "-A")
        _git(other, "commit", "-q", "-m", "hand edit")
        _git(other, "push", "-q", "origin", "main")

        (clone / "local.md").write_text("saved locally meanwhile")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-q", "-m", "local save")
        _pull_rebase_push(clone)

        assert (clone / "edited-in-forgejo.md").exists(), "the remote edit was lost"
        assert _remote_head(remote) == _git(clone, "rev-parse", "HEAD").stdout.strip()

    def test_an_unreachable_remote_still_raises(self, tmp_path):
        """Failure must stay visible: save() turns this into synced=false."""
        clone = tmp_path / "c"
        clone.mkdir()
        _git(clone, "init", "-q", "-b", "main")
        _git(clone, "config", "user.email", "m@e.invalid")
        _git(clone, "config", "user.name", "m")
        _git(clone, "commit", "--allow-empty", "-q", "-m", "x")
        _git(clone, "remote", "add", "origin", str(tmp_path / "nope.git"))
        with pytest.raises(Exception):
            _pull_rebase_push(clone)
