from memd import recall as R
from memd.query import Distilled
from memd.store import Note


def _n(slug, body):
    return Note(slug=slug, path=f"{slug}.md", title=slug, body=body)


D = Distilled(("ledger", "quotas"), {"ledger": 3.0, "quotas": 1.0}, 100, (), ())


def test_rrf_rewards_notes_found_by_both_arms_and_full_keyword_coverage(monkeypatch):
    monkeypatch.setattr(R, "FUSION", "rrf")
    monkeypatch.setattr(R, "RRF_K", 20)
    notes = {"both": _n("both", "ledger quotas"), "vec": _n("vec", "memory"),
             "kw-full": _n("kw-full", "ledger quotas"), "kw-part": _n("kw-part", "quotas only")}
    # both: 1/22 + 1/22; vec: 1/21; kw-full: 1.0/23; kw-part (coverage 0.25): 0.625/21
    assert R._fuse(["vec", "both"], ["kw-part", "both", "kw-full"], notes, D) == \
        ["both", "vec", "kw-full", "kw-part"]


def test_interleave_mode_is_the_legacy_zip(monkeypatch):
    monkeypatch.setattr(R, "FUSION", "interleave")
    assert R._fuse(["a", "b"], ["c", "a", "d"], {}, D) == ["a", "c", "b", "d"]


def test_rerank_head_reserves_keyword_seats(monkeypatch):
    monkeypatch.setattr(R, "KW_SEATS", 3)
    union = [f"v{i}" for i in range(20)] + ["k0", "k1", "k2"]
    head = R._rerank_head(union, ["k0", "k1", "k2"], 10)
    assert len(head) == 10 and {"k0", "k1", "k2"} <= set(head)
    assert head[:7] == union[:7]          # only the tail of the head is evicted
    assert R._rerank_head(["k0", "v0"], ["k0"], 10) == ["k0", "v0"]


def test_blend_keeps_a_fused_leader_the_reranker_underrated(monkeypatch):
    from pathlib import Path
    from memd.config import Config
    monkeypatch.setattr(R, "RERANK_BLEND", 1.0)
    monkeypatch.setattr(R, "BLEND_K", 10)
    monkeypatch.setattr(R, "FUSION", "interleave")      # fused order == vector order here
    monkeypatch.setattr(R, "_vector_arm", lambda db, q: ["a", "b", "c"])
    monkeypatch.setattr(R, "_bm25_arm", lambda db, d: [])
    monkeypatch.setattr(R, "distill", lambda db, q: D)
    monkeypatch.setattr(R, "_core_set", lambda db: [])
    monkeypatch.setattr(R, "embed_with_deadline", lambda *a, **k: [0.0] * 768)
    monkeypatch.setattr(R, "git_head_sha", lambda clone: "")
    monkeypatch.setattr(R, "_fetch_notes", lambda db, slugs: {s: _n(s, s) for s in slugs})
    # the cross-encoder prefers b, c, a
    monkeypatch.setattr(R, "rerank", lambda q, cands, **k: [cands[1], cands[2], cands[0]])

    class _DB:
        def close(self):
            pass
    monkeypatch.setattr(R, "open_db", lambda p, **kwargs: _DB())
    monkeypatch.setattr(R, "guard_paths", lambda profile, clone, db: (Path("/tmp/c"), Path("/tmp/d")))
    cfg = Config(clone=Path("/tmp/c"), db=Path("/tmp/d"), profile="amber")
    assert [n.slug for n in R.recall("q", cfg=cfg, include_core=False)] == ["b", "a", "c"]
    monkeypatch.setattr(R, "RERANK_BLEND", 0.0)          # legacy: reranker order wins outright
    assert [n.slug for n in R.recall("q", cfg=cfg, include_core=False)] == ["b", "c", "a"]


def test_current_intent_gate_is_strict():
    hit = ["what is the current search backend", "latest status of the beta", "where does the wiki run now",
           "what version is running at the moment", "who hosts it these days", "the proxy config now after the cutover"]
    miss = ["i'm still confused why the printer dropped", "today i want to understand the ledger fix",
            "why did quartz hang on 09-18", "how was it fixed back then"]
    assert all(R.CURRENT_INTENT.search(q) for q in hit)
    assert not any(R.CURRENT_INTENT.search(q) for q in miss)
    # Known, accepted false positive: it only adds fresh candidates for the reranker to judge.
    assert R.CURRENT_INTENT.search("now that you mention it, how was X fixed in august")


def _dated_corpus(tmp_path, rows):
    from memd.index import open_db
    from memd.query import normalize
    db = open_db(tmp_path / "f.db")
    for slug, title, body in rows:
        db.execute("INSERT INTO notes(slug,path,title,body,git_blob) VALUES (?,?,?,?,?)",
                   (slug, f"{slug}.md", title, body, "x"))
        db.execute("INSERT INTO fts_notes(slug,title,body,tags) VALUES (?,?,?,?)",
                   (slug, normalize(title), normalize(body), ""))
    db.commit()
    return db


def test_fresh_arm_returns_topical_notes_newest_first(tmp_path, monkeypatch):
    from memd.query import distill
    db = _dated_corpus(tmp_path, [
        ("old", "quartz cutover 2026-08-01", "quartz scheduler engine"),
        ("new", "quartz upgrade 2026-09-26", "quartz scheduler engine"),
        ("mid", "quartz soak 2026-09-18", "quartz scheduler engine"),
        ("undated", "quartz notes", "quartz scheduler engine"),
        ("offtopic", "printer 2026-09-27", "printer wifi quartz"),
    ] + [(f"pad{i}", f"pad {i}", "filler text") for i in range(10)])
    monkeypatch.setattr(R, "FRESH_MIN_COVERAGE", 0.6)
    got = R._fresh_arm(db, distill(db, "current quartz scheduler engine"))
    assert got[:3] == ["new", "mid", "old"]
    assert "undated" not in got and "offtopic" not in got
    assert R._fresh_arm(db, distill(db, "quartz"), []) == []


def test_reranker_sees_the_title_then_the_opening_body(monkeypatch):
    note = Note(slug="s", path="s.md", title="Quartz 2.4.1 live 2026-09-26", body="x" * 5000)
    assert R._rerank_text(note) == "Quartz 2.4.1 live 2026-09-26\n" + "x" * R.RERANK_BODY_CHARS
    monkeypatch.setattr(R, "RERANK_TITLE", False)
    assert R._rerank_text(note) == "x" * R.RERANK_BODY_CHARS
