"""A committed save cannot lose its refresh request during worker completion."""
import subprocess

import httpx
import pytest
import respx

import memd.embed as embed_mod
import memd.index as index_mod
import memd.recall as recall_mod
import memd.refresh as refresh_mod
from memd.store import clone_lock


@pytest.mark.parametrize('bad_response', [
    httpx.Response(200, json={'data': [{'embedding': [0.1] * 512}]}),
    httpx.Response(200, text='proxy returned invalid JSON'),
    httpx.Response(200, json={'data': [{'embedding': None}]}),
])
def test_bad_query_embeddings_preserve_keyword_recall(config, monkeypatch, bad_response):
    refresh_mod.ensure_lexical(config)
    monkeypatch.setattr(recall_mod, 'rerank', lambda *a, **kw: None)
    with respx.mock() as mocked:
        mocked.post(config.embed_url + '/v1/embeddings').mock(return_value=bad_response)
        assert embed_mod.embed_with_deadline('Trackr', cfg=config) is None
        found = recall_mod.recall('Trackr', cfg=config, include_core=False)
    assert any(note.slug == 'vmhost-proxmox-vm' for note in found)


def test_save_request_during_completion_schedules_followup(config, monkeypatch):
    monkeypatch.setenv('MEMD_BACKGROUND_REFRESH', '1')
    monkeypatch.setattr(index_mod, 'embed', lambda texts, cfg: [[1.0] * 768 for _ in texts])
    monkeypatch.setattr(refresh_mod, '_states', {refresh_mod._key(config): {'running': True}})
    actual_pending = index_mod.pending_vectors
    timers = []

    class CapturedTimer:
        def __init__(self, interval, function, args):
            self.interval, self.function, self.args = interval, function, args
            self.daemon = False
        def start(self):
            timers.append(self)

    monkeypatch.setattr(refresh_mod.threading, 'Timer', CapturedTimer)

    def pending_then_save(db):
        # The worker already finished its embedding batch and observed no work.
        observed = actual_pending(db)
        assert observed == 0
        with clone_lock(config.clone):
            note = config.clone / 'vmhost-proxmox-vm.md'
            note.write_text(note.read_text() + '\nnew content after the final pending check\n')
            for args in (['add', '-A'], ['commit', '-qm', 'save in completion gap']):
                subprocess.run(['git', '-C', str(config.clone), *args], check=True, capture_output=True)
            writer = index_mod.open_db(config.db)
            try:
                index_mod.refresh_lexical(writer, config)
            finally:
                writer.close()
            # Equivalent to save's post-commit scheduling while worker is running.
            assert not refresh_mod.request_refresh(config)
        return observed

    monkeypatch.setattr(refresh_mod, 'pending_vectors', pending_then_save)
    refresh_mod._worker(config)
    assert len(timers) == 1
    assert timers[0].interval == 0
    assert timers[0].daemon is True
    db = index_mod.open_db(config.db)
    assert actual_pending(db) == 1
    db.close()

    # Execute the captured followup synchronously without real background threads.
    monkeypatch.setattr(refresh_mod, 'pending_vectors', actual_pending)
    refresh_mod._worker(config)
    db = index_mod.open_db(config.db)
    assert actual_pending(db) == 0
    db.close()
    assert not refresh_mod.refresh_status(config)['running']
