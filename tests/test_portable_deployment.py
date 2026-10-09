"""Portable startup must preserve data and refuse insecure launch settings."""
import importlib.util
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def startup(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        'portable_entrypoint', Path(__file__).parents[1] / 'deploy/entrypoint.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv('MEMD_PROFILE', 'amber')
    monkeypatch.setenv('MEMD_ENFORCE_PROFILE', '1')
    monkeypatch.setenv('MEMD_REQUIRE_RECALL_TOKEN', '1')
    monkeypatch.setenv('MEMD_TOKEN', 'synthetic-test-token-' + 'x' * 32)
    monkeypatch.setenv('MEMD_TOKENS_FILE', str(tmp_path / 'absent-tokens'))
    clone = tmp_path / 'data/clone'
    monkeypatch.setenv('MEMD_CLONE', str(clone))
    monkeypatch.setenv('MEMD_DB', str(tmp_path / 'data/index.db'))
    return module, clone


def test_initializes_empty_store_and_preserves_it_on_restart(startup):
    module, clone = startup
    module.initialize()
    def head():
        return subprocess.check_output(['git', '-C', str(clone), 'rev-parse', 'HEAD'])
    before = head()
    (clone / 'existing.md').write_text('Existing note must survive restart.\n')
    module.initialize()
    assert head() == before
    assert (clone / 'existing.md').read_text() == 'Existing note must survive restart.\n'


@pytest.mark.parametrize('variable,value', [
    ('MEMD_ENFORCE_PROFILE', '0'),
    ('MEMD_REQUIRE_RECALL_TOKEN', '0'),
    ('MEMD_TOKEN', ''),
    ('MEMD_TOKEN', 'too-short'),
])
def test_rejects_insecure_startup_before_creating_data(startup, monkeypatch, variable, value):
    module, clone = startup
    monkeypatch.setenv(variable, value)
    with pytest.raises(RuntimeError):
        module.initialize()
    assert not clone.exists()


def test_refuses_to_adopt_unversioned_existing_directory(startup):
    module, clone = startup
    clone.mkdir(parents=True)
    (clone / 'existing.md').write_text('Do not adopt or overwrite me.\n')
    with pytest.raises(RuntimeError, match='nonempty'):
        module.initialize()
    assert not (clone / '.git').exists()
    assert (clone / 'existing.md').read_text() == 'Do not adopt or overwrite me.\n'


@pytest.mark.parametrize('has_unindexed_note', [False, True])
def test_empty_store_is_usable_but_missing_index_data_is_not(startup, monkeypatch, has_unindexed_note):
    from memd import server, refresh
    from memd.config import Config
    module, clone = startup
    module.initialize()
    cfg = Config.from_env()
    refresh.ensure_lexical(cfg)
    if has_unindexed_note:
        (clone / 'new.md').write_text('---\ntitle: New note\nslug: new\n---\nMissing from index.\n')
    monkeypatch.setattr(server, '_health_cache', None)
    monkeypatch.setattr(server, '_cfg', lambda: cfg)
    monkeypatch.setattr(server, 'embed_with_deadline', lambda *a, **kw: None)
    monkeypatch.setattr(server, 'rerank', lambda *a, **kw: None)
    result = server._health()
    assert result['checks']['index']['ok'] is not has_unindexed_note
    assert result['status'] == ('down' if has_unindexed_note else 'degraded')


# ---------------------------------------------------------------------------
# Remote wiring. save()'s _pull_rebase_push runs `git pull --rebase` before
# `git push`; against a remote with no branch and no upstream the pull fails,
# the push never runs, and every save reports synced: false for ever.
# ---------------------------------------------------------------------------


def _bare(tmp_path, name, seed=False):
    bare = tmp_path / name
    subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(bare)], check=True)
    if seed:
        work = tmp_path / (name + '-seed')
        subprocess.run(['git', 'clone', '-q', str(bare), str(work)], check=True)
        (work / 'seeded.md').write_text(
            '---\ntitle: Seeded\nslug: seeded\n---\nfrom the remote\n')
        subprocess.run(['git', '-C', str(work), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(work), '-c', 'user.name=s', '-c', 'user.email=s@s',
                        'commit', '-q', '-m', 'seed'], check=True)
        subprocess.run(['git', '-C', str(work), 'push', '-q', '-u', 'origin', 'main'], check=True)
    return bare


