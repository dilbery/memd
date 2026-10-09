"""Guards for the defects found in the 2026-08-15 in-depth review.

Each test pins a behaviour that was silently wrong in production. Grouped by the
consequence rather than by module, because that is what must not come back.
"""
from pathlib import Path

import pytest

from memd import recall as recall_mod
from memd.rerank import rerank
from memd.save import _frontmatter
from memd.store import Note, dump_note, git_head_sha, parse_text


# --------------------------------------------------------------------------
# Data loss: `description` is the curated one-liner that renders the core index.
# It is parsed in but was written back by NEITHER serialiser, so any rewrite
# erased it. Nearly every note in a curated store carries one.
# --------------------------------------------------------------------------

def test_dump_note_round_trip_preserves_description():
    raw = (
        "---\ntitle: T\nslug: t\nprofile: amber\nhost: gpuhost\nimportance: 5\n"
        "description: CURATED one-liner\ntags: [memd]\ngrounding: ok\n---\nBody.\n"
    )
    note = parse_text(raw, path=Path("t.md"))
    assert note.description == "CURATED one-liner"
    again = parse_text(dump_note(note), path=Path("t.md"))
    assert again.description == "CURATED one-liner"


def test_save_frontmatter_emits_description():
    fm = _frontmatter({
        "title": "T", "slug": "t", "profile": "amber", "host": "gpuhost",
        "importance": 5, "superseded_by": None, "tags": [], "grounding": "ok",
        "description": "CURATED one-liner",
    })
    assert "description: CURATED one-liner" in fm


def test_serialisers_omit_description_when_absent():
    """Do not churn every note with an empty key: that re-blobs the whole corpus
    and forces a full re-embed on the next reindex."""
    fm = _frontmatter({
        "title": "T", "slug": "t", "profile": "amber", "host": "gpuhost",
        "importance": 3, "superseded_by": None, "tags": [], "grounding": "ok",
    })
    assert "description" not in fm
    assert "description" not in dump_note(
        Note(slug="t", path="t.md", title="T", body="B"))


def test_dump_note_omits_null_last_used():
    """`last_used: null` in every note was churning blobs, and a populated value is
    what used to trip recall's sort into comparing str with int."""
    assert "last_used" not in dump_note(
        Note(slug="t", path="t.md", title="T", body="B"))
    assert "last_used: '2026-08-15'" in dump_note(
        Note(slug="t", path="t.md", title="T", body="B", last_used="2026-08-15"))


# --------------------------------------------------------------------------
# Wrong results: the union cap starved the BM25 arm to zero, so memd was a
# vector-only retriever whenever the embedder was healthy.
# --------------------------------------------------------------------------

def test_bm25_slugs_survive_a_full_vector_arm(monkeypatch):
    """With the vector arm returning a full VEC_K page, BM25 must still be
    represented in both the candidate pool and the rerank slice."""
    vec = [f"vec-{i}" for i in range(recall_mod.VEC_K)]
    bm = [f"bm-{i}" for i in range(recall_mod.FTS_K)]

    monkeypatch.setattr(recall_mod, "_vector_arm", lambda db, q: vec)
    monkeypatch.setattr(recall_mod, "_bm25_arm", lambda db, q: bm)
    monkeypatch.setattr(recall_mod, "distill", lambda db, q: _distilled())
    monkeypatch.setattr(recall_mod, "_core_set", lambda db: [])
    monkeypatch.setattr(recall_mod, "embed_with_deadline",
                        lambda *a, **k: [0.0] * 768)
    monkeypatch.setattr(recall_mod, "git_head_sha", lambda clone: "")
    captured = {}

    def fake_rerank(query, candidates, *a, **k):
        captured["reranked"] = [c["slug"] for c in candidates]
        return None

    monkeypatch.setattr(recall_mod, "rerank", fake_rerank)
    monkeypatch.setattr(recall_mod, "_fetch_notes", lambda db, slugs: {
        s: Note(slug=s, path=f"{s}.md", title=s, body=s) for s in slugs})
    monkeypatch.setattr(recall_mod, "open_db", lambda p, **kwargs: _FakeDB())
    monkeypatch.setattr(recall_mod, "guard_paths",
                        lambda profile, clone, db: (Path("/tmp/c"), Path("/tmp/d")))

    trace = {}
    recall_mod.recall("q", cfg=_cfg(), include_core=False, trace=trace)

    assert any(s.startswith("bm-") for s in trace["pool"]), \
        "BM25 arm was starved out of the candidate pool"
    head = captured["reranked"]
    assert len(head) == recall_mod.RERANK_MAX_CANDIDATES
    assert any(s.startswith("bm-") for s in head), "BM25 arm never reaches the reranker"
    assert any(s.startswith("vec-") for s in head), "vector arm must still be represented"


