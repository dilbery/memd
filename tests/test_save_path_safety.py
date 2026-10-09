"""FIX GROUP 3: save-path-safety — REAL-path tests (NO core-seam monkeypatch).

These drive the REAL recall()/save()/index against the temp git clone + sqlite
db from the `config` fixture. Only the embedding and reranking HTTP calls
are faked via respx — the core seam (_core_recall /
_core_save / _bm25_strong_match / reindex) is untouched. Each test fails before
the fix and passes after.

Covers:
  (a) NON-CLOBBER upsert — a near-but-distinct save must NOT wholesale-overwrite
      an existing hand-edited human body on a fuzzy BM25 match.
  (b) SUPERSEDED FILTERING — a superseded note never appears in recall even when
      it matches; superseded notes are not indexed (vec_notes/fts_notes) and are
      pruned on reindex; save's BM25 dedup never lands on a superseded note.
  (c) STALE-HEAD before dedup — save() runs the git_head-vs-index reindex check
      (like recall) BEFORE its dedup decision.
"""
from memd.config import DEFAULT_EMBED_URL, DEFAULT_RERANK_PATH, DEFAULT_RERANK_URL
import json
import subprocess

import httpx
import respx

import memd.index as index_mod
import memd.save as save_mod
from memd.index import open_db, build_index, reindex, head_in_index, lexical_head_in_index
from memd.recall import recall
from memd.save import save
from memd.store import read_note

# Derived from the config defaults so they cannot go stale again: the embedding
# URL was once hard-coded here and silently stopped intercepting when the
# default endpoint changed.
EMBED_URL = f"{DEFAULT_EMBED_URL}/v1/embeddings"
RERANK_URL = f"{DEFAULT_RERANK_URL.rstrip('/')}{DEFAULT_RERANK_PATH}"


def _embed_response(request: httpx.Request) -> httpx.Response:
    """Deterministic 768-dim embeddings, one per input text (embedding fake)."""
    payload = json.loads(request.content)
    texts = payload["input"]
    if isinstance(texts, str):
        texts = [texts]
    data = [{"embedding": [float(len(t) % 7) + 1.0] * 768} for t in texts]
    return httpx.Response(200, json={"data": data})


def _no_push(clone):
    return None  # never reach the network on the save() git path


