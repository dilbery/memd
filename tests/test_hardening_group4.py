"""FIX GROUP 4: high-value-hardening — REAL-path tests (NO core-seam monkeypatch).

These drive the REAL save()/reflect/store/degraded_grep/server against a temp git
clone + sqlite db (the `config`/`git_clone` fixtures) and real local git remotes.
Only the embedding call is faked where an index has to be built — the
core seam (_core_recall / _core_save / _bm25_strong_match / make_branch) is driven
unchanged. No HTTP to a live model service is made.

Each test fails BEFORE its fix and passes after.

Covers:
  (a) reflect serializes against save via an flock + restores the ORIGINAL branch
      in a finally, and detects the default branch via origin/HEAD (never the
      hardcoded literal 'main').
  (b) save() unifies dedup on dedup.best_match (-3.0 + 0.5 token-overlap backstop)
      so it catches a high-overlap near-duplicate on a TINY corpus where the old
      magnitude-only bm25 <= -2.0 probe collapsed and silently created a dup.
  (c) the SHIPPED server app exposes exactly one auth implementation: /save is
      guarded by a single require_token (Bearer MEMD_TOKEN); the dead X-Memd-Token
      factory is gone.
  (d) a conflict save for a slug with NO existing note creates instead of
      orphan-superseding; and a collision-resistant new_slug never clobbers an
      existing file.
  (e) the carved index files {MEMORY.md, MEMORY-full.md, README.md} are skipped
      everywhere via the shared store.CARVED_INDEX_FILES set.
"""
import os
import subprocess
import threading
import time

import pytest

import memd.index as index_mod
import memd.save as save_mod
from memd.index import open_db, build_index
from memd.save import save
from memd.store import CARVED_INDEX_FILES, read_note


def _git(clone, *args):
    return subprocess.run(
        ["git", "-C", str(clone), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def _no_push(clone):
    return None  # never reach the network on the save() git path


def _fake_embed(texts, cfg):
    return [[1.0] * 768 for _ in texts]


# ---------------------------------------------------------------------------
# (e) shared CARVED_INDEX_FILES constant used everywhere
# ---------------------------------------------------------------------------


def test_carved_index_files_is_the_shared_set():
    assert CARVED_INDEX_FILES == {"MEMORY.md", "MEMORY-full.md", "README.md"}


def test_store_reflect_degraded_grep_share_one_constant():
    import memd.integrations.degraded_grep as dg
    import memd.reflect as reflect
    import memd.store as store
    # All three modules resolve the SAME object — no drifting per-module literals.
    assert dg.CARVED_INDEX_FILES is store.CARVED_INDEX_FILES
    assert reflect.CARVED_INDEX_FILES is store.CARVED_INDEX_FILES


def test_degraded_grep_skips_all_carved_index_files(tmp_path):
    """The fallback must not return MEMORY-full.md / README.md (was only skipping
    MEMORY.md). Drive the REAL grep_recall over a temp checkout."""
    from memd.integrations.degraded_grep import grep_recall

    checkout = tmp_path / "ro-clone"
    checkout.mkdir()
    # A real note that should be returned.
    (checkout / "real-note.md").write_text(
        "---\ntitle: Real\nslug: real-note\n---\nunicorntoken lives here\n"
    )
    # Carved index artifacts that must be ignored even though they contain the term.
    for carved in CARVED_INDEX_FILES:
        (checkout / carved).write_text(f"# {carved}\n\n- unicorntoken in index\n")

    hits = grep_recall("unicorntoken", str(checkout), top_n=8)
    slugs = {h["slug"] for h in hits}
    assert "real-note" in slugs
    for carved in CARVED_INDEX_FILES:
        assert carved[:-3] not in slugs, f"degraded grep returned carved file {carved}"


def test_reflect_load_notes_skips_all_carved_index_files(tmp_path, monkeypatch):
    """reflect._load_notes must skip README.md too (was only MEMORY/MEMORY-full)."""
    import memd.reflect as reflect

    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "a-real-note.md").write_text(
        "---\ntitle: A Real Note\nslug: a-real-note\n---\nbody one\n"
    )
    for carved in CARVED_INDEX_FILES:
        (clone / carved).write_text(f"# {carved}\n\nindex body\n")

    monkeypatch.setenv("MEMD_CLONE", str(clone))
    notes = reflect._load_notes()
    slugs = {n["slug"] for n in notes}
    assert "a-real-note" in slugs
    for carved in CARVED_INDEX_FILES:
        assert carved[:-3] not in slugs, f"reflect ingested carved file {carved}"


# ---------------------------------------------------------------------------
# (b) save() dedup unified on dedup.best_match (overlap backstop included)
# ---------------------------------------------------------------------------


def _tiny_clone(tmp_path):
    """A git clone with a SINGLE seed note — the corpus size where bm25's IDF term
    collapses toward 0 and a magnitude-only probe can't flag a near-duplicate."""
    clone = tmp_path / "tinyclone"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "config", "user.email", "t@memd")
    _git(clone, "config", "user.name", "memd-test")
    (clone / "trackr-docker-host.md").write_text(
        "---\ntitle: Trackr Docker Host\nslug: trackr-docker-host\nprofile: amber\n"
        "host: gpuhost\nimportance: 3\ntags: []\ngrounding: ok\n---\n"
        "Trackr Docker containers on 10.10.1.11 svcuser prometheus loki alloy "
        "SSH svcuser 10.10.1.11\n"
    )
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "seed one note")
    return clone


