"""Rebuildable lexical/vector caches with independently tracked progress.

Notes/FTS and preserved provenance advance independently of embeddings. The
lexical_head marker identifies the indexed Git snapshot; head advances only when
no note has a pending vector. Each vector belongs to one exact note blob.

A note has two kinds of vector: one of its whole body (vec_notes; vector_blob),
which save's near-duplicate probe compares bodies against, and one per chunk
(vec_chunks; chunk_blob, see memd.chunk), which recall searches. Each is pending
until its blob marker matches git_blob, and pending_vectors counts either.

reindex snapshots and applies under the clone lock, releasing that lock and all
SQLite transactions during network calls. It only applies vectors whose note
blobs still match. Requests schedule this work through memd.refresh; explicit
reindex callers wait for it. Existing-schema readers do not take the writer lock.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import sqlite_vec

from memd.chunk import chunk_body, embed_text
from memd.config import Config
from memd.embed import DIM, embed, validate_vectors
from memd.query import normalize
from memd.store import assert_readable_tree, clone_lock, git_head_sha, list_notes

_TOKENCHARS = "-_.:/@"
# 2: FTS text passes through memd.query.normalize, so 'gpuhost.' and 'memd:'
# index as 'gpuhost' and 'memd'. A different stored value rebuilds fts_notes
# from the notes table (lexical only; no embedding work).
_FTS_VERSION = "2"
# 1: per-chunk vectors (memd.chunk: title + heading path + passage). A different
# stored value drops vec_chunks and marks every note's chunks pending; whole-note
# vectors, notes and FTS are kept. Bump it whenever chunk_body or embed_text
# output changes, since stored offsets and vectors describe the old chunking.
_CHUNK_VERSION = "1"
# 1: time-bounded facts (memd.facts). Facts are derived from note blobs by the
# mem-facts job, never by indexing. A different stored value drops facts and
# fact_sources, so the next mem-facts run re-extracts every note; notes, FTS
# and vectors are untouched. Bump it whenever the fact row shape or key
# normalisation (memd.facts.subject_key/predicate_key) changes.
_FACTS_VERSION = "1"
# Notes are embedded and applied in rounds of about this many texts, so a long
# first embed (every chunk of every note) keeps what it finished if it fails.
_REINDEX_BATCH_TEXTS = 128
_COLUMNS = ("slug", "path", "title", "profile", "host", "importance",
            "last_used", "superseded_by", "body", "git_blob")


def open_db(path: Path, dim: int | None = None) -> sqlite3.Connection:
    """Open (creating or migrating) an index.

    ``dim`` is the configured vector width; a different stored width rebuilds the
    vectors. None keeps the index's own width, so a caller that only reads or
    probes (and does not know the store's configured width) can never drop them.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=2.0)
    try:
        db.execute("PRAGMA busy_timeout=2000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        _ensure_schema(db, dim)
        return db
    except Exception:
        db.close()
        raise


def _ensure_schema(db: sqlite3.Connection, dim: int | None = None) -> None:
    # Ordinary WAL readers must not acquire a writer lock merely to open the
    # cache. Serialize only actual creation/migration, then recheck under lock.
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if dim is None:
        stored = (db.execute("SELECT value FROM meta WHERE key='dim'").fetchone()
                  if "meta" in tables else None)
        dim = int(stored[0]) if stored else DIM   # pre-meta.dim indexes were all 768
    if {"notes", "vec_notes", "vec_chunks", "fts_notes", "meta", "facts", "fact_sources"}.issubset(tables):
        columns = {r[1] for r in db.execute("PRAGMA table_info(notes)")}
        stored = db.execute("SELECT value FROM meta WHERE key='dim'").fetchone()
        # Databases created before meta.dim existed were all 768.
        stored_dim = int(stored[0]) if stored else DIM
        if {"metadata", "vector_blob", "chunk_blob"}.issubset(columns) and stored_dim == dim and \
                _get_meta(db, "fts_version") == _FTS_VERSION and \
                _get_meta(db, "chunk_version") == _CHUNK_VERSION and \
                _get_meta(db, "facts_version") == _FACTS_VERSION:
            return
    db.execute("BEGIN IMMEDIATE")
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS notes (
            slug TEXT PRIMARY KEY, path TEXT NOT NULL, title TEXT, profile TEXT,
            host TEXT, importance INTEGER, last_used TEXT, superseded_by TEXT,
            body TEXT NOT NULL, git_blob TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}', vector_blob TEXT, chunk_blob TEXT
        )""")
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        columns = {r[1] for r in db.execute("PRAGMA table_info(notes)")}
        if "vector_blob" not in columns:
            db.execute("ALTER TABLE notes ADD COLUMN vector_blob TEXT")
            if "vec_notes" in tables:
                db.execute("UPDATE notes SET vector_blob=git_blob WHERE slug IN "
                           "(SELECT slug FROM vec_notes)")
        if "chunk_blob" not in columns:
            # Existing whole-note vectors stay; only the chunks become pending.
            db.execute("ALTER TABLE notes ADD COLUMN chunk_blob TEXT")
        stored = db.execute("SELECT value FROM meta WHERE key='dim'").fetchone()
        stored_dim = int(stored[0]) if stored else (DIM if "vec_notes" in tables else dim)
        if "vec_notes" in tables and stored_dim != dim:
            # A vec0 table is fixed-width and its rows cannot be reinterpreted. Vectors
            # are a rebuildable cache: drop them, mark every note as needing an
            # embedding, and clear head so the next refresh re-embeds. Notes and FTS,
            # which are the authoritative and the keyword-searchable data, are untouched.
            db.execute("DROP TABLE vec_notes")
            db.execute("DROP TABLE IF EXISTS vec_chunks")
            db.execute("UPDATE notes SET vector_blob=NULL, chunk_blob=NULL")
            db.execute("DELETE FROM meta WHERE key='head'")
        if _get_meta(db, "chunk_version") != _CHUNK_VERSION:
            # Stored chunk offsets and vectors describe another chunking (or none):
            # rebuild only them. Whole-note vectors keep serving save's duplicate
            # probe and recall's fallback while the chunks are re-embedded.
            db.execute("DROP TABLE IF EXISTS vec_chunks")
            db.execute("UPDATE notes SET chunk_blob=NULL")
            db.execute("DELETE FROM meta WHERE key='head'")
            _set_meta(db, "chunk_version", _CHUNK_VERSION)
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_notes "
                   f"USING vec0(slug TEXT PRIMARY KEY, embedding FLOAT[{int(dim)}])")
        # slug is a metadata column, so a scoped KNN filters before k applies;
        # ordinal and offsets are auxiliary (returned, never filtered on).
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0("
                   f"chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{int(dim)}] distance_metric=cosine, "
                   "slug TEXT, +ordinal INTEGER, +start_char INTEGER, +end_char INTEGER)")
        _set_meta(db, "dim", str(int(dim)))
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts_notes USING fts5("
                   "slug UNINDEXED, title, body, tags, "
                   f"tokenize = \"unicode61 tokenchars '{_TOKENCHARS}'\")")
        if "metadata" not in columns:
            db.execute("ALTER TABLE notes ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
            db.execute("DELETE FROM meta WHERE key='lexical_head'")
        if _get_meta(db, "fts_version") != _FTS_VERSION:
            db.execute("DELETE FROM fts_notes")
            for slug, title, body, metadata in db.execute(
                    "SELECT slug, title, body, metadata FROM notes").fetchall():
                try:
                    tags = json.loads(metadata or "{}").get("tags") or []
                except (TypeError, ValueError, AttributeError):
                    tags = []
                _insert_fts(db, slug, title, body, tags)
            _set_meta(db, "fts_version", _FTS_VERSION)
        _ensure_facts_schema(db)


def _ensure_facts_schema(db) -> None:
    """Derived fact tables (memd.facts); caller holds the schema transaction.

    fact_sources records which blob of a note was extracted (and whether the
    chat model took part), so mem-facts re-extracts only changed notes. facts
    rows belong to exactly that blob; valid_to/closed_by are recomputed from
    all rows by memd.facts.close_facts and never edited by hand.
    """
    if _get_meta(db, "facts_version") != _FACTS_VERSION:
        db.execute("DROP TABLE IF EXISTS facts")
        db.execute("DROP TABLE IF EXISTS fact_sources")
    db.execute("""CREATE TABLE IF NOT EXISTS fact_sources (
        slug TEXT PRIMARY KEY, git_blob TEXT NOT NULL, llm INTEGER NOT NULL DEFAULT 0,
        extracted_at TEXT, error TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS facts (
        id INTEGER PRIMARY KEY, slug TEXT NOT NULL, git_blob TEXT NOT NULL,
        subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL,
        subject_key TEXT NOT NULL, predicate_key TEXT NOT NULL, object_key TEXT NOT NULL,
        valid_from TEXT, stated_to TEXT, valid_to TEXT, closed_by INTEGER,
        method TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS facts_subject ON facts(subject_key, predicate_key)")
    db.execute("CREATE INDEX IF NOT EXISTS facts_slug ON facts(slug)")
    _set_meta(db, "facts_version", _FACTS_VERSION)


def _metadata(note) -> str:
    values = dataclasses.asdict(note)
    for key in (*_COLUMNS, "matched"):
        values.pop(key, None)
    return json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)


def _insert_fts(db, slug: str, title, body, tags) -> None:
    db.execute("INSERT INTO fts_notes(slug,title,body,tags) VALUES (?,?,?,?)",
               (slug, normalize(title or ""), normalize(body or ""),
                normalize(" ".join(str(t) for t in tags or []))))


def _set_meta(db, key: str, value: str) -> None:
    db.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) "
               "DO UPDATE SET value=excluded.value", (key, value))