def _git(clone, *args):
    return subprocess.run(
        ["git", "-C", str(clone), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def _write_superseded_note(clone, *, slug, title, body, superseded_by, importance=2):
    """Commit a note carrying superseded_by into the clone (so list_notes sees it)."""
    fm = (
        f"---\ntitle: {title}\nslug: {slug}\nprofile: amber\nhost: gpuhost\n"
        f"importance: {importance}\nsuperseded_by: {superseded_by}\n"
        f"tags: []\ngrounding: ok\n---\n{body}\n"
    )
    (clone / f"{slug}.md").write_text(fm)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", f"seed superseded {slug}")


# ---------------------------------------------------------------------------
# (b) SUPERSEDED FILTERING in recall
# ---------------------------------------------------------------------------


@respx.mock
def test_superseded_note_never_appears_in_recall(config, git_clone, monkeypatch):
    """A superseded note that strongly matches the query must NOT come back."""
    respx.post(EMBED_URL).mock(side_effect=_embed_response)
    respx.post(RERANK_URL).mock(return_value=httpx.Response(503))  # fall to BM25

    # A superseded note with a rare token that would otherwise dominate FTS5/vec.
    _write_superseded_note(
        git_clone,
        slug="old-superseded-fact",
        title="Old Superseded Fact",
        body="zzqwxy unique marker token only in the superseded note",
        superseded_by="new-replacement-fact",
    )

    # Build the index from the FULL clone (real path; no core-seam stub).
    db = open_db(config.db)
    build_index(db, config)
    db.close()

    out = recall("zzqwxy unique marker token", profile="amber", k=8, cfg=config)
    slugs = [n.slug for n in out]
    assert "old-superseded-fact" not in slugs, (
        "superseded note leaked into recall via vector/bm25 arm"
    )


@respx.mock
def test_superseded_note_not_inserted_into_index_tables(config, git_clone, monkeypatch):
    """Superseded notes must not be inserted into vec_notes / fts_notes at all."""
    respx.post(EMBED_URL).mock(side_effect=_embed_response)

    _write_superseded_note(
        git_clone,
        slug="dead-note",
        title="Dead Note",
        body="raretoken-deadbeef content here",
        superseded_by="live-note",
    )

    db = open_db(config.db)
    build_index(db, config)
    try:
        fts = db.execute(
            "SELECT count(*) FROM fts_notes WHERE slug = ?", ("dead-note",)
        ).fetchone()[0]
        vec = db.execute(
            "SELECT count(*) FROM vec_notes WHERE slug = ?", ("dead-note",)
        ).fetchone()[0]
    finally:
        db.close()
    assert fts == 0, "superseded note was inserted into fts_notes"
    assert vec == 0, "superseded note was inserted into vec_notes"


@respx.mock
def test_reindex_prunes_a_newly_superseded_note(config, git_clone, monkeypatch):
    """When a live note becomes superseded, reindex must drop it from the tables."""
    respx.post(EMBED_URL).mock(side_effect=_embed_response)

    # Start with a LIVE note (no superseded_by) carrying a rare token.
    (git_clone / "soon-dead.md").write_text(
        "---\ntitle: Soon Dead\nslug: soon-dead\nprofile: amber\nhost: gpuhost\n"
        "importance: 2\ntags: []\ngrounding: ok\n---\n"
        "qpzm-prune-token live for now\n"
    )
    _git(git_clone, "add", "-A")
    _git(git_clone, "commit", "-q", "-m", "add soon-dead live")

    db = open_db(config.db)
    build_index(db, config)
    # Sanity: it is indexed while live.
    assert db.execute(
        "SELECT count(*) FROM fts_notes WHERE slug = ?", ("soon-dead",)
    ).fetchone()[0] == 1

    # Now mark it superseded and reindex.
    (git_clone / "soon-dead.md").write_text(
        "---\ntitle: Soon Dead\nslug: soon-dead\nprofile: amber\nhost: gpuhost\n"
        "importance: 2\nsuperseded_by: replacement\ntags: []\ngrounding: ok\n---\n"
        "qpzm-prune-token now superseded\n"
    )
    _git(git_clone, "add", "-A")
    _git(git_clone, "commit", "-q", "-m", "supersede soon-dead")

    reindex(db, config)
    try:
        fts = db.execute(
            "SELECT count(*) FROM fts_notes WHERE slug = ?", ("soon-dead",)
        ).fetchone()[0]
        vec = db.execute(
            "SELECT count(*) FROM vec_notes WHERE slug = ?", ("soon-dead",)
        ).fetchone()[0]
    finally:
        db.close()
    assert fts == 0, "reindex did not prune the newly-superseded note from fts_notes"
    assert vec == 0, "reindex did not prune the newly-superseded note from vec_notes"


# ---------------------------------------------------------------------------
# (b) SUPERSEDED FILTERING in save() dedup
# ---------------------------------------------------------------------------


@respx.mock
def test_save_dedup_never_lands_on_superseded_note(config, git_clone, monkeypatch):
    """save()'s BM25 strong-match must not upsert onto a superseded note."""
    monkeypatch.setattr(index_mod, "embed",
                        lambda texts, cfg: [[1.0] * 768 for _ in texts])
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)

    # A superseded note whose body is a near-identical strong BM25 match.
    shared = ("monitorx alpha beta gamma delta epsilon zeta eta theta iota "
              "kappa lambda mu nu xi omicron pi rho sigma tau")
    _write_superseded_note(
        git_clone,
        slug="superseded-twin",
        title="Superseded Twin",
        body=shared,
        superseded_by="some-newer-note",
    )

    db = open_db(config.db)
    build_index(db, config)
    db.close()

    # Saving a strongly-overlapping fact must NOT target the superseded twin.
    res = save(
        {"title": "Fresh Monitor Fact", "body": shared + " plus new detail",
         "host": "gpuhost"},
        profile="amber", cfg=config,
    )
    assert res.slug != "superseded-twin", (
        "save upserted onto a superseded note (BM25 dedup ignored superseded_by)"
    )
    # the superseded note's body is untouched
    old = read_note(config.clone, "superseded-twin")
    assert old is not None and "plus new detail" not in old.body


# ---------------------------------------------------------------------------
# (a) NON-CLOBBER upsert
# ---------------------------------------------------------------------------


@respx.mock
def test_near_but_distinct_save_does_not_overwrite_human_body(config, git_clone, monkeypatch):
    """A fuzzy BM25 near-match to a DIFFERENT note must not clobber its body."""
    monkeypatch.setattr(index_mod, "embed",
                        lambda texts, cfg: [[1.0] * 768 for _ in texts])
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)

    # A hand-edited human note with a distinct title/slug but overlapping words.
    human_body = (
        "HUMAN-CURATED: engine vulkan tuning notes that Amber hand-wrote and "
        "must be preserved verbatim batch 1024 sweet spot model3 35b mtp spec decode "
        "do not lose this carefully edited paragraph"
    )
    (git_clone / "human-tuning-note.md").write_text(
        "---\ntitle: Human Tuning Note\nslug: human-tuning-note\nprofile: amber\n"
        "host: gpuhost\nimportance: 3\ntags: []\ngrounding: ok\n---\n"
        f"{human_body}\n"
    )
    _git(git_clone, "add", "-A")
    _git(git_clone, "commit", "-q", "-m", "add human tuning note")

    db = open_db(config.db)
    build_index(db, config)
    db.close()

    # Save a related-but-distinct fact: different title (different slug), body
    # shares enough vocabulary to trip a strong BM25 match against the human note.
    res = save(
        {"title": "Reranker Endpoint Detail",
         "body": ("engine vulkan tuning batch 1024 sweet spot model3 35b mtp spec "
                  "decode reranker model endpoint is /v1/rerank on port 9000"),
         "host": "gpuhost"},
        profile="amber", cfg=config,
    )

    # The human note's hand-edited body must survive intact.
    human = read_note(config.clone, "human-tuning-note")
    assert human is not None
    assert "carefully edited paragraph" in human.body, (
        "fuzzy save wholesale-overwrote a hand-edited human body"
    )
    assert "reranker model endpoint" not in human.body, (
        "fuzzy save clobbered the human body with the incoming body"
    )
    # And the new distinct fact still lands somewhere (created or superseded path).
    assert res.slug != "human-tuning-note" or res.action != "updated"


