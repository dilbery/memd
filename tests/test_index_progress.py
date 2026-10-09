"""Memory remains searchable while vector work is pending or fails."""
import dataclasses
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import respx

import memd.embed as embed_mod
import memd.index as index_mod
import memd.recall as recall_mod
from memd.query import distill
import memd.refresh as refresh_mod
from memd.store import StoreUnavailable, clone_lock, git_head_sha, list_notes


def fake_embed(texts, cfg):
    return [[float(i + 1)] * 768 for i, _ in enumerate(texts)]


def commit(clone):
    for args in (['add', '-A'], ['commit', '-qm', 'test changes']):
        subprocess.run(['git', '-C', str(clone), *args], check=True, capture_output=True)


def test_partial_vectors_leave_all_notes_lexically_searchable(config, monkeypatch):
    monkeypatch.setattr(index_mod, 'embed', lambda texts, cfg: [[1.0] * 768])
    db = index_mod.open_db(config.db)
    # Three whole-note texts plus each short note's single chunk, in one round.
    with pytest.raises(embed_mod.EmbedBackendError, match='expected 6'):
        index_mod.reindex(db, config)
    assert db.execute('SELECT COUNT(*) FROM notes').fetchone()[0] == 3
    assert db.execute('SELECT COUNT(*) FROM fts_notes').fetchone()[0] == 3
    assert db.execute('SELECT COUNT(*) FROM vec_notes').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM vec_chunks').fetchone()[0] == 0
    assert index_mod.pending_vectors(db) == 3
    assert index_mod.head_in_index(db) is None
    assert index_mod.lexical_head_in_index(db) == git_head_sha(config.clone)
    with pytest.raises(ValueError, match='pending'):
        index_mod.set_head_in_index(db, git_head_sha(config.clone))
    monkeypatch.setattr(index_mod, 'embed', fake_embed)
    assert index_mod.reindex(db, config) == 3
    assert index_mod.pending_vectors(db) == 0
    assert index_mod.head_in_index(db) == git_head_sha(config.clone)
    db.close()


def test_vector_response_indices_and_validation(config):
    url = config.embed_url + '/v1/embeddings'
    with respx.mock() as mock:
        route = mock.post(url).mock(return_value=httpx.Response(200, json={'data': [
            {'index': 1, 'embedding': [2.0] * 768},
            {'index': 0, 'embedding': [1.0] * 768},
        ]}))
        assert embed_mod.embed(['a', 'b'], config)[0][0] == 1.0
        route.mock(return_value=httpx.Response(200, json={'data': [
            {'index': 0, 'embedding': [1.0] * 768},
            {'index': 0, 'embedding': [2.0] * 768},
        ]}))
        with pytest.raises(embed_mod.EmbedBackendError, match='indices'):
            embed_mod.embed(['a', 'b'], config)
    for bad in (float('nan'), float('inf'), '1', True):
        with pytest.raises(embed_mod.EmbedBackendError):
            embed_mod.validate_vectors([[bad] + [1.0] * 767], 1)


def test_metadata_survives_lexical_only_recall(config, monkeypatch):
    path = config.clone / 'vmhost-proxmox-vm.md'
    path.write_text(path.read_text().replace('grounding: unverified-remote',
        'grounding: unverified-remote\ndescription: Curated summary\nsource: maintenance-log\n'
        'observed_at: 2026-09-09\nverified_at: 2026-09-09T04:00:00Z\npinned: true'))
    db = index_mod.open_db(config.db)
    with clone_lock(config.clone):
        index_mod.refresh_lexical(db, config)
    db.close()
    monkeypatch.setattr(recall_mod, 'embed_with_deadline', lambda *a, **kw: None)
    monkeypatch.setattr(recall_mod, 'rerank', lambda *a, **kw: None)
    result = recall_mod.recall('Trackr', cfg=config, include_core=False)
    note = next(n for n in result if n.slug == 'vmhost-proxmox-vm')
    assert note.grounding == 'unverified-remote'
    assert note.tags == ['proxmox', 'trackr']
    assert note.description == 'Curated summary'
    assert note.source == 'maintenance-log'
    assert note.observed_at == '2026-09-09'
    assert note.verified_at == '2026-09-09T04:00:00Z'
    assert note.pinned is True


def test_embedding_does_not_block_writer_or_apply_obsolete_vectors(config, monkeypatch):
    begun, resume = threading.Event(), threading.Event()
    def blocked_embed(texts, cfg):
        begun.set()
        assert resume.wait(5), 'writer blocked behind embedding lock'
        return fake_embed(texts, cfg)
    monkeypatch.setattr(index_mod, 'embed', blocked_embed)
    def worker():
        db = index_mod.open_db(config.db)
        try:
            return index_mod.reindex(db, config)
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        assert begun.wait(5)
        try:
            with clone_lock(config.clone):
                path = config.clone / 'vmhost-proxmox-vm.md'
                path.write_text(path.read_text() + '\nchanged-during-embed\n')
                commit(config.clone)
                db = index_mod.open_db(config.db)
                index_mod.refresh_lexical(db, config)
                db.close()
        finally:
            resume.set()
        assert future.result(timeout=5) == 2
    db = index_mod.open_db(config.db)
    assert index_mod.pending_vectors(db) == 1
    assert db.execute('SELECT COUNT(*) FROM vec_notes WHERE slug=?',
                      ('vmhost-proxmox-vm',)).fetchone()[0] == 0
    assert index_mod.head_in_index(db) is None
    db.close()