def _distilled():
    from memd.query import Distilled
    return Distilled(("q",), {"q": 1.0}, 1, (), ())


class _FakeDB:
    def close(self):
        pass


def _cfg():
    from memd.config import Config
    return Config(clone=Path("/tmp/c"), db=Path("/tmp/d"), profile="amber")


# --------------------------------------------------------------------------
# Silent degradation: an HTTP 200 with no usable results is a FAILURE. Returning
# [] made the caller's ladder read it as success and serve nothing.
# --------------------------------------------------------------------------

def test_rerank_returns_none_on_empty_results(monkeypatch):
    monkeypatch.setattr("memd.rerank.httpx.post",
                        lambda *a, **k: _Resp({"results": []}))
    assert rerank("q", [{"slug": "a", "body": "b"}], top_n=4, ms=900,
                  cfg=_cfg()) is None


def test_rerank_returns_none_when_every_index_is_unusable(monkeypatch):
    monkeypatch.setattr("memd.rerank.httpx.post", lambda *a, **k: _Resp(
        {"results": [{"index": 99, "relevance_score": 1.0}]}))
    assert rerank("q", [{"slug": "a", "body": "b"}], top_n=4, ms=900,
                  cfg=_cfg()) is None


def test_rerank_tolerates_a_missing_relevance_score(monkeypatch):
    """The sort key used to raise KeyError straight out of recall()."""
    monkeypatch.setattr("memd.rerank.httpx.post", lambda *a, **k: _Resp(
        {"results": [{"index": 0}, {"index": 1, "relevance_score": 2.0}]}))
    out = rerank("q", [{"slug": "a", "body": "a"}, {"slug": "b", "body": "b"}],
                 top_n=2, ms=900, cfg=_cfg())
    assert [c["slug"] for c in out] == ["b", "a"]


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# --------------------------------------------------------------------------
# Never fail a turn: git_head_sha raised out of recall() for a clone that was
# missing, not a repo, or had no commits.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("make", [
    lambda tmp: tmp / "does-not-exist",          # missing directory
    lambda tmp: tmp,                             # exists but is not a git repo
])
def test_git_head_sha_never_raises(tmp_path, make):
    assert git_head_sha(make(tmp_path)) == ""


def test_recall_does_not_reindex_when_head_is_unknown(monkeypatch):
    """An empty sha must not compare unequal to the stored marker and trigger a
    full reindex on every turn exactly when git is broken."""
    called = []
    monkeypatch.setattr(recall_mod, "git_head_sha", lambda clone: "")
    monkeypatch.setattr(recall_mod, "head_in_index", lambda db: "real-sha")
    monkeypatch.setattr(recall_mod, "request_refresh",
                        lambda cfg: called.append(True))
    monkeypatch.setattr(recall_mod, "_core_set", lambda db: [])
    monkeypatch.setattr(recall_mod, "_vector_arm", lambda db, q: [])
    monkeypatch.setattr(recall_mod, "_bm25_arm", lambda db, q: [])
    monkeypatch.setattr(recall_mod, "distill", lambda db, q: _distilled())
    monkeypatch.setattr(recall_mod, "_fetch_notes", lambda db, s: {})
    monkeypatch.setattr(recall_mod, "embed_with_deadline", lambda *a, **k: None)
    monkeypatch.setattr(recall_mod, "open_db", lambda p, **kwargs: _FakeDB())
    monkeypatch.setattr(recall_mod, "guard_paths",
                        lambda profile, clone, db: (Path("/tmp/c"), Path("/tmp/d")))

    recall_mod.recall("q", cfg=_cfg(), include_core=False)

    assert called == [], "reindex must not run when HEAD is unknown"


# --------------------------------------------------------------------------
# Deadlines must bound TOTAL wall time. httpx.Timeout(0.8) sets every phase to
# 0.8 independently, so an "800ms" call could spend ~2.4s.
# --------------------------------------------------------------------------

