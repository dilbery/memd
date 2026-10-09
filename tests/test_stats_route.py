"""GET /stats must not rebuild the vector cache, and must not answer without a token.

stats_route rebuilt the Config by hand instead of using dataclasses.replace, so every
field added after the original four fell back to its dataclass default. cfg.embed_dim
came back as 768 while the service ran at 2560, and the open_db migration guard then
dropped vec_notes, nulled every vector_blob and deleted head. A single unauthenticated
GET destroyed semantic recall on a live store. Observed in a real deployment, not
hypothetical.
"""
import sqlite3
import subprocess

import pytest
import sqlite_vec
from fastapi.testclient import TestClient

import memd.server as server_mod
from memd.index import _upsert_lexical, open_db
from memd.store import Note


@pytest.fixture
def store(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    subprocess.run(["git", "-C", str(clone), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-q", "--allow-empty", "-m", "init"],
                   check=True)
    db_path = tmp_path / "m.db"
    monkeypatch.setenv("MEMD_AMBER_CLONE", str(clone))
    monkeypatch.setenv("MEMD_AMBER_DB", str(db_path))
    monkeypatch.setenv("MEMD_CLONE", str(clone))
    monkeypatch.setenv("MEMD_DB", str(db_path))
    monkeypatch.setenv("MEMD_LOCAL_HOST", "any")
    monkeypatch.setenv("MEMD_PROFILE", "amber")
    monkeypatch.setenv("MEMD_TOKEN", "x" * 40)
    monkeypatch.setenv("MEMD_EMBED_DIM", "2560")
    monkeypatch.setenv("MEMD_EMBED_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("MEMD_RERANK_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("MEMD_STARTUP_REFRESH", "0")

    db = open_db(db_path, dim=2560)
    note = Note(title="t", slug="t", path="t.md", body="b", git_blob="blob1")
    with db:
        _upsert_lexical(db, note)
        db.execute("INSERT INTO vec_notes(slug, embedding) VALUES (?, ?)",
                   ("t", sqlite_vec.serialize_float32([1.0] * 2560)))
        # A fully embedded note also has its chunk vectors.
        db.execute("INSERT INTO vec_chunks(embedding, slug, ordinal, start_char, end_char) "
                   "VALUES (?, 't', 0, 0, 1)", (sqlite_vec.serialize_float32([1.0] * 2560),))
        db.execute("UPDATE notes SET vector_blob=git_blob, chunk_blob=git_blob WHERE slug='t'")
        db.execute("INSERT INTO meta(key,value) VALUES ('head','abc')")
    db.close()
    return db_path


def _meta(db_path):
    raw = sqlite3.connect(str(db_path))
    try:
        return dict(raw.execute("SELECT key, value FROM meta").fetchall())
    finally:
        raw.close()


def _vector_rows(db_path):
    raw = sqlite3.connect(str(db_path))
    try:
        return raw.execute(
            "SELECT COUNT(*) FROM notes WHERE vector_blob IS NOT NULL").fetchone()[0]
    finally:
        raw.close()


def test_stats_leaves_the_vector_cache_alone(store):
    assert _meta(store)["dim"] == "2560"
    assert _vector_rows(store) == 1

    with TestClient(server_mod.create_token_app()) as client:
        response = client.get("/stats", headers={"Authorization": "Bearer " + "x" * 40})

    assert response.status_code == 200, response.text
    after = _meta(store)
    assert after["dim"] == "2560", "stats rebuilt the cache at the wrong dimension"
    assert after.get("head") == "abc", "stats deleted the indexed head"
    assert _vector_rows(store) == 1, "stats discarded the cached vectors"
    assert response.json()["pending_vectors"] == 0


def test_stats_reports_the_configured_dimension(store):
    with TestClient(server_mod.create_token_app()) as client:
        response = client.get("/stats", headers={"Authorization": "Bearer " + "x" * 40})
    assert response.json()["vec"] == 1
    assert response.json()["vec_chunks"] == 1


def test_stats_without_a_token_is_refused(store):
    with TestClient(server_mod.create_token_app()) as client:
        response = client.get("/stats")
    assert response.status_code == 401
    assert _meta(store)["dim"] == "2560"