def test_old_schema_upgrade_preserves_vectors_and_hydrates_metadata(config, monkeypatch):
    monkeypatch.setattr(index_mod, 'embed', fake_embed)
    db = index_mod.open_db(config.db)
    index_mod.build_index(db, config)
    db.execute('ALTER TABLE notes DROP COLUMN metadata')
    db.execute('ALTER TABLE notes DROP COLUMN vector_blob')
    db.execute("DELETE FROM meta WHERE key='lexical_head'")
    db.commit()
    db.close()
    db = index_mod.open_db(config.db)
    assert index_mod.pending_vectors(db) == 0
    with clone_lock(config.clone):
        index_mod.refresh_lexical(db, config)
    data = json.loads(db.execute('SELECT metadata FROM notes WHERE slug=?',
                                ('vmhost-proxmox-vm',)).fetchone()[0])
    assert data['grounding'] == 'unverified-remote'
    assert data['tags'] == ['proxmox', 'trackr']
    assert db.execute('SELECT COUNT(*) FROM vec_notes').fetchone()[0] == 3
    db.close()


def test_background_requests_are_single_flight(config, monkeypatch):
    monkeypatch.setenv('MEMD_BACKGROUND_REFRESH', '1')
    started = []
    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def start(self):
            started.append(self.kwargs)
    monkeypatch.setattr(refresh_mod.threading, 'Thread', FakeThread)
    monkeypatch.setattr(refresh_mod, '_states', {})
    assert refresh_mod.request_refresh(config)
    assert not refresh_mod.request_refresh(config)
    assert len(started) == 1
    assert refresh_mod.refresh_status(config)['running'] is True


def test_scoped_search_filters_before_candidate_limits(config, monkeypatch):
    for i in range(65):
        (config.clone / f'common-{i}.md').write_text(
            f'---\ntitle: common {i}\nhost: gpuhost\n---\nbackup retention')
    (config.clone / 'rare.md').write_text(
        '---\ntitle: rare backup\nhost: Apphost\ntags: [restore]\n---\nbackup retention')
    monkeypatch.setattr(index_mod, 'embed', fake_embed)
    db = index_mod.open_db(config.db)
    index_mod.reindex(db, config)
    assert recall_mod._bm25_arm(db, distill(db, 'backup'), ['rare-backup']) == ['rare-backup']
    assert recall_mod._vector_arm(db, [1.0] * 768, ['rare-backup']) == ['rare-backup']
    db.close()
    monkeypatch.setattr(recall_mod, 'embed_with_deadline', lambda *a, **kw: [1.0] * 768)
    monkeypatch.setattr(recall_mod, 'rerank', lambda *a, **kw: None)
    result = recall_mod.recall('backup', cfg=config, host='apphost', tags=['restore'], include_core=False)
    assert [n.slug for n in result] == ['rare-backup']
    assert recall_mod.recall('backup', cfg=config, host='hass', include_core=False) == []


def test_unfinished_rebase_keeps_previous_lexical_cache(config):
    db = index_mod.open_db(config.db)
    with clone_lock(config.clone):
        index_mod.refresh_lexical(db, config)
        (config.clone / '.git' / 'rebase-merge').mkdir()
        (config.clone / 'conflict.md').write_text('---\ntitle: conflict\n---\n<<<<<<< conflict')
        with pytest.raises(StoreUnavailable, match='rebase'):
            index_mod.refresh_lexical(db, config)
    assert db.execute('SELECT COUNT(*) FROM notes').fetchone()[0] == 3
    db.close()


def test_note_rename_updates_path_without_reembedding(config, monkeypatch):
    monkeypatch.setattr(index_mod, 'embed', fake_embed)
    db = index_mod.open_db(config.db)
    index_mod.reindex(db, config)
    previous = config.clone / 'vmhost-proxmox-vm.md'
    renamed = config.clone / 'renamed.md'
    previous.rename(renamed)
    commit(config.clone)
    monkeypatch.setattr(index_mod, 'embed', lambda *a: pytest.fail('rename re-embedded unchanged content'))
    assert index_mod.reindex(db, config) == 0
    assert db.execute('SELECT path FROM notes WHERE slug=?', ('vmhost-proxmox-vm',)).fetchone()[0] == str(renamed)
    assert index_mod.pending_vectors(db) == 0
    db.close()


def test_existing_cache_reader_opens_while_another_connection_writes(config):
    writer = index_mod.open_db(config.db)
    with clone_lock(config.clone):
        index_mod.refresh_lexical(writer, config)
    writer.execute('BEGIN IMMEDIATE')
    writer.execute("UPDATE meta SET value='uncommitted' WHERE key='lexical_head'")
    try:
        reader = index_mod.open_db(config.db)
        assert index_mod.lexical_head_in_index(reader) != 'uncommitted'
        assert reader.execute('SELECT COUNT(*) FROM notes').fetchone()[0] == 3
        reader.close()
    finally:
        writer.rollback()
        writer.close()
