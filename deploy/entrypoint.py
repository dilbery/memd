#!/usr/bin/env python3
"""Initialize a pilot store and attach it to its Git remote.

Never silently adopts a non-Git directory, and never overwrites remote history.

MEMD_GIT_REMOTE turns on remote wiring. Without it the behaviour is the original
local-only one. With it there are three cases:

  no local repo, empty remote        init, empty commit, push -u  (publish)
  no local repo, populated remote    clone
  local repo already exists          set url and identity, fetch, set upstream;
                                     push only when the remote has no main yet

The push in the third case publishes a store that grew before its remote existed.
A populated remote is never pushed to or pulled from here: divergent histories are
an operator decision, not something a container start should resolve.

Wiring the remote at startup matters because save()'s _pull_rebase_push runs
`git pull --rebase` before `git push`. Against a remote with no branch and no
upstream set, the pull fails, the push never runs, and every save reports
synced: false for ever.
"""
import os
import pathlib
import subprocess
import sys
from pathlib import Path

from memd.config import Config, model_api_key_from
from memd.mcp_http import load_tokens
from memd.profiles import guard_paths, locked_profile


def git(clone, *args):
    subprocess.run(['git', '-C', str(clone), *args], check=True)


def _ssh_command():
    """GIT_SSH_COMMAND for the deploy key, or None when no key is configured.

    StrictHostKeyChecking stays on: the known_hosts file is pinned at build time,
    so an unexpected host key fails the push instead of trusting it.
    """
    key = os.environ.get('MEMD_SSH_KEY', '').strip()
    if not key:
        return None
    parts = ['ssh', '-i', key, '-o', 'IdentitiesOnly=yes',
             '-o', 'StrictHostKeyChecking=yes', '-o', 'BatchMode=yes']
    known = os.environ.get('MEMD_SSH_KNOWN_HOSTS', '').strip()
    if known:
        parts += ['-o', 'UserKnownHostsFile=' + known]
    return ' '.join(parts)


def _wire_remote(clone, remote):
    """Attach the data clone to `remote`. Never overwrites remote history."""
    name = os.environ.get('MEMD_GIT_AUTHOR_NAME') or 'memd service'
    email = os.environ.get('MEMD_GIT_AUTHOR_EMAIL') or 'memd@example.invalid'
    ssh_cmd = _ssh_command()
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
    if ssh_cmd:
        env['GIT_SSH_COMMAND'] = ssh_cmd

    def run(*args, check=True):
        return subprocess.run(['git', '-C', str(clone), *args], check=check,
                              capture_output=True, text=True, env=env, timeout=120)

    listed = subprocess.run(['git', 'ls-remote', '--heads', remote, 'main'],
                            capture_output=True, text=True, env=env, timeout=120)
    if listed.returncode != 0:
        raise RuntimeError(
            'Cannot reach the memory remote {}: {}'.format(remote, listed.stderr.strip()[:300]))
    remote_has_main = bool(listed.stdout.strip())

    if not (clone / '.git').exists():
        if any(clone.iterdir()):
            raise RuntimeError('Refusing to initialize a nonempty non-Git memory directory')
        if remote_has_main:
            # `clone` exists and is empty at this point, which git clone accepts.
            # Nothing may be written into it before here or the clone refuses.
            subprocess.run(['git', 'clone', '-q', '-b', 'main', remote, str(clone)],
                           check=True, capture_output=True, text=True, env=env, timeout=300)
        else:
            git(clone, 'init', '-q', '-b', 'main')

    run('config', 'user.name', name)
    run('config', 'user.email', email)
    if ssh_cmd:
        run('config', 'core.sshCommand', ssh_cmd)

    if run('remote', 'get-url', 'origin', check=False).returncode == 0:
        run('remote', 'set-url', 'origin', remote)
    else:
        run('remote', 'add', 'origin', remote)

    if run('rev-parse', '--verify', '--quiet', 'HEAD', check=False).returncode != 0:
        run('commit', '--allow-empty', '-q', '-m', 'Initialize empty memory store')

    if remote_has_main:
        run('fetch', '-q', 'origin')
        run('branch', '--set-upstream-to=origin/main', 'main')
    else:
        run('push', '-q', '-u', 'origin', 'main')


