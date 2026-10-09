"""Storage guarantees from a design review; all stores and failures are local."""
from pathlib import Path
import subprocess

import pytest

import memd.index as index_mod
import memd.save as save_mod
from memd.config import Config
from memd.index import open_db
from memd.store import Note, dump_note, git_head_sha, list_notes, read_note


def git(clone, *args):
    return subprocess.run(['git', '-C', str(clone), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def store(tmp_path, monkeypatch):
    clone = tmp_path / 'clone'
    clone.mkdir()
    git(clone, 'init', '-q')
    git(clone, 'config', 'user.email', 'test@memd')
    git(clone, 'config', 'user.name', 'test')
    git(clone, 'commit', '--allow-empty', '-qm', 'seed')
    monkeypatch.setattr(save_mod, '_pull_rebase_push', lambda clone: None)
    return Config(clone=clone, db=tmp_path / 'index.db', profile='amber')


def seed(cfg, filename, **fields):
    note = Note(path=filename, **fields)
    (cfg.clone / filename).write_text(dump_note(note))
    git(cfg.clone, 'add', '-A')
    git(cfg.clone, 'commit', '-qm', 'add fixture')
    return read_note(cfg.clone, note.slug)


@pytest.mark.parametrize('conflict', [False, True])
def test_new_note_cannot_overwrite_a_legacy_filename_owner(store, conflict):
    old = seed(store, 'backup.md', title='Kitchen recipes', slug='kitchen-recipes',
               body='Grandmas pastry cinnamon custard temperature')
    original = Path(old.path).read_bytes()
    result = save_mod.save({'title': 'Backup', 'body': 'Offsite snapshots thirty days',
                            'host': 'remote', 'conflict': conflict}, cfg=store)
    assert result.saved and result.action == 'created'
    assert Path(old.path).read_bytes() == original
    assert read_note(store.clone, old.slug).body == old.body
    assert read_note(store.clone, result.slug).body == 'Offsite snapshots thirty days'
    assert len(list_notes(store.clone)) == 2
    assert result.slug == 'backup'
    again = save_mod.save({'title': 'Backup', 'body': 'Offsite snapshots thirty days',
                           'host': 'remote'}, cfg=store)
    assert again.action == 'updated'
    assert again.slug == result.slug and again.path == result.path
    assert again.revision == result.revision
    assert len(list_notes(store.clone)) == 2
    assert Path(old.path).read_bytes() == original


@pytest.mark.parametrize('dangling', [False, True])
def test_new_note_never_follows_or_overwrites_a_symlink(store, tmp_path, dangling):
    outside = tmp_path / 'outside.md'
    if not dangling:
        outside.write_text('private unrelated data')
    link = store.clone / 'backup.md'
    link.symlink_to(outside)
    result = save_mod.save({'title': 'Backup', 'body': 'new backup detail',
                            'host': 'remote'}, cfg=store)
    assert result.saved
    assert link.is_symlink() and link.readlink() == outside
    if dangling:
        assert not outside.exists()
    else:
        assert outside.read_text() == 'private unrelated data'
    assert all(Path(n.path) != link for n in list_notes(store.clone))


def test_write_helper_rejects_existing_path_symlinks(store, tmp_path):
    outside = tmp_path / 'private.md'
    outside.write_text('unchanged')
    link = store.clone / 'linked.md'
    link.symlink_to(outside)
    with pytest.raises(ValueError, match='escapes|symlinks'):
        save_mod._write_note(store.clone, {
            'title': 'Linked', 'slug': 'linked', 'body': 'overwrite',
        }, path=str(link))
    assert outside.read_text() == 'unchanged'


def test_repeated_saves_work_without_embeddings_and_are_keyword_searchable(store, monkeypatch):
    def no_embeddings(*args, **kwargs):
        raise AssertionError('save must not call the embedding backend')
    monkeypatch.setattr(index_mod, 'embed', no_embeddings)
    for number in range(3):
        result = save_mod.save({'title': f'Outage note {number}',
                                'body': f'outage-unique-marker-{number}',
                                'host': 'remote'}, cfg=store)
        assert result.saved and result.synced and result.lexical_indexed
        assert not result.indexed
        assert result.revision == read_note(store.clone, result.slug).git_blob
    db = open_db(store.db)
    try:
        assert db.execute('SELECT count(*) FROM notes').fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM fts_notes WHERE fts_notes MATCH ?",
                          ('"outage-unique-marker-2"',)).fetchone()[0] == 1
    finally:
        db.close()


def test_related_notes_stay_live_and_return_a_suggestion(store):
    seed(store, 'nas_backup.md', title='NAS backup settings', slug='nas-backup-settings',
         body='NAS backup uses restic to store encrypted snapshots. Retention is 90 days. '
              'Encryption recovery key is stored in the paper safe.')
    result = save_mod.save({'title': 'Laptop backup settings',
                            'body': 'Laptop backup uses restic to store encrypted snapshots. '
                                    'Retention is 30 days.', 'host': 'remote'}, cfg=store)
    assert result.action == 'created'
    assert result.related == ['nas-backup-settings']
    assert read_note(store.clone, 'nas-backup-settings').superseded_by is None
    db = open_db(store.db)
    try:
        assert db.execute('SELECT count(*) FROM notes').fetchone()[0] == 2
        assert db.execute("SELECT slug FROM fts_notes WHERE fts_notes MATCH 'paper'").fetchone()[0] == 'nas-backup-settings'
    finally:
        db.close()


def test_explicit_supersedes_is_deliberate_and_keeps_old_metadata(store):
    old = seed(store, 'nas.md', title='NAS retention', slug='nas-retention',
               body='Retention is 90 days', source='manual inspection', pinned=True,
               observed_at='2026-09-08', metadata={'type': 'project'})
    result = save_mod.save({'title': 'NAS retention corrected', 'body': 'Retention is 30 days',
                            'supersedes': old.slug, 'expected_revision': old.git_blob,
                            'host': 'remote'}, cfg=store)
    assert result.action == 'superseded'
    retained = read_note(store.clone, old.slug)
    assert retained.superseded_by == result.slug
    assert retained.body == old.body and retained.source == old.source
    assert retained.pinned and retained.metadata == old.metadata


def test_update_preserves_metadata_and_checks_the_current_note_revision(store):
    old = seed(store, 'legacy.md', title='Old display title', slug='stable-identity',
               body='Original body', source='physical inspection', pinned=True,
               observed_at='2026-09-08', verified_at='2026-09-09',
               last_used='2026-09-09T00:00:00Z', importance=5, tags=['nas'],
               description='Curated summary', metadata={'type': 'project', 'metadata': {'source': 'hermes'}})
    result = save_mod.save({'slug': old.slug, 'title': 'New display title',
                            'body': 'New body', 'expected_revision': old.git_blob}, cfg=store)
    new = read_note(store.clone, old.slug)
    assert result.action == 'updated' and result.slug == old.slug
    assert new.title == 'New display title' and new.path == old.path
    for key in ('source', 'pinned', 'observed_at', 'verified_at', 'last_used',
                'importance', 'tags', 'description', 'metadata', 'host'):
        assert getattr(new, key) == getattr(old, key)
    assert result.revision != old.git_blob
    head = git_head_sha(store.clone)
    with pytest.raises(save_mod.RevisionConflict):
        save_mod.save({'slug': old.slug, 'body': 'stale overwrite',
                        'expected_revision': old.git_blob}, cfg=store)
    assert git_head_sha(store.clone) == head
    assert read_note(store.clone, old.slug).body == 'New body'


def test_remote_sync_failure_keeps_local_saves_and_reports_pending(store, monkeypatch):
    def offline(clone):
        raise subprocess.CalledProcessError(128, ['git', 'push'], stderr='offline')
    monkeypatch.setattr(save_mod, '_pull_rebase_push', offline)
    for number in range(2):
        result = save_mod.save({'title': f'Sync outage {number}', 'body': f'durable {number}',
                                'host': 'remote'}, cfg=store)
        assert result.saved and not result.synced and result.lexical_indexed
        assert any('remote sync pending' in warning for warning in result.warnings)
        assert read_note(store.clone, result.slug) is not None
    assert git(store.clone, 'status', '--porcelain') == ''


def test_same_title_tombstone_cannot_be_overwritten(store):
    old = seed(store, 'old.md', title='Old', slug='old', body='Historical evidence',
               superseded_by='replacement')
    before = Path(old.path).read_bytes()
    result = save_mod.save({'title': 'Old', 'body': 'Unrelated fresh note',
                            'host': 'remote'}, cfg=store)
    assert result.slug != old.slug and result.action == 'created'
    assert Path(old.path).read_bytes() == before


def test_supersede_rolls_back_both_files_when_second_write_fails(store, monkeypatch):
    old = seed(store, 'old.md', title='Old', slug='old', body='Original evidence',
               metadata={'type': 'project'})
    before = Path(old.path).read_bytes()
    before_head = git_head_sha(store.clone)
    write_note = save_mod._write_note
    def failing_write(clone, note, *, path=None, create=False):
        result = write_note(clone, note, path=path, create=create)
        if path == old.path:
            raise OSError('simulated fsync failure after replacement')
        return result
    monkeypatch.setattr(save_mod, '_write_note', failing_write)
    with pytest.raises(OSError, match='simulated fsync'):
        save_mod.save({'title': 'Replacement', 'body': 'New evidence',
                        'supersedes': old.slug, 'host': 'remote'}, cfg=store)
    assert Path(old.path).read_bytes() == before
    assert len(list_notes(store.clone)) == 1
    assert git_head_sha(store.clone) == before_head
    assert git(store.clone, 'status', '--porcelain') == ''


@pytest.mark.parametrize('supersede', [False, True])
def test_commit_failure_restores_notes_and_preexisting_staging(store, monkeypatch, supersede):
    old = seed(store, 'old.md', title='Old', slug='old', body='Original evidence')
    before = Path(old.path).read_bytes()
    unrelated = store.clone / 'operator.txt'
    unrelated.write_text('keep staged by operator')
    git(store.clone, 'add', '--', unrelated.name)
    before_status = git(store.clone, 'status', '--porcelain')
    before_head = git_head_sha(store.clone)
    def failing_commit(clone, message, *, paths):
        git(clone, 'add', '--', *[str(p.relative_to(clone)) for p in paths])
        raise subprocess.CalledProcessError(1, ['git', 'commit'], stderr='simulated hook rejection')
    monkeypatch.setattr(save_mod, '_commit', failing_commit)
    fact = {'title': 'Old', 'body': 'New evidence', 'host': 'remote'}
    if supersede:
        fact.update(title='Replacement', supersedes='old')
    with pytest.raises(subprocess.CalledProcessError):
        save_mod.save(fact, cfg=store)
    assert Path(old.path).read_bytes() == before
    assert len(list_notes(store.clone)) == 1
    assert git_head_sha(store.clone) == before_head
    assert git(store.clone, 'status', '--porcelain') == before_status


def test_save_commits_only_its_note_and_preserves_operator_staging(store):
    unrelated = store.clone / 'operator.txt'
    unrelated.write_text('not part of a memory save')
    git(store.clone, 'add', '--', unrelated.name)
    result = save_mod.save({'title': 'Saved note', 'body': 'Memory content',
                            'host': 'remote'}, cfg=store)
    assert result.saved
    assert git(store.clone, 'diff', '--cached', '--name-only') == 'operator.txt'
    assert git(store.clone, 'show', '--format=', '--name-only', 'HEAD') == 'saved_note.md'


def test_local_git_conflict_aborts_rebase_and_keeps_the_saved_revision(store, tmp_path, monkeypatch):
    old = seed(store, 'old.md', title='Old', slug='old', body='Original body')
    remote = tmp_path / 'remote.git'
    git(store.clone, 'init', '--bare', '-q', str(remote))
    branch = git(store.clone, 'branch', '--show-current')
    git(remote, 'symbolic-ref', 'HEAD', f'refs/heads/{branch}')
    git(store.clone, 'remote', 'add', 'origin', str(remote))
    git(store.clone, 'push', '-u', 'origin', 'HEAD')
    peer = tmp_path / 'peer'
    subprocess.run(['git', 'clone', '-q', str(remote), str(peer)], check=True, capture_output=True)
    git(peer, 'config', 'user.email', 'peer@memd')
    git(peer, 'config', 'user.name', 'peer')
    (peer / 'old.md').write_text(dump_note(Note(title='Old', slug='old', path='old.md', body='Remote body')))
    git(peer, 'add', '-A')
    git(peer, 'commit', '-qm', 'remote change')
    git(peer, 'push')

    def real_local_sync(clone):
        git(clone, 'pull', '--rebase', '--autostash')
        git(clone, 'push')
    monkeypatch.setattr(save_mod, '_pull_rebase_push', real_local_sync)
    result = save_mod.save({'title': 'Old', 'body': 'Local body', 'host': 'remote',
                            'expected_revision': old.git_blob}, cfg=store)
    assert result.saved and not result.synced
    assert result.lexical_indexed
    assert read_note(store.clone, old.slug).body == 'Local body'
    assert result.revision == read_note(store.clone, old.slug).git_blob
    assert not (store.clone / '.git' / 'rebase-merge').exists()
    assert not (store.clone / '.git' / 'rebase-apply').exists()
    assert git(store.clone, 'status', '--porcelain') == ''


@pytest.mark.parametrize('supersede', [False, True])
def test_commit_timeout_after_head_moves_preserves_committed_save(store, monkeypatch, supersede):
    """A post-commit timeout must never restore old bytes over the new commit."""
    old = seed(store, 'old.md', title='Old', slug='old', body='Original evidence')
    real_commit = save_mod._commit
    before_head = git_head_sha(store.clone)
    def committed_then_timed_out(clone, message, *, paths):
        real_commit(clone, message, paths=paths)
        raise subprocess.TimeoutExpired(['git', 'commit'], timeout=10)
    monkeypatch.setattr(save_mod, '_commit', committed_then_timed_out)
    fact = {'title': 'Old', 'body': 'Durable replacement', 'host': 'remote'}
    if supersede:
        fact.update(title='Replacement', supersedes=old.slug)
    result = save_mod.save(fact, cfg=store)
    assert result.saved and result.synced and result.lexical_indexed
    assert git_head_sha(store.clone) != before_head
    assert read_note(store.clone, result.slug).body == 'Durable replacement'
    if supersede:
        assert read_note(store.clone, old.slug).superseded_by == result.slug
    assert git(store.clone, 'status', '--porcelain') == ''
    assert any('Saved commit verified' in warning for warning in result.warnings)


def test_unknown_commit_outcome_does_not_rollback_changed_worktree(store, monkeypatch):
    old = seed(store, 'old.md', title='Old', slug='old', body='Original evidence')
    real_commit = save_mod._commit
    def ambiguous_commit(clone, message, *, paths):
        real_commit(clone, message, paths=paths)
        paths[0].write_text(paths[0].read_text() + '\nPost-commit hook edit\n')
        raise subprocess.TimeoutExpired(['git', 'commit'], timeout=10)
    monkeypatch.setattr(save_mod, '_commit', ambiguous_commit)
    with pytest.raises(save_mod.CommitOutcomeUnknown, match='files were preserved'):
        save_mod.save({'slug': old.slug, 'body': 'Durable replacement'}, cfg=store)
    assert 'Durable replacement' in git(store.clone, 'show', 'HEAD:old.md')
    assert 'Post-commit hook edit' in Path(old.path).read_text()


def test_unrelated_head_move_cannot_falsely_confirm_an_untracked_note(store, monkeypatch):
    def unrelated_commit(clone, message, *, paths):
        marker = clone / 'unrelated.txt'
        marker.write_text('another process committed this')
        git(clone, 'add', '--', marker.name)
        git(clone, 'commit', '--only', '-qm', 'unrelated commit', '--', marker.name)
        raise subprocess.TimeoutExpired(['git', 'commit'], timeout=10)
    monkeypatch.setattr(save_mod, '_commit', unrelated_commit)
    with pytest.raises(save_mod.CommitOutcomeUnknown, match='files were preserved'):
        save_mod.save({'title': 'New untracked note', 'body': 'Not committed'}, cfg=store)
    assert 'new_untracked_note.md' not in git(store.clone, 'ls-tree', '--name-only', 'HEAD')
    assert (store.clone / 'new_untracked_note.md').exists()


def test_explicit_id_is_preserved_when_only_its_filename_is_taken(store):
    old = seed(store, 'stable_id.md', title='Original unrelated note',
               slug='unrelated', body='Keep me')
    result = save_mod.save({'slug': 'stable-id', 'title': 'Readable label',
                            'body': 'New evidence', 'host': 'remote'}, cfg=store)
    assert result.slug == 'stable-id'
    assert result.path != old.path
    again = save_mod.save({'slug': 'stable-id', 'body': 'Revised evidence',
                           'expected_revision': result.revision, 'host': 'remote'}, cfg=store)
    assert again.slug == result.slug and again.path == result.path
    assert read_note(store.clone, old.slug).body == 'Keep me'


@pytest.mark.parametrize('state', ['rebase-merge', 'rebase-apply'])
def test_save_refuses_a_rebase_tree_before_writing(store, state):
    from memd.store import StoreUnavailable, assert_readable_tree
    (store.clone / '.git' / state).mkdir()
    before_head = git_head_sha(store.clone)
    with pytest.raises(StoreUnavailable, match='unfinished rebase'):
        assert_readable_tree(store.clone)
    with pytest.raises(StoreUnavailable):
        save_mod.save({'title': 'Unsafe snapshot', 'body': 'must not be written'}, cfg=store)
    assert git_head_sha(store.clone) == before_head
    assert not list(store.clone.glob('*.md'))


def test_save_refuses_an_unmerged_tree_even_without_a_rebase_marker(store):
    from memd.store import StoreUnavailable, assert_readable_tree
    seed(store, 'fact.md', title='Fact', slug='fact', body='Original body')
    branch = git(store.clone, 'branch', '--show-current')
    git(store.clone, 'checkout', '-qb', 'peer')
    (store.clone / 'fact.md').write_text('peer version\n')
    git(store.clone, 'commit', '-am', 'peer change')
    git(store.clone, 'checkout', branch)
    (store.clone / 'fact.md').write_text('local version\n')
    git(store.clone, 'commit', '-am', 'local change')
    merge = subprocess.run(['git', '-C', str(store.clone), 'merge', 'peer'],
                           capture_output=True, text=True)
    assert merge.returncode == 1
    assert not (store.clone / '.git' / 'rebase-merge').exists()
    conflicted = (store.clone / 'fact.md').read_bytes()
    with pytest.raises(StoreUnavailable, match='unresolved merge or autostash conflicts'):
        assert_readable_tree(store.clone)
    with pytest.raises(StoreUnavailable):
        save_mod.save({'title': 'Unsafe snapshot', 'body': 'must not be written'}, cfg=store)
    assert (store.clone / 'fact.md').read_bytes() == conflicted
    assert not (store.clone / 'unsafe_snapshot.md').exists()
