"""Per-chunk vectors: indexing, pending accounting, migration and the recall arm."""
import dataclasses
import subprocess

import pytest
import sqlite_vec

import memd.index as index_mod
import memd.recall as recall_mod
from memd.chunk import chunk_body, embed_text
from memd.config import Config
from memd.store import Note, clone_lock, git_head_sha

DIM = 768
KEYWORDS = ["kestrel", "osprey", "heron", "plover", "curlew", "dunlin"]


def keyword_vector(text: str) -> list[float]:
    """Deterministic bag-of-keywords embedding; never the zero vector."""
    low = text.lower()
    vec = [0.0] * DIM
    for i, word in enumerate(KEYWORDS):
        vec[i] = float(low.count(word))
    vec[DIM - 1] = 0.05
    return vec


def keyword_embed(texts, cfg):
    return [keyword_vector(t) for t in texts]


def unit(i: int, tail: float = 0.0) -> list[float]:
    vec = [0.0] * DIM
    vec[i] = 1.0
    vec[DIM - 1] = tail
    return vec


def commit(clone):
    for args in (["add", "-A"], ["commit", "-qm", "test changes"]):
        subprocess.run(["git", "-C", str(clone), *args], check=True, capture_output=True)


def filler(tag: str, paragraphs: int) -> str:
    return "\n\n".join(" ".join(f"{tag}{p}w{i}" for i in range(40)) for p in range(paragraphs))


UMBRELLA = f"""---
title: vmhost umbrella notes
slug: vmhost-umbrella
host: any
importance: 2
---
{filler("intro", 5)}

## Current state

The kestrel service now runs on port 8443 behind the proxy.

## History

{filler("old", 5)}
"""


def _add_umbrella(clone):
    (clone / "vmhost-umbrella.md").write_text(UMBRELLA)
    commit(clone)


def _chunk_rows(db, slug):
    return db.execute("SELECT ordinal, start_char, end_char FROM vec_chunks WHERE slug=? "
                      "ORDER BY ordinal", (slug,)).fetchall()


# ---------------------------------------------------------------- indexing


def test_reindex_stores_one_vector_per_chunk_with_offsets(config, monkeypatch):
    _add_umbrella(config.clone)
    seen = []
    def recording_embed(texts, cfg):
        seen.extend(texts)
        return keyword_embed(texts, cfg)
    monkeypatch.setattr(index_mod, "embed", recording_embed)
    db = index_mod.open_db(config.db)
    index_mod.reindex(db, config)
    body = db.execute("SELECT body FROM notes WHERE slug='vmhost-umbrella'").fetchone()[0]
    chunks = chunk_body(body)
    assert len(chunks) > 2
    assert _chunk_rows(db, "vmhost-umbrella") == [(c.ordinal, c.start, c.end) for c in chunks]
    # Whole-note vector texts are the raw body; chunk texts carry the title.
    assert body in seen
    for c in chunks:
        assert embed_text("vmhost umbrella notes", body, c) in seen
    assert all(t.startswith("vmhost umbrella notes\n") for t in seen if t != body
               and "umbrella" in t)
    assert index_mod.pending_vectors(db) == 0
    assert index_mod.head_in_index(db) == git_head_sha(config.clone)
    db.close()


def test_a_changed_note_replaces_its_chunks_and_nothing_else_is_reembedded(config, monkeypatch):
    _add_umbrella(config.clone)
    monkeypatch.setattr(index_mod, "embed", keyword_embed)
    db = index_mod.open_db(config.db)
    index_mod.reindex(db, config)
    before_other = _chunk_rows(db, "repo-hosting-policy")
    path = config.clone / "vmhost-umbrella.md"
    path.write_text(UMBRELLA.replace("## History", "## Later\n\nshort.\n\n## History"))
    commit(config.clone)
    seen = []
    def recording_embed(texts, cfg):
        seen.extend(texts)
        return keyword_embed(texts, cfg)
    monkeypatch.setattr(index_mod, "embed", recording_embed)
    assert index_mod.reindex(db, config) == 1
    body = db.execute("SELECT body FROM notes WHERE slug='vmhost-umbrella'").fetchone()[0]
    assert len(seen) == 1 + len(chunk_body(body))
    assert _chunk_rows(db, "vmhost-umbrella") == [(c.ordinal, c.start, c.end) for c in chunk_body(body)]
    assert _chunk_rows(db, "repo-hosting-policy") == before_other
    db.close()