def test_save_dedup_uses_best_match_overlap_backstop(tmp_path, monkeypatch):
    """On a TINY (one-note) corpus, a high-token-OVERLAP near-duplicate against a
    DISTINCT note must be caught by save() the way save_lint/carve catch it.

    The old save() used a magnitude-only bm25 <= -2.0 SQL probe. On a one-note
    index bm25's IDF term collapses toward 0 (score ~ -1e-5), so that probe never
    fired and save() happily wrote a SECOND near-identical note (action=created).
    dedup.best_match adds a >=0.5 token-overlap backstop, so the unified path now
    recognises the dup and routes it through the supersede arm (distinct slug,
    matched note marked superseded — never silently duplicated)."""
    import dataclasses

    from memd.config import Config

    clone = _tiny_clone(tmp_path)
    cfg = Config(clone=clone, db=tmp_path / "memd.db", profile="amber", token="t")

    monkeypatch.setattr(index_mod, "embed", _fake_embed)
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)

    db = open_db(cfg.db)
    build_index(db, cfg)
    db.close()

    res = save(
        {"title": "Trackr containers host",
         "body": ("Trackr Docker containers on 10.10.1.11 svcuser prometheus "
                  "loki alloy SSH svcuser"),
         "host": "gpuhost"},
        profile="amber", cfg=cfg,
    )

    # Similarity is reported for review; retiring another fact requires an
    # explicit supersedes target so related knowledge remains recallable.
    assert res.action == "created"
    assert "trackr-docker-host" in res.related
    old = read_note(cfg.clone, "trackr-docker-host")
    assert old is not None and old.superseded_by is None


def test_bm25_strong_match_matches_dedup_best_match(tmp_path, monkeypatch):
    """Direct seam check: save._bm25_strong_match must agree with dedup.best_match
    on the live indexed corpus (same threshold + overlap backstop) — including on
    the tiny corpus where the old magnitude-only probe disagreed (returned None)."""
    import dataclasses

    from memd.config import Config
    from memd.dedup import best_match, build_bm25
    from memd.store import Note

    clone = _tiny_clone(tmp_path)
    cfg = Config(clone=clone, db=tmp_path / "memd.db", profile="amber", token="t")

    monkeypatch.setattr(index_mod, "embed", _fake_embed)

    db = open_db(cfg.db)
    build_index(db, cfg)
    db.close()

    title = "Trackr containers host"
    body = "Trackr Docker containers on 10.10.1.11 svcuser prometheus loki alloy SSH svcuser"

    # What the unified save seam returns.
    seam = save_mod._bm25_strong_match(cfg, title, body)

    # What dedup.best_match returns over the same live corpus.
    from memd.store import list_notes
    existing = [n for n in list_notes(cfg.clone) if not n.superseded_by]
    idx = build_bm25(existing)
    cand = Note(slug="", path="", title=title, body=body)
    direct = best_match(idx, existing, cand)
    idx.close()

    assert seam == (direct.slug if direct else None)
    assert seam == "trackr-docker-host"


# ---------------------------------------------------------------------------
# (d) conflict supersede must not orphan; new_slug must be collision-resistant
# ---------------------------------------------------------------------------


def test_conflict_for_unknown_slug_creates_not_orphan_supersedes(tmp_path, monkeypatch):
    """A conflict save whose slug names NO existing note must CREATE the note, not
    fabricate a superseded->dangling-slug orphan. The old code blindly wrote a
    new `<slug>-<rev>` note and tried to mark a non-existent `<slug>` as
    superseded — leaving a confusing renamed orphan and no clean note at `<slug>`."""
    import dataclasses

    from memd.config import Config

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "config", "user.email", "t@memd")
    _git(clone, "config", "user.name", "memd-test")
    (clone / "unrelated.md").write_text(
        "---\ntitle: Unrelated\nslug: unrelated\nprofile: amber\nhost: gpuhost\n"
        "importance: 3\ntags: []\ngrounding: ok\n---\nnothing to do with it\n"
    )
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "seed")

    cfg = Config(clone=clone, db=tmp_path / "memd.db", profile="amber", token="t")
    monkeypatch.setattr(index_mod, "embed", _fake_embed)
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)

    res = save(
        {"title": "Totally New Fact", "body": "fresh body", "host": "gpuhost",
         "conflict": True},
        profile="amber", cfg=cfg,
    )

    # No prior note named totally-new-fact existed -> this is a plain create at
    # the natural slug, with NOTHING orphaned/renamed.
    assert res.action == "created"
    assert res.slug == "totally-new-fact"
    # Brand-new note files use the shared underscore convention (note_filename),
    # the SAME one save_lint emits — never a hyphen file that would fragment the
    # note into two.
    assert (clone / "totally_new_fact.md").exists()
    assert not (clone / "totally-new-fact.md").exists()
    # The clean slug file is the one written; no `-<rev>` orphan was produced.
    orphans = [p.name for p in clone.glob("totally?new?fact?*.md")]
    assert orphans == [], f"conflict-for-unknown-slug produced orphan(s): {orphans}"