def _get_meta(db, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def head_in_index(db) -> str | None:
    return _get_meta(db, "head")


def lexical_head_in_index(db) -> str | None:
    return _get_meta(db, "lexical_head")


_PENDING = ("(vector_blob IS NULL OR vector_blob != git_blob "
            "OR chunk_blob IS NULL OR chunk_blob != git_blob)")


def pending_vectors(db) -> int:
    """Live notes missing a current whole-note vector or current chunk vectors."""
    return db.execute("SELECT COUNT(*) FROM notes WHERE superseded_by IS NULL "
                      f"AND {_PENDING}").fetchone()[0]


def set_head_in_index(db, head: str) -> None:
    if pending_vectors(db):
        raise ValueError("cannot mark vector index current while embeddings are pending")
    with db:
        _set_meta(db, "head", head)


def _prune_from_index(db, slug: str) -> None:
    # Facts this note's facts had closed are current again (up to their own stated
    # end) until mem-facts recomputes closing; never leave them closed by nothing.
    db.execute("UPDATE facts SET valid_to=stated_to, closed_by=NULL "
               "WHERE closed_by IN (SELECT id FROM facts WHERE slug=?) AND slug<>?", (slug, slug))
    for table in ("notes", "vec_notes", "vec_chunks", "fts_notes", "facts", "fact_sources"):
        db.execute(f"DELETE FROM {table} WHERE slug=?", (slug,))


def _upsert_lexical(db, note) -> None:
    old = db.execute("SELECT git_blob, vector_blob, chunk_blob FROM notes WHERE slug=?",
                     (note.slug,)).fetchone()
    same = bool(old) and old[0] == note.git_blob
    vector_blob = old[1] if same else None
    chunk_blob = old[2] if same else None
    if vector_blob is None:
        db.execute("DELETE FROM vec_notes WHERE slug=?", (note.slug,))
    if chunk_blob is None:
        db.execute("DELETE FROM vec_chunks WHERE slug=?", (note.slug,))
    db.execute("DELETE FROM notes WHERE slug=?", (note.slug,))
    db.execute("INSERT INTO notes(" + ",".join(_COLUMNS) + ",metadata,vector_blob,chunk_blob) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               tuple(getattr(note, key) for key in _COLUMNS)
               + (_metadata(note), vector_blob, chunk_blob))
    db.execute("DELETE FROM fts_notes WHERE slug=?", (note.slug,))
    _insert_fts(db, note.slug, note.title, note.body, note.tags)


def refresh_lexical(db, cfg: Config, *, notes=None, head: str | None = None) -> int:
    """Refresh metadata, paths and FTS without network I/O; caller holds the clone lock.

    Preserve unchanged vectors, invalidate changed blobs, and prune deleted or
    superseded notes. Refuse an unreadable or conflicted tree before changing data.
    """
    assert_readable_tree(cfg.clone)
    if notes is None:
        notes = list_notes(cfg.clone)
    if head is None:
        head = git_head_sha(cfg.clone)
    slugs = [n.slug for n in notes]
    if len(slugs) != len(set(slugs)):
        raise ValueError("duplicate note slugs in Git; resolve ownership before indexing")
    live = {n.slug: n for n in notes if not n.superseded_by}
    have = {r[0]: (r[1], r[2], r[3]) for r in db.execute(
        "SELECT slug,git_blob,metadata,path FROM notes")}
    changed = 0
    with db:
        for gone in set(have) - set(live):
            _prune_from_index(db, gone)
            changed += 1
        for slug, note in live.items():
            if have.get(slug) != (note.git_blob, _metadata(note), note.path):
                _upsert_lexical(db, note)
                changed += 1
        if head:
            _set_meta(db, "lexical_head", head)
            if not pending_vectors(db):
                _set_meta(db, "head", head)
    return changed


def _write_note_vector(db, note, vector: list[float]) -> None:
    db.execute("DELETE FROM vec_notes WHERE slug=?", (note.slug,))
    db.execute("INSERT INTO vec_notes(slug,embedding) VALUES (?,?)",
               (note.slug, sqlite_vec.serialize_float32(vector)))
    db.execute("UPDATE notes SET vector_blob=? WHERE slug=?", (note.git_blob, note.slug))


def _write_chunk_vectors(db, note, chunks, vectors: list[list[float]]) -> None:
    db.execute("DELETE FROM vec_chunks WHERE slug=?", (note.slug,))
    for chunk, vector in zip(chunks, vectors, strict=True):
        db.execute("INSERT INTO vec_chunks(embedding,slug,ordinal,start_char,end_char) "
                   "VALUES (?,?,?,?,?)",
                   (sqlite_vec.serialize_float32(vector), note.slug,
                    chunk.ordinal, chunk.start, chunk.end))
    db.execute("UPDATE notes SET chunk_blob=? WHERE slug=?", (note.git_blob, note.slug))


def _upsert_note(db, note, vector: list[float],
                 chunk_vectors: list[list[float]] | None = None) -> None:
    """Compatibility helper for callers building individual indexed notes.

    chunk_vectors, one per memd.chunk.chunk_body(note.body) chunk, also makes
    the note's chunks current; without them its chunks stay pending.
    """
    if note.superseded_by:
        _prune_from_index(db, note.slug)
        return
    dim = int(_get_meta(db, "dim") or DIM)
    validate_vectors([vector], 1, dim=dim)
    chunks = chunk_body(note.body)
    if chunk_vectors is not None:
        validate_vectors(chunk_vectors, len(chunks), dim=dim)
    _upsert_lexical(db, note)
    _write_note_vector(db, note, vector)
    if chunk_vectors is not None:
        _write_chunk_vectors(db, note, chunks, chunk_vectors)


def _rounds(plans: list[tuple]) -> list[list[tuple]]:
    """Group (note, texts, ...) plans into rounds of about _REINDEX_BATCH_TEXTS texts."""
    rounds: list[list[tuple]] = []
    size = 0
    for plan in plans:
        if not rounds or size >= _REINDEX_BATCH_TEXTS:
            rounds.append([])
            size = 0
        rounds[-1].append(plan)
        size += len(plan[1])
    return rounds


def reindex(db, cfg: Config) -> int:
    """Refresh lexical data, embed unlocked, and return the number of notes whose vectors were applied.

    Lexical changes remain committed if embedding fails. A concurrent save may
    invalidate returned vectors; skipped blobs remain pending for a later pass.
    Embedding runs in rounds of about _REINDEX_BATCH_TEXTS texts, each applied
    before the next starts, so a failure keeps the rounds already finished.
    """
    with clone_lock(cfg.clone):
        notes = list_notes(cfg.clone)
        head = git_head_sha(cfg.clone)
        refresh_lexical(db, cfg, notes=notes, head=head)
        pending = {r[0]: (r[1] != r[3], r[2] != r[3]) for r in db.execute(
            "SELECT slug, vector_blob, chunk_blob, git_blob FROM notes WHERE " + _PENDING)}
    plans = []
    for note in notes:
        if note.superseded_by or note.slug not in pending:
            continue
        need_note, need_chunks = pending[note.slug]
        chunks = chunk_body(note.body) if need_chunks else None
        texts = ([note.body] if need_note else []) + \
            [embed_text(note.title, note.body, c) for c in chunks or ()]
        plans.append((note, texts, need_note, chunks))
    applied = 0
    for batch in _rounds(plans):
        texts = [t for plan in batch for t in plan[1]]
        vectors = embed(texts, cfg)
        validate_vectors(vectors, len(texts), dim=cfg.embed_dim)
        applied += _apply_vectors(db, cfg, batch, vectors)
    return applied


def _apply_vectors(db, cfg: Config, batch: list[tuple], vectors: list[list[float]]) -> int:
    """Write one round's vectors for notes whose blob is still the one embedded."""
    applied = 0
    with clone_lock(cfg.clone):
        # A writer may have moved Git while the embedding request was in flight.
        current_head = git_head_sha(cfg.clone)
        refresh_lexical(db, cfg, head=current_head)
        with db:
            position = 0
            for note, texts, need_note, chunks in batch:
                own = vectors[position:position + len(texts)]
                position += len(texts)
                row = db.execute("SELECT git_blob FROM notes WHERE slug=?", (note.slug,)).fetchone()
                if row is None or row[0] != note.git_blob:
                    continue
                if need_note:
                    _write_note_vector(db, note, own[0])
                if chunks is not None:
                    _write_chunk_vectors(db, note, chunks, own[1:] if need_note else own)
                applied += 1
            if current_head and not pending_vectors(db):
                _set_meta(db, "head", current_head)
    return applied


def build_index(db, cfg: Config) -> None:
    reindex(db, cfg)