def test_superseded_note_chunks_are_pruned(config, monkeypatch):
    _add_umbrella(config.clone)
    monkeypatch.setattr(index_mod, "embed", keyword_embed)
    db = index_mod.open_db(config.db)
    index_mod.reindex(db, config)
    assert _chunk_rows(db, "vmhost-umbrella")
    path = config.clone / "vmhost-umbrella.md"
    path.write_text(UMBRELLA.replace("importance: 2", "importance: 2\nsuperseded_by: repo-hosting-policy"))
    commit(config.clone)
    index_mod.reindex(db, config)
    assert _chunk_rows(db, "vmhost-umbrella") == []
    assert index_mod.pending_vectors(db) == 0
    db.close()


def test_writer_during_embedding_leaves_no_chunks_for_the_obsolete_blob(config, monkeypatch):
    def racing_embed(texts, cfg):
        # A save lands while the (unlocked) embed is in flight.
        with clone_lock(config.clone):
            path = config.clone / "vmhost-proxmox-vm.md"
            path.write_text(path.read_text() + "\nchanged-during-embed\n")
            commit(config.clone)
        return keyword_embed(texts, cfg)
    monkeypatch.setattr(index_mod, "embed", racing_embed)
    db = index_mod.open_db(config.db)
    assert index_mod.reindex(db, config) == 2
    assert _chunk_rows(db, "vmhost-proxmox-vm") == []
    assert index_mod.pending_vectors(db) == 1
    assert index_mod.head_in_index(db) is None
    db.close()


def test_rounds_keep_finished_work_when_a_later_round_fails(config, monkeypatch):
    monkeypatch.setattr(index_mod, "_REINDEX_BATCH_TEXTS", 2)   # one note (2 texts) per round
    calls = {"n": 0}
    def flaky_embed(texts, cfg):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("backend went away")
        return keyword_embed(texts, cfg)
    monkeypatch.setattr(index_mod, "embed", flaky_embed)
    db = index_mod.open_db(config.db)
    with pytest.raises(RuntimeError):
        index_mod.reindex(db, config)
    assert index_mod.pending_vectors(db) == 2
    assert db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 1
    assert index_mod.head_in_index(db) is None
    monkeypatch.setattr(index_mod, "embed", keyword_embed)
    assert index_mod.reindex(db, config) == 2
    assert index_mod.pending_vectors(db) == 0
    assert index_mod.head_in_index(db) == git_head_sha(config.clone)
    db.close()


def _pre_chunk_database(config, monkeypatch):
    """An index as the previous release left it: whole-note vectors, no chunks."""
    monkeypatch.setattr(index_mod, "embed", keyword_embed)
    db = index_mod.open_db(config.db)
    index_mod.reindex(db, config)
    db.execute("DROP TABLE vec_chunks")
    db.execute("ALTER TABLE notes DROP COLUMN chunk_blob")
    db.execute("DELETE FROM meta WHERE key='chunk_version'")
    db.commit()
    db.close()