def initialize():
    multitenant = bool((os.environ.get('MEMD_STORES_ROOT') or '').strip())
    if not multitenant and not locked_profile():
        # The lock is what confines a SINGLE-store deployment to its one store.
        # A multi-tenant deployment must NOT set it: the store follows the
        # authenticated caller, and a lock would serve every user the one locked
        # store. Its unbound callers (the onboarding job) are confined by being
        # unable to name a store they were not given.
        raise RuntimeError('MEMD_ENFORCE_PROFILE=1 is required for this deployment')
    if os.environ.get('MEMD_REQUIRE_RECALL_TOKEN') != '1':
        raise RuntimeError('MEMD_REQUIRE_RECALL_TOKEN=1 is required for this deployment')
    # Resolve the model key once at start. The multi-tenant path below never
    # builds a Config, so without this a missing MEMD_MODEL_API_KEY_FILE would
    # surface only as 401s on the first save, behind a healthy task.
    model_api_key_from(os.environ)
    from memd.registry import configured, Registry
    if configured():
        Registry().upgrade()
        # An absent/corrupt registry stops startup. Never silently re-import.
        with Registry().connection() as db:
            if db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0] != '2':
                raise RuntimeError('Unsupported token registry schema')
        tokens = {}
    else:
        tokens = load_tokens()
    oidc = bool((os.environ.get('MEMD_OIDC_ISSUER') or '').strip())
    if any(len(token) < 32 for token in tokens):
        raise RuntimeError('Configure random bearer tokens of at least 32 characters')
    if not configured() and not tokens and not oidc:
        # One of the two has to be able to authenticate somebody. OIDC alone is
        # a legitimate configuration: on a per-user deployment the static file
        # exists only for the onboarding job and admin REST.
        raise RuntimeError(
            'Configure MEMD_TOKEN/MEMD_TOKENS_FILE, or MEMD_OIDC_ISSUER, or both')

    if multitenant:
        # Nothing single-store to wire. Stores are created on first use by
        # memd.store_bootstrap, because users appear one at a time and there is no
        # startup moment at which their set is known. Preparing a clone here
        # would use MEMD_PROFILE, which on this deployment means nothing.
        root = pathlib.Path(os.environ['MEMD_STORES_ROOT']).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        print('memd: multi-tenant, stores root {} ({} existing)'.format(
            root, sum(1 for p in root.iterdir() if p.is_dir())), flush=True)
        return

    cfg = Config.from_env()
    clone, db = guard_paths(cfg.profile, cfg.clone, cfg.db)
    clone.mkdir(parents=True, exist_ok=True)
    db.parent.mkdir(parents=True, exist_ok=True)
    remote = os.environ.get('MEMD_GIT_REMOTE', '').strip()
    if remote:
        _wire_remote(clone, remote)
    elif not (clone / '.git').exists():
        if any(clone.iterdir()):
            raise RuntimeError('Refusing to initialize a nonempty non-Git memory directory')
        git(clone, 'init', '-q', '-b', 'main')
        git(clone, 'config', 'user.name',
            os.environ.get('MEMD_GIT_AUTHOR_NAME') or 'memd service')
        git(clone, 'config', 'user.email',
            os.environ.get('MEMD_GIT_AUTHOR_EMAIL') or 'memd@example.invalid')
        git(clone, 'commit', '--allow-empty', '-q', '-m', 'Initialize empty memory store')
    git(clone, 'rev-parse', '--verify', 'HEAD')


if __name__ == '__main__':
    initialize()
    if len(sys.argv) < 2:
        raise SystemExit('Missing server command')
    os.execvp(sys.argv[1], sys.argv[1:])
