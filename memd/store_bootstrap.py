"""Create a per-user store's git clone on first use (Plan 2).

The link that joins the three pieces. Piece 1 resolves an identity to
`<root>/<store>/{clone,memd.db}`; piece 2 binds that identity from a bearer;
piece 3 creates the Forgejo repo. Nothing created the CLONE, so the first save
by a newly authenticated user would have written into a directory with no git
repository in it.

`deploy/entrypoint.py` wires exactly one clone at startup from
`MEMD_GIT_REMOTE`. That is right for a single-store deployment and cannot work
for N users who appear one at a time, so multi-tenant stores are bootstrapped
lazily by whoever first resolves them.

Single-tenant is untouched: with no `MEMD_STORES_ROOT`, this is a no-op and
entrypoint keeps full ownership of the clone.

**Two traps worth knowing before editing this.**

`store.clone_lock` mkdirs `<clone>/.git` in order to place its lock file, so
"`.git` exists" is not a usable test for "this is a repository" and the
bootstrap lock cannot live inside `.git`. The repository test here is
`.git/HEAD` and the lock sits beside the clone.

An unreachable remote must NOT prevent the store existing. `save()` already
reports `synced: false` when a push fails, which is the right degradation;
refusing to bootstrap would turn a Forgejo outage into a total outage for every
user who had not saved before.
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 120


class StoreBootstrapError(RuntimeError):
    """The store could not be prepared, and no partial state was left usable."""


def _is_repo(clone: Path) -> bool:
    """A real repository, not merely a directory named `.git`."""
    return (clone / ".git" / "HEAD").exists()


def ssh_command() -> str | None:
    """`core.sshCommand` for the deploy key, or None when none is configured.

    Mirrors deploy/entrypoint.py deliberately, including StrictHostKeyChecking:
    the known_hosts file is pinned, so an unexpected host key fails the push
    rather than being trusted.

    This is written PER CLONE, which is what makes a per-store deploy key
    possible without changing memd's core: point `MEMD_SSH_KEY` at a different
    key and each store's clone carries its own.
    """
    key = (os.environ.get("MEMD_SSH_KEY") or "").strip()
    if not key:
        return None
    parts = ["ssh", "-i", key, "-o", "IdentitiesOnly=yes",
             "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes"]
    known = (os.environ.get("MEMD_SSH_KNOWN_HOSTS") or "").strip()
    if known:
        parts += ["-o", "UserKnownHostsFile=" + known]
    return " ".join(parts)


@contextlib.contextmanager
def _bootstrap_lock(store_dir: Path):
    """Serialise bootstrap for one store across processes.

    Beside the clone, never inside `.git`: the whole point is that `.git` does
    not exist yet, and creating it to hold a lock is what makes `.git` an
    unreliable signal in the first place.
    """
    store_dir.mkdir(parents=True, exist_ok=True)
    path = store_dir / ".bootstrap.lock"
    fd = open(path, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()


def _git(clone: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(clone), *args], check=check, capture_output=True,
        text=True, timeout=GIT_TIMEOUT_SECONDS,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def store_remote(store: str) -> str:
    """The git remote for a store, from MEMD_STORES_REPO_TEMPLATE.

    Empty means local-only. Deliberately not guessed: a store pointed at the
    wrong repository is worse than one with no remote at all.
    """
    template = (os.environ.get("MEMD_STORES_REPO_TEMPLATE") or "").strip()
    if not template:
        return ""
    from memd.stores import repo_slug

    # repo_slug, NOT the store name. The store DIRECTORY is the email; the
    # Forgejo REPOSITORY cannot be, because "@" is not legal in a repo name.
    # Deriving it here independently of the onboarding job is what made every
    # store point at a repository that did not exist.
    return template.format(store=repo_slug(store))


def ensure_store(cfg) -> bool:
    """Prepare `cfg`'s store for use. Returns True if it created anything.

    Idempotent and safe to call on every request: the common path is a single
    `stat` on `<clone>/.git/HEAD`.
    """
    from memd.profiles import guard_paths
    from memd.stores import is_multitenant

    if not is_multitenant():
        # entrypoint.py owns the clone in a single-store deployment.
        return False

    # Resolve through the guard, so a bootstrap can never be talked into
    # creating a repository outside the caller's own store.
    clone, db = guard_paths(cfg.profile, cfg.clone, cfg.db)
    if _is_repo(clone):
        return False

    store_dir = clone.parent
    with _bootstrap_lock(store_dir):
        if _is_repo(clone):  # another caller won the race
            return False
        return _create(cfg.profile, clone, db)


def _create(store: str, clone: Path, db: Path) -> bool:
    clone.mkdir(parents=True, exist_ok=True)
    db.parent.mkdir(parents=True, exist_ok=True)

    # Refuse a directory that already holds someone else's content. Mirrors
    # entrypoint.py. `.git` may exist as an empty artefact of clone_lock, so it
    # does not count as content.
    existing = [p for p in clone.iterdir() if p.name != ".git"]
    if existing:
        raise StoreBootstrapError(
            f"refusing to initialise {clone}: it is not empty and not a git "
            f"repository ({len(existing)} entries)"
        )

    try:
        _git(clone, "init", "-q", "-b", "main")
    except subprocess.CalledProcessError as exc:
        raise StoreBootstrapError(f"git init failed for {store}: {exc.stderr}") from exc

    name = os.environ.get("MEMD_GIT_AUTHOR_NAME") or "memd service"
    email = os.environ.get("MEMD_GIT_AUTHOR_EMAIL") or "memd@example.invalid"
    _git(clone, "config", "user.name", name)
    _git(clone, "config", "user.email", email)

    ssh = ssh_command()
    if ssh:
        _git(clone, "config", "core.sshCommand", ssh)

    remote = store_remote(store)
    if remote:
        _git(clone, "remote", "add", "origin", remote, check=False)

    # An empty repository has no HEAD commit, and several read paths assume one
    # exists. Make it here rather than making every reader defensive.
    _git(clone, "commit", "--allow-empty", "-q", "-m",
         f"Initialise memory store for {store}")

    # The remote is NOT contacted. Forgejo being unreachable must not stop a
    # user having memory today; save() reports `synced: false` and the next
    # successful push catches up.
    log.info("bootstrapped store=%s clone=%s remote=%s", store, clone, remote or "(none)")
    return True