def test_upgrade_embeds_only_chunks_and_keeps_notes_fts_and_note_vectors(config, monkeypatch):
    _pre_chunk_database(config, monkeypatch)
    db = index_mod.open_db(config.db)
    assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM fts_notes").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM vec_notes").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM notes WHERE vector_blob = git_blob").fetchone()[0] == 3
    assert index_mod.pending_vectors(db) == 3
    assert index_mod.head_in_index(db) is None
    with pytest.raises(ValueError, match="pending"):
        index_mod.set_head_in_index(db, git_head_sha(config.clone))
    seen = []
    def recording_embed(texts, cfg):
        seen.extend(texts)
        return keyword_embed(texts, cfg)
    monkeypatch.setattr(index_mod, "embed", recording_embed)
    assert index_mod.reindex(db, config) == 3
    bodies = {r[0] for r in db.execute("SELECT body FROM notes")}
    assert len(seen) == 3 and not bodies & set(seen), "whole-note vectors were re-embedded"
    assert index_mod.pending_vectors(db) == 0
    assert index_mod.head_in_index(db) == git_head_sha(config.clone)
    db.close()


def test_a_chunk_version_bump_rebuilds_only_chunks(config, monkeypatch):
    monkeypatch.setattr(index_mod, "embed", keyword_embed)
    db = index_mod.open_db(config.db)
    index_mod.reindex(db, config)
    db.close()
    monkeypatch.setattr(index_mod, "_CHUNK_VERSION", "test-next")
    db = index_mod.open_db(config.db)
    assert db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM vec_notes").fetchone()[0] == 3
    assert index_mod.pending_vectors(db) == 3
    assert index_mod.head_in_index(db) is None
    db.close()
    # Reopening at the new version is a no-op for readers.
    db = index_mod.open_db(config.db)
    assert index_mod.pending_vectors(db) == 3
    db.close()


def test_upsert_note_helper_writes_chunks_only_when_given(tmp_path):
    db = index_mod.open_db(tmp_path / "m.db")
    note = Note(title="t", slug="t", path="t.md", body="kestrel", git_blob="b1")
    with db:
        index_mod._upsert_note(db, note, unit(0))
    assert index_mod.pending_vectors(db) == 1
    with db:
        index_mod._upsert_note(db, note, unit(0), [unit(0)])
    assert index_mod.pending_vectors(db) == 0
    assert _chunk_rows(db, "t") == [(0, 0, 7)]
    with pytest.raises(Exception):
        index_mod._upsert_note(db, note, unit(0), [unit(0), unit(1)])
    db.close()


# ---------------------------------------------------------------- recall arm


def _seed(db, slug, chunk_vectors, *, body=None, note_vector=None, title=None):
    """A note whose chunk_body yields len(chunk_vectors) chunks, with those vectors."""
    if body is None:
        body = "\n\n".join(f"## S{i}\n\n" + " ".join(f"{slug}{i}w{j}" for j in range(90))
                           for i in range(len(chunk_vectors)))
    assert len(chunk_body(body)) == len(chunk_vectors)
    note = Note(title=title or slug, slug=slug, path=f"{slug}.md", body=body, git_blob=f"{slug}-blob")
    with db:
        index_mod._upsert_note(db, note, note_vector or chunk_vectors[0], chunk_vectors)
    return note


def test_arm_scores_a_note_by_its_best_chunk(tmp_path):
    db = index_mod.open_db(tmp_path / "m.db")
    # umbrella: opening is off-topic, its third chunk is exactly the query.
    _seed(db, "umbrella", [unit(1), unit(2), unit(0)])
    _seed(db, "near", [unit(0, tail=0.8)])
    _seed(db, "far", [unit(3)])
    assert recall_mod._vector_arm(db, unit(0))[:2] == ["umbrella", "near"]
    best = recall_mod._best_chunks(db, unit(0), ["umbrella", "near"])
    assert best["umbrella"].ordinal == 2 and best["umbrella"].similarity == pytest.approx(1.0)
    db.close()