# ---------------------------------------------------------------------------
# (c) STALE-HEAD before dedup
# ---------------------------------------------------------------------------


def test_save_reads_git_for_dedup_when_index_is_stale(config, git_clone, monkeypatch):
    """An out-of-band note informs suggestions without requiring embeddings."""
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)
    (git_clone / "out-of-band.md").write_text(
        "---\ntitle: Out Of Band\nslug: out-of-band\nprofile: amber\n"
        "host: remote\nimportance: 2\n---\n"
        "wibble-oob-token backup snapshots encrypted retention thirty days\n"
    )
    _git(git_clone, "add", "-A")
    _git(git_clone, "commit", "-qm", "out of band note")

    def no_embeddings(*args, **kwargs):
        raise AssertionError("save must not rebuild vectors before dedup")
    monkeypatch.setattr(index_mod, "embed", no_embeddings)
    result = save(
        {"title": "Related backup detail",
         "body": "wibble-oob-token backup snapshots encrypted retention thirty days",
         "host": "remote"}, cfg=config,
    )
    assert "out-of-band" in result.related
    assert read_note(git_clone, "out-of-band").superseded_by is None
    db = open_db(config.db)
    try:
        assert db.execute("SELECT count(*) FROM fts_notes WHERE slug='out-of-band'").fetchone()[0] == 1
        assert lexical_head_in_index(db) == _git(git_clone, "rev-parse", "HEAD").strip()
    finally:
        db.close()


@respx.mock
def test_saved_note_is_searchable_immediately(config, git_clone, monkeypatch):
    """A fact you just saved must be findable NOW, not after the next reindex.

    save() writes the file and commits, but does not index. Without the catch-up
    at the end of _commit_and_push the note stayed invisible to recall until
    something else happened to reindex -- the next save's _reindex_if_stale, or a
    process restart. Caught on a live deployment: a note was saved, committed
    and pushed, and was still absent from the index afterwards.
    """
    monkeypatch.setattr(index_mod, "embed",
                        lambda texts, cfg: [[1.0] * 768 for _ in texts])
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)

    db = open_db(config.db)
    build_index(db, config)
    db.close()

    save(
        {"title": "Freshly Saved Fact", "body": "zzfreshtoken unique body text",
         "host": "gpuhost"},
        profile="amber", cfg=config,
    )

    db = open_db(config.db)
    try:
        in_notes = db.execute(
            "SELECT count(*) FROM notes WHERE slug = ?", ("freshly-saved-fact",)
        ).fetchone()[0]
        in_fts = db.execute(
            "SELECT count(*) FROM fts_notes WHERE slug = ?", ("freshly-saved-fact",)
        ).fetchone()[0]
    finally:
        db.close()
    assert in_notes == 1, "saved note never reached the index -- it is not searchable"
    assert in_fts == 1, "saved note missing from fts_notes -- recall cannot match it"


@respx.mock
def test_save_leaves_the_keyword_index_head_current(config, git_clone, monkeypatch):
    """After a save the stored head must equal the clone head.

    A lagging head makes /health report git.in_sync false, which makes overall
    status "degraded", which had a deployed canary firing false alerts on a
    completely healthy server -- and again on every flip back to ok.
    """
    monkeypatch.setattr(index_mod, "embed",
                        lambda texts, cfg: [[1.0] * 768 for _ in texts])
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)

    db = open_db(config.db)
    build_index(db, config)
    db.close()

    save(
        {"title": "Head Sync Fact", "body": "another unrelated body", "host": "gpuhost"},
        profile="amber", cfg=config,
    )

    db = open_db(config.db)
    try:
        head_now = lexical_head_in_index(db)
    finally:
        db.close()
    assert head_now == _git(git_clone, "rev-parse", "HEAD").strip(), (
        "index head lags the clone after a save -- /health will report degraded"
    )
