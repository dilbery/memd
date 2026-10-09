"""save and dry_run share one identity decision (memd.save._identity).

Regressions from review: the in-place supersede checked expected_revision
against the note being retired, and dry_run mispredicted some saves.
"""
import subprocess

import pytest

import memd.save as save_mod
from memd.config import Config
from memd.store import Note, dump_note, read_note


def git(clone, *args):
    return subprocess.run(["git", "-C", str(clone), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    git(clone, "init", "-q")
    git(clone, "config", "user.email", "test@memd")
    git(clone, "config", "user.name", "test")
    git(clone, "commit", "--allow-empty", "-qm", "seed")
    monkeypatch.setattr(save_mod, "_pull_rebase_push", lambda clone: None)
    monkeypatch.setattr(save_mod, "_vector_near_matches", lambda cfg, body: [])
    return Config(clone=clone, db=tmp_path / "index.db", profile="amber", conflict_check="off")


def seed(cfg, filename, **fields):
    (cfg.clone / filename).write_text(dump_note(Note(path=filename, **fields)))
    git(cfg.clone, "add", "-A")
    git(cfg.clone, "commit", "-qm", "fixture")
    return read_note(cfg.clone, fields["slug"])


def test_in_place_supersede_guards_the_note_it_overwrites(cfg):
    x = seed(cfg, "x.md", title="X", slug="x", body="Proxy runs on vmhost.")
    y = seed(cfg, "y.md", title="Y", slug="y", body="Proxy runs on lapbox.")
    # Someone edits y after the caller read it.
    seed(cfg, "y.md", title="Y", slug="y", body="Proxy runs on gpuhost now.")
    with pytest.raises(save_mod.RevisionConflict):
        save_mod.save({"slug": "y", "title": "Y", "body": "Proxy runs on lapbox.", "supersedes": "x",
                       "expected_revision": x.git_blob}, "amber", cfg=cfg)
    with pytest.raises(save_mod.RevisionConflict):
        save_mod.save({"slug": "y", "title": "Y", "body": "Proxy runs on lapbox.", "supersedes": "x",
                       "expected_revision": y.git_blob}, "amber", cfg=cfg)
    current = read_note(cfg.clone, "y")
    out = save_mod.save({"slug": "y", "title": "Y", "body": "Proxy runs on lapbox.", "supersedes": "x",
                         "expected_revision": current.git_blob}, "amber", cfg=cfg)
    assert out.saved and read_note(cfg.clone, "x").superseded_by == "y"
    assert read_note(cfg.clone, "y").body == "Proxy runs on lapbox."


def test_dry_run_reports_what_save_refuses(cfg):
    seed(cfg, "x.md", title="X", slug="x", body="old")
    seed(cfg, "z.md", title="Z", slug="z", body="retired", superseded_by="x")
    fact = {"slug": "z", "title": "Z", "body": "new", "supersedes": "x"}
    report = save_mod.dry_run(fact, "amber", cfg=cfg)
    assert report["error"] == "replacement slug already belongs to another note"
    with pytest.raises(ValueError, match="replacement slug"):
        save_mod.save(fact, "amber", cfg=cfg)


def test_dry_run_never_reports_another_live_notes_slug(cfg):
    seed(cfg, "x.md", title="X", slug="x", body="old")
    seed(cfg, "y.md", title="Y", slug="y", body="a different live note")
    fact = {"title": "Y", "body": "replacement for x", "supersedes": "x"}
    report = save_mod.dry_run(fact, "amber", cfg=cfg)
    assert report["error"] is None and report["action"] == "supersede"
    assert report["slug"] is None and any("new unique slug" in w for w in report["warnings"])
    out = save_mod.save(fact, "amber", cfg=cfg)
    assert out.slug not in ("y", "x") and read_note(cfg.clone, "y").body == "a different live note"


def test_dry_run_matches_save_on_duplicate_stored_slugs(cfg):
    seed(cfg, "a.md", title="A", slug="dup", body="one")
    seed(cfg, "b.md", title="B", slug="dup", body="two")
    report = save_mod.dry_run({"title": "New", "body": "x"}, "amber", cfg=cfg)
    assert "duplicate stored slug" in report["error"]