def test_multi_chunk_bonus_breaks_near_ties_and_is_capped(tmp_path, monkeypatch):
    db = index_mod.open_db(tmp_path / "m.db")
    _seed(db, "single", [unit(0, tail=0.1)])
    _seed(db, "several", [unit(0, tail=0.1), unit(0, tail=0.3), unit(0, tail=0.3)])
    monkeypatch.setattr(recall_mod, "CHUNK_HIT_BONUS", 0.0)
    assert recall_mod._vector_arm(db, unit(0))[0] in {"single", "several"}
    monkeypatch.setattr(recall_mod, "CHUNK_HIT_BONUS", 0.005)
    assert recall_mod._vector_arm(db, unit(0))[:2] == ["several", "single"]
    # A cap of zero extra chunks removes the bonus entirely.
    monkeypatch.setattr(recall_mod, "CHUNK_HIT_CAP", 0)
    _seed(db, "exact", [unit(0)])
    assert recall_mod._vector_arm(db, unit(0))[0] == "exact"
    db.close()


def test_arm_keeps_its_note_cap_and_scope(tmp_path, monkeypatch):
    db = index_mod.open_db(tmp_path / "m.db")
    for i in range(6):
        _seed(db, f"n{i}", [unit(0, tail=0.1 * i), unit(1)])
    _seed(db, "remote", [unit(5)])
    monkeypatch.setattr(recall_mod, "VEC_K", 3)
    assert recall_mod._vector_arm(db, unit(0)) == ["n0", "n1", "n2"]
    # Scope filters inside the KNN, so the one far in-scope note is still found.
    monkeypatch.setattr(recall_mod, "VEC_CHUNK_K", 2)
    assert recall_mod._vector_arm(db, unit(0), ["remote"]) == ["remote"]
    assert recall_mod._vector_arm(db, unit(0), []) == []
    db.close()


def test_arm_ignores_stale_chunks_and_falls_back_to_the_note_vector(tmp_path):
    db = index_mod.open_db(tmp_path / "m.db")
    _seed(db, "fresh", [unit(0, tail=0.5)])
    _seed(db, "stale", [unit(0)], note_vector=unit(0, tail=0.2))
    with db:
        db.execute("UPDATE notes SET chunk_blob='older' WHERE slug='stale'")
    assert "stale" not in recall_mod._chunk_hits(db, unit(0), None, 50)
    # While its chunks are pending, the note competes on its whole-note vector.
    assert recall_mod._vector_arm(db, unit(0)) == ["stale", "fresh"]
    with db:
        db.execute("UPDATE notes SET vector_blob='older' WHERE slug='stale'")
    assert recall_mod._vector_arm(db, unit(0)) == ["fresh"]
    db.close()


def test_zero_vector_chunks_never_rank_first(tmp_path):
    db = index_mod.open_db(tmp_path / "m.db")
    _seed(db, "zero", [[0.0] * DIM])
    _seed(db, "real", [unit(0, tail=0.9)])
    assert recall_mod._vector_arm(db, unit(0)) == ["real"]
    db.close()


def test_rerank_text_uses_a_later_chunk_with_a_lead_and_heading(monkeypatch):
    body = UMBRELLA.split("---\n", 2)[2]
    note = Note(slug="u", path="u.md", title="vmhost umbrella notes", body=body)
    chunks = chunk_body(body)
    fact = next(c for c in chunks if "kestrel" in body[c.start:c.end])
    assert fact.ordinal > 0 and fact.end > recall_mod.RERANK_BODY_CHARS
    hit = recall_mod._ChunkHit(fact.ordinal, fact.start, fact.end, 0.9)
    text = recall_mod._rerank_text(note, hit)
    assert text.startswith("vmhost umbrella notes\n" + body[:recall_mod.RERANK_LEAD_CHARS].rstrip())
    assert "\n…\n" in text and "port 8443" in text
    assert len(text) <= len(note.title) + recall_mod.RERANK_LEAD_CHARS + recall_mod.RERANK_BODY_CHARS + 100
    # The opening chunk, a disabled switch, or no hit: title + opening as before.
    opening = "vmhost umbrella notes\n" + body[:recall_mod.RERANK_BODY_CHARS]
    assert recall_mod._rerank_text(note, recall_mod._ChunkHit(0, 0, chunks[0].end, 0.9)) == opening
    assert recall_mod._rerank_text(note) == opening
    monkeypatch.setattr(recall_mod, "RERANK_CHUNK", False)
    assert recall_mod._rerank_text(note, hit) == opening


