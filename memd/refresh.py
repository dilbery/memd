"""Background vector refresh, deduplicated per profile and across processes."""
from __future__ import annotations

import dataclasses
import fcntl
import logging
import os
import threading
import time

from memd.config import Config
from memd.index import open_db, pending_vectors, refresh_lexical, reindex
from memd.profiles import guard_paths
from memd.store import clone_lock

log = logging.getLogger(__name__)
_lock = threading.Lock()
_states: dict[tuple[str, str], dict] = {}
_RETRY_SECONDS = 30.0


def _key(cfg):
    return cfg.profile, str(cfg.db)


def _guard(cfg):
    clone, db = guard_paths(cfg.profile, cfg.clone, cfg.db)
    return dataclasses.replace(cfg, clone=clone, db=db)


def ensure_lexical(cfg: Config, *, blocking: bool = True) -> int:
    cfg = _guard(cfg)
    if not (cfg.clone / '.git').exists():
        raise FileNotFoundError(f'memory clone unavailable: {cfg.clone}')
    with clone_lock(cfg.clone, blocking=blocking):
        db = open_db(cfg.db, dim=cfg.embed_dim)
        try:
            return refresh_lexical(db, cfg)
        finally:
            db.close()


def refresh_status(cfg: Config) -> dict:
    with _lock:
        state = _states.get(_key(cfg), {})
        return {'running': bool(state.get('running')), 'error': state.get('error'),
                'last_completed': state.get('last_completed')}


def _worker(cfg: Config) -> None:
    key = _key(cfg)
    error = None
    retry_needed = False
    try:
        # The process-local guard avoids threads piling up. The file lock also
        # covers multiple stdio servers using the same disposable cache.
        path = cfg.clone / '.git' / 'memd-index.lock'
        with path.open('a') as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                retry_needed = True
                return
            db = open_db(cfg.db, dim=cfg.embed_dim)
            try:
                reindex(db, cfg)
                retry_needed = pending_vectors(db) > 0
            finally:
                db.close()
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        retry_needed = True
        log.warning('vector refresh deferred for %s: %s', cfg.profile, error)
    finally:
        with _lock:
            # A save can arrive after the last pending_vectors read but before
            # running is cleared. Preserve that request instead of losing its
            # only wakeup until another recall happens to request a refresh.
            requested = bool(_states.get(key, {}).get('requested'))
            delay = _RETRY_SECONDS if retry_needed else 0
            retry_needed = retry_needed or requested
            _states[key] = {'running': False, 'error': error,
                            'requested': False,
                            'last_completed': time.time(),
                            'next_attempt': time.monotonic() + delay}
        if retry_needed:
            timer = threading.Timer(delay, request_refresh, args=(cfg,))
            timer.daemon = True
            timer.start()


def request_refresh(cfg: Config) -> bool:
    if os.environ.get('MEMD_BACKGROUND_REFRESH', '1').strip().lower() in {'0', 'false', 'off', 'no'}:
        return False
    cfg = _guard(cfg)
    if not (cfg.clone / '.git').exists():
        return False
    key = _key(cfg)
    with _lock:
        state = _states.get(key, {})
        if state.get('running'):
            _states[key] = {**state, 'requested': True}
            return False
        if time.monotonic() < state.get('next_attempt', 0):
            return False
        _states[key] = {**state, 'running': True}
    worker = threading.Thread(target=_worker, args=(cfg,), name=f'memd-index-{cfg.profile}', daemon=True)
    worker.start()
    return True
