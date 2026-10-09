import sqlite3

import pytest

from memd.index import _FTS_VERSION, _get_meta, open_db
from memd.query import (coverage, distill, is_identifier, match_expr, normalize,
                        tokens)


@pytest.mark.parametrize("raw, want", [
    ("gpuhost.", "gpuhost"), ("memd:", "memd"), ("--ctx-size", "ctx-size"),
    ("10.10.1.11", "10.10.1.11"), ("bge-reranker-v2-m3", "bge-reranker-v2-m3"),
    ("(oomkiller).", "(oomkiller)"), ("---", ""),
])
def test_normalize_strips_only_token_run_edges(raw, want):
    assert normalize(raw) == want
    assert normalize(want) == want


def test_tokens_drop_stopwords_and_agent_framing():
    got = tokens("ok so before i touch the render stuff what do i need to know about the gpu")
    assert got == ["touch", "render", "gpu"]
    assert tokens("Appd: restarted on gpuhost.") == ["appd", "restarted", "gpuhost"]
    assert tokens("what should i know") == ["what", "should", "know"]  # framing-only survives


def test_is_identifier():
    assert all(map(is_identifier, ["gfx1100", "8080", "netd-guard", "v2", "10.10.1.11"]))
    assert not any(map(is_identifier, ["neon", "ok", "gpu"]))


def test_match_expr_quotes_terms():
    assert match_expr(['a"b', "c"]) == '"a""b" OR "c"'
    assert match_expr([]) == ""


def _corpus(tmp_path, bodies):
    db = open_db(tmp_path / "m.db")
    for i, body in enumerate(bodies):
        db.execute("INSERT INTO notes(slug,path,title,body,git_blob) VALUES (?,?,?,?,?)",
                   (f"n{i}", f"n{i}.md", f"n{i}", body, "x"))
        db.execute("INSERT INTO fts_notes(slug,title,body,tags) VALUES (?,?,?,?)",
                   (f"n{i}", f"n{i}", normalize(body), ""))
    db.commit()
    return db


def test_distill_keeps_rare_terms_and_drops_saturated_ones(tmp_path):
    db = _corpus(tmp_path, ["gpuhost gpu box"] * 8 + ["gpuhost oomkiller floors", "gpuhost netd-guard"])
    d = distill(db, "why is oomkiller killing things on the gpuhost gpu box")
    assert d.terms[0] == "oomkiller"
    assert "gpuhost" in d.dropped_saturated and "killing" in d.dropped_absent
    assert set(d.idf) == set(d.terms)
    # identifiers are never dropped as saturated
    (tmp_path / "b").mkdir()
    db2 = _corpus(tmp_path / "b", ["netd-guard here"] * 9 + ["other"])
    assert "netd-guard" in distill(db2, "netd-guard").terms


def test_coverage_is_idf_weighted_and_sees_normalised_text(tmp_path):
    db = _corpus(tmp_path, ["alpha common", "beta common", "common"])
    d = distill(db, "alpha beta", saturation=1.0)
    assert coverage("Alpha. and beta:", d) == pytest.approx(1.0)
    assert 0 < coverage("alphabet only", d) < 0.5


def test_old_fts_rows_are_rebuilt_normalised(tmp_path):
    path = tmp_path / "old.db"
    db = open_db(path)
    db.execute("INSERT INTO notes(slug,path,title,body,git_blob,metadata) VALUES "
               "('a','a.md','A','served by gpuhost.','x','{\"tags\": [\"gpu:\"]}')")
    db.execute("INSERT INTO fts_notes(slug,title,body,tags) VALUES ('a','A','served by gpuhost.','gpu:')")
    db.execute("DELETE FROM meta WHERE key='fts_version'")
    db.commit()
    db.close()
    db = open_db(path)
    assert _get_meta(db, "fts_version") == _FTS_VERSION
    assert db.execute("SELECT count(*) FROM fts_notes WHERE fts_notes MATCH '\"gpuhost\"'").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM fts_notes WHERE fts_notes MATCH '\"gpu\"'").fetchone()[0] == 1
