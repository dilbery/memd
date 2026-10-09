"""mem-forget: conservative archive selection, review-branch proposals (plain and
encrypted), recall/core/read handling of archived notes, restore, health counts
and the nightly step.

Hermetic: temp git clones with fixed commit dates, a local usage log and facts
table, a fake embedder; no network.
"""
import datetime as dt
import json
import os
import subprocess

import pytest

import memd.index as index_mod
import memd.recall as recall_mod
import memd.save as save_mod
from memd import forget, insights, nightly
from memd.carve import select_core
from memd.config import Config
from memd.index import build_index, open_db
from memd.normalize import normalize_recall_args
from memd.read import read
from memd.recall import recall
from memd.render import render_result
from memd.store import is_archived, list_notes, parse_text, read_note
from memd.usage import log_recall

NOW = dt.datetime(2026, 9, 28, 12, tzinfo=dt.timezone.utc).timestamp()
SEED_DATE = "2025-01-15T10:00:00+00:00"
DAY = 86400


def _git(repo, *args, date=None):
    env = None
    if date:
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _md(title, *, importance=2, body=None, **fields):
    fm = [f"title: {title}", f"importance: {importance}"]
    for key, value in fields.items():
        fm.append(f"{key}: {value}")
    return "---\n" + "\n".join(fm) + "\n---\n" + (body or f"{title} body text.") + "\n"


NOTES = {
    # candidates
    "old_stale.md": _md("Old stale", importance=1, volatility="state", observed_at="'2025-01-10'",
                        body="The quokkaport relay listens on port 7000."),
    "old_unused.md": _md("Old unused"),
    "closed_facts.md": _md("Closed facts"),
    # no signal: recall still returns it
    "recalled.md": _md("Recalled", importance=1),
    # protected
    "pinned_note.md": _md("Pinned note", importance=1, pinned="true", volatility="state",
                          observed_at="'2025-01-10'"),
    "core_note.md": _md("Core note", importance=4, volatility="state", observed_at="'2025-01-10'"),
    "summary_source.md": _md("Summary source", importance=1),
    "summary.md": _md("'Current state: sources'", importance=3, kind="summary",
                      sources="[summary-source]", tags="[summary]"),
    "replacement.md": _md("Replacement", importance=1),
    "old_version.md": _md("Old version", importance=1, superseded_by="replacement"),
    "published_source.md": _md("Published source", importance=1),
    "published_copy.md": _md("Published copy", importance=3,
                             published_from="{store: amber, slug: published-source}"),
    # too important, too recent
    "important.md": _md("Important", importance=3, volatility="state", observed_at="'2025-01-10'"),
    "recent_edit.md": _md("Recent edit", importance=1, volatility="state", observed_at="'2025-01-10'"),
    "reverified.md": _md("Reverified", importance=1, volatility="state", verified_at="'2026-08-01'"),
    "newer_note.md": _md("Newer note", importance=3),
}
CANDIDATES = {"old-stale", "old-unused", "closed-facts"}


def _seed(repo):
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@memd")
    _git(repo, "config", "user.name", "memd-test")
    for name, text in NOTES.items():
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed", date=SEED_DATE)
    path = repo / "recent_edit.md"
    path.write_text(path.read_text() + "Edited again.\n")
    _git(repo, "commit", "-qam", "edit", date="2026-09-01T10:00:00+00:00")


