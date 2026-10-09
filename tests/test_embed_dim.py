"""The embedding dimension is configuration, and changing it rebuilds the vector cache."""
import sqlite_vec

import pytest

from memd.config import Config
from memd.embed import DIM, CanaryError, validate_vectors
from memd.index import _upsert_lexical, open_db, pending_vectors
from memd.store import Note


def test_default_dim_is_768_and_env_overrides():
    assert Config().embed_dim == 768 == DIM
    assert Config.from_env({"MEMD_EMBED_DIM": "2560"}, env_file=None).embed_dim == 2560


def test_garbage_and_out_of_range_fall_back_to_the_default():
    assert Config.from_env({"MEMD_EMBED_DIM": "nonsense"}, env_file=None).embed_dim == 768
    assert Config.from_env({"MEMD_EMBED_DIM": "0"}, env_file=None).embed_dim == 768
    assert Config.from_env({"MEMD_EMBED_DIM": "999999"}, env_file=None).embed_dim == 768


def test_validate_vectors_uses_the_given_dim():
    validate_vectors([[0.0] * 2560], 1, dim=2560)
    with pytest.raises(CanaryError):
        validate_vectors([[0.0] * 768], 1, dim=2560)


def test_validate_vectors_still_defaults_to_768():
    validate_vectors([[0.0] * 768], 1)
    with pytest.raises(CanaryError):
        validate_vectors([[0.0] * 2560], 1)


def test_open_db_creates_the_table_at_the_requested_dim(tmp_path):
    db = open_db(tmp_path / "m.db", dim=2560)
    db.execute("INSERT INTO vec_notes(slug, embedding) VALUES (?, ?)",
               ("a", sqlite_vec.serialize_float32([1.0] * 2560)))
    assert db.execute("SELECT value FROM meta WHERE key='dim'").fetchone()[0] == "2560"
    db.close()


def _seed(db, blob="blob1", dim=768):
    # A fully embedded note has a whole-note vector and its chunk vectors.
    note = Note(title="t", slug="t", path="t.md", body="b", git_blob=blob)
    with db:
        _upsert_lexical(db, note)
        db.execute("INSERT INTO vec_notes(slug, embedding) VALUES (?, ?)",
                   ("t", sqlite_vec.serialize_float32([1.0] * dim)))
        db.execute("INSERT INTO vec_chunks(embedding, slug, ordinal, start_char, end_char) "
                   "VALUES (?, 't', 0, 0, 1)", (sqlite_vec.serialize_float32([1.0] * dim),))
        db.execute("UPDATE notes SET vector_blob=git_blob, chunk_blob=git_blob WHERE slug='t'")
        db.execute("INSERT INTO meta(key,value) VALUES ('head','abc')")


def test_dim_change_rebuilds_vectors_and_keeps_notes(tmp_path):
    path = tmp_path / "m.db"
    db = open_db(path, dim=768)
    _seed(db)
    assert pending_vectors(db) == 0
    db.close()

    db = open_db(path, dim=2560)
    assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM fts_notes").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM vec_notes").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 0
    assert pending_vectors(db) == 1
    assert db.execute("SELECT value FROM meta WHERE key='head'").fetchone() is None
    db.execute("INSERT INTO vec_notes(slug, embedding) VALUES (?, ?)",
               ("t", sqlite_vec.serialize_float32([1.0] * 2560)))
    db.execute("INSERT INTO vec_chunks(embedding, slug, ordinal, start_char, end_char) "
               "VALUES (?, 't', 0, 0, 1)", (sqlite_vec.serialize_float32([1.0] * 2560),))
    db.close()


def test_reopening_at_the_same_dim_is_a_no_op(tmp_path):
    path = tmp_path / "m.db"
    db = open_db(path, dim=2560)
    with db:
        db.execute("INSERT INTO meta(key,value) VALUES ('head','keep')")
    db.close()
    db = open_db(path, dim=2560)
    assert db.execute("SELECT value FROM meta WHERE key='head'").fetchone()[0] == "keep"
    db.close()


def test_a_pre_guard_database_is_treated_as_768(tmp_path):
    """Databases created before meta.dim existed were all 768; reopening at 768 keeps them."""
    path = tmp_path / "m.db"
    db = open_db(path, dim=768)
    _seed(db)
    with db:
        db.execute("DELETE FROM meta WHERE key='dim'")
    db.close()
    db = open_db(path, dim=768)
    assert db.execute("SELECT COUNT(*) FROM vec_notes").fetchone()[0] == 1
    assert db.execute("SELECT value FROM meta WHERE key='head'").fetchone()[0] == "abc"
    db.close()


def test_opening_without_a_width_keeps_the_stored_one(tmp_path):
    """A reader that does not know the store's width must never drop its vectors."""
    path = tmp_path / "m.db"
    db = open_db(path, dim=16)
    _seed(db, dim=16)
    db.close()
    db = open_db(path)
    assert db.execute("SELECT COUNT(*) FROM vec_notes").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 1
    assert db.execute("SELECT value FROM meta WHERE key='head'").fetchone()[0] == "abc"
    assert pending_vectors(db) == 0
    db.close()
    fresh = open_db(tmp_path / "new.db")
    assert fresh.execute("SELECT value FROM meta WHERE key='dim'").fetchone()[0] == str(DIM)
    fresh.close()


def test_save_dry_run_on_a_non_default_width_keeps_vectors(git_clone, tmp_path, monkeypatch):
    """Regression: the duplicate probe behind dry_run/propose opened the index at 768."""
    import memd.save as save_mod
    db_path = tmp_path / "m.db"
    for key, value in {"MEMD_CLONE": str(git_clone), "MEMD_DB": str(db_path), "MEMD_PROFILE": "amber",
                       "MEMD_AMBER_CLONE": str(git_clone), "MEMD_AMBER_DB": str(db_path),
                       "MEMD_EMBED_DIM": "16", "MEMD_LOCAL_HOST": "any",
                       "MEMD_EMBED_URL": "http://127.0.0.1:9"}.items():
        monkeypatch.setenv(key, value)
    cfg = Config.from_env(env_file=None)
    db = open_db(db_path, dim=16)
    _seed(db, dim=16)
    db.close()
    monkeypatch.setattr(save_mod, "embed_with_deadline", lambda *a, **k: [1.0] * 16, raising=False)
    save_mod.dry_run({"title": "Probe", "body": "Something new about the backups."}, "amber", cfg=cfg)
    db = open_db(db_path, dim=16)
    assert db.execute("SELECT COUNT(*) FROM vec_notes").fetchone()[0] == 1
    assert db.execute("SELECT value FROM meta WHERE key='head'").fetchone()[0] == "abc"
    db.close()