def test_conflict_new_slug_is_collision_resistant(tmp_path, monkeypatch):
    """When the conflict's `<slug>-<short-rev>` target already exists on disk, the
    new note must get a DIFFERENT, non-clobbering slug. The old code used only the
    HEAD short-rev: if that exact file already existed (e.g. a prior supersede at
    the same HEAD), it silently overwrote it."""
    import dataclasses

    from memd.config import Config

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "config", "user.email", "t@memd")
    _git(clone, "config", "user.name", "memd-test")
    # The note being superseded.
    (clone / "fact.md").write_text(
        "---\ntitle: Fact\nslug: fact\nprofile: amber\nhost: gpuhost\n"
        "importance: 3\ntags: []\ngrounding: ok\n---\noriginal fact body\n"
    )
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "seed")

    cfg = Config(clone=clone, db=tmp_path / "memd.db", profile="amber", token="t")
    monkeypatch.setattr(index_mod, "embed", _fake_embed)
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)

    # Pre-create the EXACT `<slug>-<short-rev>` file the old code would have
    # chosen against the CURRENT HEAD, holding hand-authored content that must
    # NOT be clobbered. Left uncommitted so HEAD (and thus the short-rev the save
    # computes) does not move — the save would target this very file.
    short = save_mod._short_rev(clone)
    squatter = clone / f"fact-{short}.md"
    squatter.write_text(
        "---\ntitle: Pre-existing Supersede\nslug: fact-" + short + "\nprofile: amber\n"
        "host: gpuhost\nimportance: 3\ntags: []\ngrounding: ok\n---\n"
        "DO NOT CLOBBER — hand-authored content\n"
    )

    res = save(
        {"title": "Fact", "body": "revised fact body", "host": "gpuhost",
         "conflict": True},
        profile="amber", cfg=cfg,
    )

    assert res.action == "superseded"
    # The new note did NOT reuse the squatter's slug.
    assert res.slug != f"fact-{short}", "conflict new_slug collided with an existing file"
    # The squatter's hand-authored content is intact.
    assert "DO NOT CLOBBER" in squatter.read_text()
    # The original note is superseded by the freshly-chosen slug.
    old = read_note(clone, "fact")
    assert old is not None and old.superseded_by == res.slug


# ---------------------------------------------------------------------------
# (c) the SHIPPED app has exactly one auth implementation (Bearer MEMD_TOKEN)
# ---------------------------------------------------------------------------


def test_shipped_app_requires_memd_token(monkeypatch):
    """The module-level `app` (what ships) must guard /save with require_token.
    No token -> 401; wrong token -> 401; correct Bearer MEMD_TOKEN -> 200."""
    from fastapi.testclient import TestClient

    import memd.server as srv

    monkeypatch.setenv("MEMD_TOKEN", "ship-secret")

    class _SaveResult:
        def to_dict(self):
            return {"slug": "n1", "action": "created"}

    # Fake ONLY the I/O core (no model service/git). The auth seam is REAL.
    monkeypatch.setattr(srv, "_core_save", lambda fact, profile="amber": _SaveResult())
    c = TestClient(srv.app)

    assert c.post("/save", json={"title": "T", "body": "B"}).status_code == 401
    assert c.post(
        "/save", json={"title": "T", "body": "B"},
        headers={"Authorization": "Bearer nope"},
    ).status_code == 401
    r = c.post(
        "/save", json={"title": "T", "body": "B"},
        headers={"Authorization": "Bearer ship-secret"},
    )
    assert r.status_code == 200
    assert r.json()["action"] == "created"


def test_server_has_single_auth_dependency():
    """No dead second auth factory: the X-Memd-Token `create_app` is gone, leaving
    one require_token shared by the shipped app."""
    import memd.server as srv

    assert not hasattr(srv, "create_app"), "dead X-Memd-Token create_app still present"
    assert hasattr(srv, "require_token")
    src = open(srv.__file__, encoding="utf-8").read()
    # The only header the auth path reads is the Bearer Authorization header.
    assert "X-Memd-Token" not in src
    assert "x_memd_token" not in src