def _facts(db_path):
    db = open_db(db_path)
    with db:
        rows = [(1, "closed-facts", "relay", "port", "7000", 2), (2, "newer-note", "relay", "port", "7100", None)]
        for fid, slug, subject, predicate, obj, closed in rows:
            db.execute("INSERT INTO facts(id, slug, git_blob, subject, predicate, object, subject_key, "
                       "predicate_key, object_key, closed_by, method) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (fid, slug, "b", subject, predicate, obj, subject, predicate, obj, closed, "pattern"))
    db.close()


@pytest.fixture
def store(tmp_path, monkeypatch):
    repo = tmp_path / "clone"
    _seed(repo)
    monkeypatch.setenv("MEMD_CLONE", str(repo))
    monkeypatch.setenv("MEMD_DB", str(tmp_path / "memd.db"))
    monkeypatch.setenv("MEMD_PROFILE", "amber")
    monkeypatch.setenv("MEMD_USAGE_LOG", "on")
    monkeypatch.setattr(save_mod, "_pull_rebase_push", lambda clone: None)
    cfg = Config.from_env(env_file=None)
    _facts(cfg.db)
    for ts in (NOW - 60 * DAY, NOW - DAY):
        log_recall(cfg.db, "relay port", ["recalled"], cfg=cfg, now=ts)
    return cfg


def _slugs(rows):
    return {r["slug"] for r in rows}


def _by_slug(rows):
    return {r["slug"]: r for r in rows}


# --------------------------------------------------------------------------- selection


def test_selects_only_old_unimportant_notes_with_a_signal(store):
    report = forget.run(store, dry_run=True, now=NOW)
    assert _slugs(report["candidates"]) == CANDIDATES
    rows = _by_slug(report["candidates"])
    assert any(r.startswith("stale: state note past its 30-day") for r in rows["old-stale"]["reasons"])
    assert any(r.startswith("never recalled or read in the usage window") for r in rows["old-unused"]["reasons"])
    assert "every fact (1) was closed by newer notes: newer-note" in rows["closed-facts"]["reasons"]
    for row in rows.values():
        assert row["reasons"][0].endswith("(at most 2), not pinned")
        assert row["reasons"][1].startswith("no activity since 2025-01-")
    assert rows["old-stale"]["last_activity"] == "2025-01-15" and rows["old-stale"]["basis"] == "last commit"
    # oldest activity first
    assert [r["slug"] for r in report["candidates"]][0] in CANDIDATES


def test_each_protection_is_explained(store):
    report = forget.run(store, dry_run=True, now=NOW)
    kept = {r["slug"]: r["reason"] for r in report["protected"]}
    assert kept["pinned-note"] == "pinned"
    assert kept["core-note"] == "core-eligible (importance 4)"
    assert kept["summary-source"] == "a source cited by summary current-state-sources"
    assert kept["current-state-sources"].startswith("a current-state summary")
    assert kept["replacement"] == "replaces superseded note old-version"
    assert kept["published-source"] == "the source of published note published-copy"
    chosen = _slugs(report["candidates"])
    assert not chosen & {"important", "recent-edit", "reverified", "recalled", "old-version", "newer-note"}
    assert not chosen & set(kept)


def test_thresholds_and_a_short_usage_window(store, tmp_path):
    report = forget.run(store, dry_run=True, now=NOW, max_importance=3)
    assert "important" in _slugs(report["candidates"])
    assert "old-stale" not in _slugs(forget.run(store, dry_run=True, now=NOW, days=700)["candidates"])
    # A usage log younger than MIN_USAGE_DAYS never calls a note unused.
    short = Config(clone=store.clone, db=tmp_path / "short.db", profile="amber", usage_log="on")
    log_recall(short.db, "relay", ["recalled"], cfg=short, now=NOW - 5 * DAY)
    log_recall(short.db, "relay", ["recalled"], cfg=short, now=NOW)
    report = forget.run(short, dry_run=True, now=NOW)
    assert "old-unused" not in _slugs(report["candidates"])
    assert "old-stale" in _slugs(report["candidates"])
    assert "at least 30 are needed" in report["evidence"]["usage"]["reason"]
    assert report["evidence"]["facts"]["available"] is False


def test_uncommitted_notes_are_never_candidates(store):
    (store.clone / "brand_new.md").write_text(_md("Brand new", importance=1, volatility="state",
                                                  observed_at="'2024-01-01'"))
    assert "brand-new" not in _slugs(forget.run(store, dry_run=True, now=NOW)["candidates"])


# --------------------------------------------------------------------------- proposals


def test_dry_run_writes_nothing(store):
    head, status = _git(store.clone, "rev-parse", "HEAD"), _git(store.clone, "status", "--porcelain")
    report = forget.run(store, dry_run=True, now=NOW)
    assert report["proposed"] == 3 and report["commit"] is None and report["branch"] is None
    assert "archived: '2026-09-28'" in report["_texts"]["old_stale.md"]
    assert subprocess.run(["git", "-C", str(store.clone), "rev-parse", "-q", "--verify", "memd/forget"],
                          capture_output=True).returncode != 0
    assert _git(store.clone, "rev-parse", "HEAD") == head
    assert _git(store.clone, "status", "--porcelain") == status


def test_proposes_on_a_review_branch_and_leaves_the_checkout(store):
    head, status = _git(store.clone, "rev-parse", "HEAD"), _git(store.clone, "status", "--porcelain")
    report = forget.run(store, now=NOW)
    assert report["branch"] == "memd/forget" and report["proposed"] == 3
    assert _git(store.clone, "rev-parse", "HEAD") == head
    assert _git(store.clone, "status", "--porcelain") == status
    assert _git(store.clone, "rev-parse", "memd/forget^") == head
    files = _git(store.clone, "diff", "--name-only", head, "memd/forget").split()
    assert sorted(files) == ["closed_facts.md", "old_stale.md", "old_unused.md"]
    note = parse_text(_git(store.clone, "show", "memd/forget:old_stale.md"), path="old_stale.md")
    assert note.metadata["archived"] == "2026-09-28" and "stale: state note" in note.metadata["archived_reason"]
    assert note.body == "The quokkaport relay listens on port 7000." and note.saved_by == "mem-forget"
    assert note.volatility == "state" and note.importance == 1
    message = _git(store.clone, "log", "-1", "--format=%B", "memd/forget")
    assert "Proposed-By: mem-forget" in message and "- old-unused: never recalled" in message
    # nothing is archived until the branch is merged
    assert not any(is_archived(n) for n in list_notes(store.clone))
    assert forget.pending(store.clone) == 3

    # a rerun (even days later) keeps the unchanged proposal as it is
    again = forget.run(store, now=NOW + 3 * DAY)
    assert again["commit"] == report["commit"] and all(c["reused"] for c in again["candidates"])


def test_max_defers_the_rest(store):
    report = forget.run(store, dry_run=True, now=NOW, max_proposals=1)
    assert report["proposed"] == 1
    assert sum(c["deferred"] for c in report["candidates"]) == 2


def test_proposals_on_an_encrypted_store(tmp_path, monkeypatch):
    from memd.cli_crypt import encrypt_store
    from memd.codec import GENERIC_PROPOSAL, codec_for
    from memd.crypt import generate_key_file
    repo = tmp_path / "clone"
    _seed(repo)
    key = tmp_path / "keys" / "store.key"
    generate_key_file(key)
    monkeypatch.setenv("MEMD_PROFILE", "amber")
    monkeypatch.setenv("MEMD_CLONE", str(repo))
    monkeypatch.setenv("MEMD_AMBER_KEY_FILE", str(key))
    monkeypatch.setenv("GIT_AUTHOR_DATE", SEED_DATE)
    monkeypatch.setenv("GIT_COMMITTER_DATE", SEED_DATE)
    encrypt_store(repo)
    monkeypatch.delenv("GIT_AUTHOR_DATE")
    monkeypatch.delenv("GIT_COMMITTER_DATE")
    cfg = Config(clone=repo, db=tmp_path / "memd.db", profile="amber")
    head, status = _git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain")
    report = forget.run(cfg, now=NOW)
    assert _slugs(report["candidates"]) == {"old-stale", "recent-edit"}   # the re-encryption commit is old
    assert _git(repo, "log", "-1", "--format=%B", "memd/forget") == GENERIC_PROPOSAL
    assert _git(repo, "rev-parse", "HEAD") == head and _git(repo, "status", "--porcelain") == status
    paths = _git(repo, "diff", "--name-only", head, "memd/forget").split()
    assert len(paths) == 2 and all(p.endswith(".md.enc") for p in paths)
    for path in paths:
        data = subprocess.run(["git", "-C", str(repo), "show", f"memd/forget:{path}"],
                              capture_output=True, check=True).stdout
        assert b"archived" not in data and b"quokkaport" not in data
        assert "archived: '2026-09-28'" in codec_for(repo).decode(path, data)[0]
    _git(repo, "merge", "-q", "--ff-only", "memd/forget")
    assert is_archived(read_note(repo, "old-stale"))


# --------------------------------------------------------------------------- after approval


def _approve(cfg, monkeypatch):
    forget.run(cfg, now=NOW)
    _git(cfg.clone, "merge", "-q", "--ff-only", "memd/forget")
    monkeypatch.setattr(index_mod, "embed", lambda texts, cfg: [[float(len(t) % 5) + 1.0] * 768 for t in texts])
    db = open_db(cfg.db)
    build_index(db, cfg)
    db.close()
    monkeypatch.setattr(recall_mod, "embed_with_deadline", lambda *a, **k: None)
    monkeypatch.setattr(recall_mod, "rerank", lambda *a, **k: None)


def test_recall_excludes_archived_notes_unless_asked(store, monkeypatch):
    _approve(store, monkeypatch)
    hits = recall("quokkaport relay", profile="amber", cfg=store, include_core=False)
    assert "old-stale" not in {n.slug for n in hits}
    found = recall("quokkaport relay", profile="amber", cfg=store, include_core=False, include_archived=True)
    assert found[0].slug == "old-stale" and is_archived(found[0])
    shaped = render_result([n.to_dict() for n in found], query="quokkaport relay")
    assert "### old-stale" in shaped["text"] and "(ARCHIVED 2026-09-28: kept for history" in shaped["text"]
    assert shaped["excerpts"][0]["archived"] == "2026-09-28"
    # a note that is not archived renders without the marker
    assert "ARCHIVED" not in render_result([read_note(store.clone, "recalled").to_dict()])["text"]
    assert normalize_recall_args({"q": "x", "include_archived": "true"})["include_archived"] is True
    assert "include_archived" not in normalize_recall_args({"q": "x", "include_archived": False})


def test_archived_notes_are_never_core(store, monkeypatch):
    path = store.clone / "core_note.md"
    path.write_text(path.read_text().replace("importance: 4", "importance: 4\narchived: '2026-09-01'"))
    _git(store.clone, "commit", "-qam", "archive a core note by hand")
    _approve(store, monkeypatch)
    core = recall("", profile="amber", cfg=store)
    assert "core-note" not in {n.slug for n in core} and "pinned-note" in {n.slug for n in core}
    chosen = select_core(list_notes(store.clone), 10_000)
    assert "core-note" not in {n.slug for n in chosen} and "pinned-note" in {n.slug for n in chosen}


def test_read_marks_an_archived_note(store, monkeypatch):
    _approve(store, monkeypatch)
    receipt = read("old-stale", profile="amber", cfg=store)
    assert receipt["ok"] and receipt["archived"] == "2026-09-28"
    assert "stale" in receipt["archived_reason"]
    assert receipt["notice"].startswith("ARCHIVED 2026-09-28 by review")
    assert "mem-forget restore old-stale" in receipt["notice"]
    assert "archived" not in read("recalled", profile="amber", cfg=store)


def test_restore_brings_one_back(store, monkeypatch, capsys):
    _approve(store, monkeypatch)
    head = _git(store.clone, "rev-parse", "HEAD")
    assert forget.main(["restore", "old-stale"]) == 0
    assert "restored old-stale" in capsys.readouterr().out
    note = read_note(store.clone, "old-stale")
    assert not is_archived(note) and "archived_reason" not in note.metadata
    assert note.body == "The quokkaport relay listens on port 7000."
    assert _git(store.clone, "rev-parse", "HEAD^") == head
    assert "Saved-By: mem-forget" in _git(store.clone, "log", "-1", "--format=%B")
    assert _git(store.clone, "status", "--porcelain") == ""
    # restoring what is not archived says so and fails
    assert forget.main(["restore", "old-stale", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["restored"] is False and "not archived" in out["err"]
    assert forget.restore(store, "no-such-note")["err"] == "no note with slug 'no-such-note'"


def test_restore_of_a_note_only_proposed_points_at_the_branch(store):
    forget.run(store, now=NOW)
    out = forget.restore(store, "old-unused")
    assert out["restored"] is False and "only proposed on memd/forget" in out["err"]


def test_saving_an_archived_note_brings_it_back(store, monkeypatch):
    _approve(store, monkeypatch)
    save_mod.save({"slug": "old-unused", "title": "Old unused", "body": "Still true today."}, cfg=store)
    note = read_note(store.clone, "old-unused")
    assert not is_archived(note) and "archived_reason" not in note.metadata


# --------------------------------------------------------------------------- health, nightly, CLI


def test_health_counts_archived_and_pending(store, monkeypatch):
    monkeypatch.setattr(index_mod, "embed", lambda texts, cfg: [[1.0] * 768 for t in texts])
    db = open_db(store.db)
    build_index(db, store)
    db.close()
    forget.run(store, now=NOW)
    before = insights.compute(store, "amber", clone=store.clone, db_path=store.db, now=NOW)
    assert before["forget"] == {"available": True, "reason": None, "archived": 0, "pending": 3,
                                "branch": "memd/forget"}
    assert before["summary"]["pending_forget"] == 3 and before["summary"]["archived"] == 0
    _approve(store, monkeypatch)
    after = insights.compute(store, "amber", clone=store.clone, db_path=store.db, now=NOW)
    assert after["coverage"]["archived"] == 3 and after["forget"]["pending"] == 0
    assert after["coverage"]["notes"] == before["coverage"]["notes"] - 3
    assert "old-stale" not in {i["slug"] for i in after["stale"]["items"]}
    text = insights.format_text(after)
    assert "3 archived" in text and "forget: 3 archived, 0 proposed on memd/forget" in text


def test_nightly_forget_step_is_opt_in():
    assert "forget" not in nightly.DEFAULT_STEPS
    assert nightly.parse_steps("health,forget") == ("forget", "health")
    assert nightly._main("forget") is forget.main
    assert "forget" in nightly.DRY_RUN and "forget" in nightly.PUSH


def test_cli_json_dry_run(store, capsys):
    assert forget.main(["--dry-run", "--json", "--days", "200"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] and out["settings"]["days"] == 200 and "_texts" not in out
    assert {c["slug"] for c in out["candidates"]} <= CANDIDATES      # the wall clock decides the rest


def test_console_script_declared():
    import pathlib
    text = (pathlib.Path(__file__).parent.parent / "pyproject.toml").read_text()
    assert 'mem-forget = "memd.forget:main"' in text
