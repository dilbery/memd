import memd.index as index_mod
from memd.index import open_db, build_index, reindex, head_in_index, set_head_in_index
from memd.store import git_head_sha


def _fake_embed(texts, cfg):
    # deterministic 768-dim vector keyed off text length; no embedding backend.
    return [[float(len(t) % 7)] * 768 for t in texts]


def test_build_index_populates_all_tables(config, monkeypatch):
    monkeypatch.setattr(index_mod, "embed", _fake_embed)
    db = open_db(config.db)
    build_index(db, config)
    (notes,) = db.execute("select count(*) from notes").fetchone()
    (vecs,) = db.execute("select count(*) from vec_notes").fetchone()
    (fts,) = db.execute("select count(*) from fts_notes").fetchone()
    (chunks,) = db.execute("select count(*) from vec_chunks").fetchone()
    assert notes == 3 == vecs == fts == chunks


def test_fts_tokenizer_keeps_identifiers(config, monkeypatch):
    monkeypatch.setattr(index_mod, "embed", _fake_embed)
    db = open_db(config.db)
    build_index(db, config)
    # dotted/colon identifiers must survive tokenization as single tokens.
    rows = db.execute(
        "select slug from fts_notes where fts_notes match ?",
        ('"10.10.1.11"',),
    ).fetchall()
    assert ("vmhost-proxmox-vm",) in rows


def test_vec0_is_float768(config, monkeypatch):
    monkeypatch.setattr(index_mod, "embed", _fake_embed)
    db = open_db(config.db)
    build_index(db, config)
    # a 768-dim KNN query must run without dimension error.
    import sqlite_vec
    q = sqlite_vec.serialize_float32([1.0] * 768)
    rows = db.execute(
        "select slug from vec_notes where embedding match ? order by distance limit 3",
        (q,),
    ).fetchall()
    assert len(rows) == 3


def test_reindex_skips_unchanged_blobs(config, monkeypatch):
    calls = {"n": 0}

    def counting_embed(texts, cfg):
        calls["n"] += len(texts)
        return [[1.0] * 768 for _ in texts]

    monkeypatch.setattr(index_mod, "embed", counting_embed)
    db = open_db(config.db)
    build_index(db, config)
    first = calls["n"]
    # One whole-note vector and one chunk vector for each short fixture note.
    assert first == 6
    # No file changed -> reindex re-embeds nothing.
    reindex(db, config)
    assert calls["n"] == first


def test_reindex_reembeds_only_changed(config, git_clone, monkeypatch):
    calls = {"n": 0}

    def counting_embed(texts, cfg):
        calls["n"] += len(texts)
        return [[1.0] * 768 for _ in texts]

    monkeypatch.setattr(index_mod, "embed", counting_embed)
    db = open_db(config.db)
    build_index(db, config)
    base = calls["n"]
    # mutate one note's body -> its git_blob changes -> only it re-embeds.
    p = git_clone / "vmhost-proxmox-vm.md"
    p.write_text(p.read_text() + "\nNew line changes the blob.\n")
    reindex(db, config)
    assert calls["n"] == base + 2   # its whole-note vector and its one chunk


def test_head_roundtrip(config, monkeypatch, git_head):
    monkeypatch.setattr(index_mod, "embed", _fake_embed)
    db = open_db(config.db)
    build_index(db, config)
    set_head_in_index(db, git_head())
    assert head_in_index(db) == git_head()