def test_rerank_text_shows_a_continuation_chunks_heading_path():
    body = "# Server\n\n## Current state\n\n" + "\n\n".join(
        " ".join(f"f{p}w{i}" for i in range(40)) for p in range(12))
    chunks = chunk_body(body)
    later = next(c for c in chunks if c.context and c.end > recall_mod.RERANK_BODY_CHARS)
    note = Note(slug="s", path="s.md", title="", body=body)
    text = recall_mod._rerank_text(note, recall_mod._ChunkHit(later.ordinal, later.start, later.end, 0.5))
    assert "\n…\nServer > Current state\n" in text


def test_recall_finds_the_umbrella_fact_and_shows_it_to_the_reranker(config, monkeypatch):
    _add_umbrella(config.clone)
    monkeypatch.setattr(index_mod, "embed", keyword_embed)
    db = index_mod.open_db(config.db)
    index_mod.reindex(db, config)
    db.close()
    monkeypatch.setattr(recall_mod, "embed_with_deadline",
                        lambda text, **kw: keyword_vector(text))
    seen = {}
    def capture_rerank(query, candidates, top_n=8, ms=400, *, cfg):
        seen.update({c["slug"]: c["body"] for c in candidates})
        return list(candidates)
    monkeypatch.setattr(recall_mod, "rerank", capture_rerank)
    trace = {}
    result = recall_mod.recall("which port does kestrel use", cfg=config,
                               include_core=False, trace=trace)
    assert result[0].slug == "vmhost-umbrella"
    assert trace["vec"][0] == "vmhost-umbrella"
    assert "port 8443" in seen["vmhost-umbrella"]
    assert trace["chunks"]["vmhost-umbrella"][0] > 0


def test_recall_vectors_notes_restores_the_whole_note_arm(config, monkeypatch):
    _add_umbrella(config.clone)
    monkeypatch.setattr(index_mod, "embed", keyword_embed)
    db = index_mod.open_db(config.db)
    index_mod.reindex(db, config)
    db.close()
    monkeypatch.setattr(recall_mod, "embed_with_deadline",
                        lambda text, **kw: keyword_vector(text))
    monkeypatch.setattr(recall_mod, "_vector_arm",
                        lambda *a, **kw: pytest.fail("chunk arm used under recall_vectors=notes"))
    seen = {}
    def capture_rerank(query, candidates, top_n=8, ms=400, *, cfg):
        seen.update({c["slug"]: c["body"] for c in candidates})
        return list(candidates)
    monkeypatch.setattr(recall_mod, "rerank", capture_rerank)
    cfg = dataclasses.replace(config, recall_vectors="notes")
    trace = {}
    recall_mod.recall("which port does kestrel use", cfg=cfg, include_core=False, trace=trace)
    assert "vmhost-umbrella" in trace["vec"]
    assert trace["chunks"] == {}
    # The reranker sees title + opening, as before chunking.
    assert seen["vmhost-umbrella"].startswith("vmhost umbrella notes\nintro0w0")
    assert "port 8443" not in seen["vmhost-umbrella"]


def test_recall_vectors_setting():
    assert Config().recall_vectors == "chunks"
    assert Config.from_env({"MEMD_RECALL_VECTORS": "notes"}, env_file=None).recall_vectors == "notes"
    assert Config.from_env({"MEMD_RECALL_VECTORS": " Chunks "}, env_file=None).recall_vectors == "chunks"
    assert Config.from_env({"MEMD_RECALL_VECTORS": "bogus"}, env_file=None).recall_vectors == "chunks"