# ---------------------------------------------------------------------------
# (a) reflect: origin/HEAD default-branch detection + flock + branch restore
# ---------------------------------------------------------------------------


def _clone_with_remote(tmp_path, default_branch):
    """A working clone wired to a bare remote whose default branch is
    `default_branch` (NOT 'main'), so a hardcoded 'main' would break."""
    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", default_branch, str(bare)],
        check=True, capture_output=True,
    )
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)],
                   check=True, capture_output=True)
    _git(clone, "config", "user.email", "t@memd")
    _git(clone, "config", "user.name", "memd-test")
    (clone / "a-note.md").write_text(
        "---\ntitle: A\nslug: a\n---\nbody a\n"
    )
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "seed")
    _git(clone, "push", "-q", "-u", "origin", default_branch)
    _git(clone, "remote", "set-head", "origin", "-a")
    return clone, bare


def test_reflect_detects_default_branch_via_origin_head(tmp_path, monkeypatch):
    """make_branch must return to the repo's ACTUAL default branch (detected via
    origin/HEAD), not a hardcoded 'main'. Here the default is 'trunk'."""
    import memd.reflect as reflect

    clone, _bare = _clone_with_remote(tmp_path, "trunk")
    monkeypatch.setenv("MEMD_CLONE", str(clone))

    report = {"generated": "2026-06-16T00:00:00+00:00", "n_notes": 1,
              "duplicates": [], "stale": []}
    branch = reflect.make_branch(report, "draft body\n")

    assert branch.startswith("reflect/")
    # After make_branch we are back on the detected default branch (trunk),
    # never the hardcoded 'main' (which does not exist here).
    head = _git(clone, "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert head == "trunk", f"reflect did not restore the default branch (on {head})"


def test_reflect_default_branch_helper_reads_origin_head(tmp_path, monkeypatch):
    """The detection helper resolves origin/HEAD; falls back to 'main' off-remote."""
    import memd.reflect as reflect

    clone, _bare = _clone_with_remote(tmp_path, "trunk")
    assert reflect._default_branch(clone) == "trunk"

    # A bare local repo with no origin/HEAD falls back to the literal default.
    norem = tmp_path / "norem"
    norem.mkdir()
    _git(norem, "init", "-q")
    _git(norem, "config", "user.email", "t@memd")
    _git(norem, "config", "user.name", "memd-test")
    (norem / "x.md").write_text("hi")
    _git(norem, "add", "-A")
    _git(norem, "commit", "-q", "-m", "x")
    assert reflect._default_branch(norem) == "main"


def test_reflect_make_branch_takes_lock_and_serializes(tmp_path, monkeypatch):
    """make_branch must hold an flock for its whole git sequence so a concurrent
    save() git op cannot interleave on the shared clone. We prove the lock exists
    and is exclusive: while a holder owns it, make_branch blocks, then proceeds."""
    import memd.reflect as reflect

    clone, _bare = _clone_with_remote(tmp_path, "trunk")
    monkeypatch.setenv("MEMD_CLONE", str(clone))

    lock_path = reflect._lock_path(clone)
    import fcntl

    holder = open(lock_path, "w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

    done = threading.Event()
    started = threading.Event()

    def run():
        started.set()
        reflect.make_branch(
            {"generated": "2026-06-16T00:00:00+00:00", "n_notes": 1,
             "duplicates": [], "stale": []},
            "draft\n",
        )
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    started.wait(2)
    # While we hold the lock, make_branch must NOT complete.
    assert not done.wait(0.5), "make_branch ignored the flock (did not serialize)"
    # Release -> make_branch proceeds and finishes.
    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    holder.close()
    assert done.wait(5), "make_branch never completed after lock release"
    t.join(5)


def test_reflect_restores_original_branch_on_push_failure(tmp_path, monkeypatch):
    """If push fails mid-flight, make_branch must restore the ORIGINAL branch in a
    finally (never strand the shared clone on the reflect/ branch)."""
    import memd.reflect as reflect

    clone, _bare = _clone_with_remote(tmp_path, "trunk")
    monkeypatch.setenv("MEMD_CLONE", str(clone))

    real_run = subprocess.run

    def boom(cmd, *a, **k):
        if isinstance(cmd, list) and "push" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(reflect.subprocess, "run", boom)

    with pytest.raises(subprocess.CalledProcessError):
        reflect.make_branch(
            {"generated": "2026-06-16T00:00:00+00:00", "n_notes": 1,
             "duplicates": [], "stale": []},
            "draft\n",
        )

    head = _git(clone, "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert head == "trunk", f"reflect stranded the clone on {head} after push failure"