def _git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_remote_empty_local_empty_publishes_initial_commit(startup, monkeypatch, tmp_path):
    module, clone = startup
    bare = _bare(tmp_path, 'remote.git')
    monkeypatch.setenv('MEMD_GIT_REMOTE', 'file://' + str(bare))
    monkeypatch.setenv('MEMD_GIT_AUTHOR_NAME', 'memd pilot')
    monkeypatch.setenv('MEMD_GIT_AUTHOR_EMAIL', 'memd@example.test')
    module.initialize()
    assert _git(clone, 'remote', 'get-url', 'origin') == 'file://' + str(bare)
    assert _git(clone, 'config', 'user.email') == 'memd@example.test'
    assert _git(clone, 'rev-parse', '--abbrev-ref', 'main@{upstream}') == 'origin/main'
    assert _git(bare, 'rev-parse', 'main') == _git(clone, 'rev-parse', 'main')


def test_remote_populated_local_empty_clones(startup, monkeypatch, tmp_path):
    module, clone = startup
    bare = _bare(tmp_path, 'remote.git', seed=True)
    monkeypatch.setenv('MEMD_GIT_REMOTE', 'file://' + str(bare))
    module.initialize()
    assert (clone / 'seeded.md').exists()
    assert _git(clone, 'rev-parse', '--abbrev-ref', 'main@{upstream}') == 'origin/main'


def test_existing_local_publishes_to_empty_remote(startup, monkeypatch, tmp_path):
    """A store that grew before its remote existed is published, never discarded."""
    module, clone = startup
    module.initialize()
    (clone / 'local.md').write_text('local only\n')
    subprocess.run(['git', '-C', str(clone), 'add', '-A'], check=True)
    subprocess.run(['git', '-C', str(clone), 'commit', '-q', '-m', 'local'], check=True)
    bare = _bare(tmp_path, 'remote.git')
    monkeypatch.setenv('MEMD_GIT_REMOTE', 'file://' + str(bare))
    module.initialize()
    assert (clone / 'local.md').exists()
    assert _git(bare, 'rev-parse', 'main') == _git(clone, 'rev-parse', 'main')


def test_existing_local_and_populated_remote_sets_upstream_only(startup, monkeypatch, tmp_path):
    """Divergent histories are an operator decision: nothing is pushed, pulled or reset."""
    module, clone = startup
    module.initialize()
    (clone / 'local.md').write_text('local only\n')
    subprocess.run(['git', '-C', str(clone), 'add', '-A'], check=True)
    subprocess.run(['git', '-C', str(clone), 'commit', '-q', '-m', 'local'], check=True)
    bare = _bare(tmp_path, 'remote.git', seed=True)
    remote_head = _git(bare, 'rev-parse', 'main')
    local_head = _git(clone, 'rev-parse', 'main')
    monkeypatch.setenv('MEMD_GIT_REMOTE', 'file://' + str(bare))
    module.initialize()
    assert _git(clone, 'rev-parse', '--abbrev-ref', 'main@{upstream}') == 'origin/main'
    assert _git(bare, 'rev-parse', 'main') == remote_head
    assert _git(clone, 'rev-parse', 'main') == local_head
    assert (clone / 'local.md').exists() and not (clone / 'seeded.md').exists()


def test_ssh_key_sets_a_strict_ssh_command(startup, monkeypatch, tmp_path):
    module, clone = startup
    bare = _bare(tmp_path, 'remote.git')
    key = tmp_path / 'id'
    key.write_text('not a real key\n')
    key.chmod(0o600)
    known = tmp_path / 'known_hosts'
    known.write_text('\n')
    monkeypatch.setenv('MEMD_GIT_REMOTE', 'file://' + str(bare))
    monkeypatch.setenv('MEMD_SSH_KEY', str(key))
    monkeypatch.setenv('MEMD_SSH_KNOWN_HOSTS', str(known))
    module.initialize()
    command = _git(clone, 'config', 'core.sshCommand')
    assert str(key) in command
    assert str(known) in command
    assert 'StrictHostKeyChecking=yes' in command


def test_unreachable_remote_is_a_clear_error_not_a_silent_local_store(startup, monkeypatch, tmp_path):
    module, clone = startup
    monkeypatch.setenv('MEMD_GIT_REMOTE', 'file://' + str(tmp_path / 'does-not-exist.git'))
    with pytest.raises(RuntimeError, match='Cannot reach the memory remote'):
        module.initialize()


def test_without_a_remote_behaviour_is_unchanged(startup, monkeypatch):
    module, clone = startup
    monkeypatch.delenv('MEMD_GIT_REMOTE', raising=False)
    module.initialize()
    assert (clone / '.git').exists()
    assert subprocess.run(['git', '-C', str(clone), 'remote', 'get-url', 'origin'],
                          capture_output=True).returncode != 0