def test_deadline_timeout_clamps_connect_and_pool():
    from memd.embed import _deadline_timeout
    t = _deadline_timeout(800)
    assert t.read == pytest.approx(0.8)
    assert t.connect <= 0.15
    assert t.pool <= 0.05


def test_deadline_timeout_never_exceeds_a_tiny_budget():
    from memd.embed import _deadline_timeout
    t = _deadline_timeout(50)
    assert t.connect <= 0.05 and t.pool <= 0.05 and t.read == pytest.approx(0.05)


# --------------------------------------------------------------------------
# Concurrency: recall must not die with "database is locked" when a save commits.
# --------------------------------------------------------------------------

def test_open_db_sets_wal_and_busy_timeout(tmp_path):
    from memd.index import open_db
    db = open_db(tmp_path / "x.db")
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 2000
    finally:
        db.close()


# --------------------------------------------------------------------------
# Write path: a tombstone is not an identity, an unchanged re-save is not an
# error, and reflect's duplicate gates must not change which pairs are found.
# --------------------------------------------------------------------------

def _clone(tmp_path):
    import subprocess
    c = tmp_path / "clone"
    c.mkdir()
    for args in (["init", "-q", str(c)],):
        subprocess.run(["git", *args], check=True)
    subprocess.run(["git", "-C", str(c), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(c), "config", "user.name", "t"], check=True)
    return c


def test_commit_reports_nothing_to_commit_instead_of_raising(tmp_path):
    """Re-saving an unchanged fact writes identical bytes; that must not be an
    error, or every idempotent re-assert becomes an HTTP 500 that never converges."""
    import subprocess
    from memd.save import _commit
    c = _clone(tmp_path)
    (c / "n.md").write_text("x\n")
    assert _commit(c, "first", paths=[c / "n.md"]) is True
    assert _commit(c, "second", paths=[c / "n.md"]) is False
    log = subprocess.run(["git", "-C", str(c), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert log.count("\n") == 1, "the no-op must not create a second commit"


def test_save_does_not_update_a_superseded_tombstone(tmp_path, monkeypatch):
    """Writing into a tombstone was accepted, then pruned from the index — the
    fact was permanently unrecallable behind an HTTP 200."""
    from memd import save as save_mod
    c = _clone(tmp_path)
    (c / "dead.md").write_text(
        "---\ntitle: Dead\nslug: dead\nprofile: amber\nhost: any\n"
        "importance: 3\nsuperseded_by: alive\ntags: []\ngrounding: ok\n---\nold\n"
    )
    note = save_mod.read_note(c, "dead")
    assert note is not None and note.superseded_by == "alive"

    monkeypatch.setattr(save_mod, "_bm25_strong_match", lambda *a, **k: None)
    monkeypatch.setattr(save_mod, "_pull_rebase_push", lambda clone: None)

    from memd.config import Config
    cfg = Config(clone=c, db=tmp_path / "d.db", profile="amber")
    res = save_mod.save({"title": "Dead", "body": "new fact"}, cfg=cfg)

    assert res.action != "updated", "must not update a tombstone"
    assert save_mod.read_note(c, "dead").superseded_by == "alive"
    assert save_mod.read_note(c, res.slug).body == "new fact"
    assert res.lexical_indexed is True


def test_reflect_duplicate_gates_are_exact():
    """The prefilters are upper bounds on ratio(), so the reported pairs must be
    byte-identical to the unfiltered comparison."""
    import datetime as dt
    import difflib
    import random
    import string
    from memd.reflect import build_report, _DUP_RATIO

    random.seed(11)
    def body(n):
        return " ".join("".join(random.choices(string.ascii_lowercase,
                                               k=random.randint(3, 10)))
                        for _ in range(n))
    notes = [{"slug": f"n{i}", "body": body(random.randint(20, 400)),
              "last_used": ""} for i in range(40)]
    for a, b in [(0, 1), (7, 8)]:
        notes[b]["body"] = notes[a]["body"][:-5] + " end"

    expected = []
    for i in range(len(notes)):
        for j in range(i + 1, len(notes)):
            if difflib.SequenceMatcher(None, notes[i]["body"],
                                       notes[j]["body"]).ratio() >= _DUP_RATIO:
                expected.append((notes[i]["slug"], notes[j]["slug"]))

    got = [tuple(x) for x in
           build_report(notes, dt.datetime.now(dt.timezone.utc))["duplicates"]]
    assert sorted(got) == sorted(expected)
    assert len(expected) >= 2, "fixture must actually contain duplicates"
