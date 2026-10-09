"""Fix-group 1/2 (note-identity-filepath): an established corpus is underscore-named
with legacy `name:`/`description:` frontmatter and NO `slug:` field, so a note's
slug (slugify of its H1/name title) does NOT equal its filename stem.

The clean `<slug>.md` fixtures in conftest hide a data-loss class of bug: the
write path (_write_note / _set_superseded) synthesized `<slug>.md` instead of
rewriting the EXISTING note's real file (its .path). On the real corpus that
forks one note into TWO files (the original is never updated/superseded) and
leaves a duplicate-slug row in the index.

These tests seed a REPRESENTATIVE corpus-shaped note and drive the REAL
save()/recall() (only the embedding HTTP call is faked). Each FAILS before the fix and
PASSES after.
"""
import subprocess

import pytest

import memd.save as save_mod
import memd.index as index_mod
from memd.save import save
from memd.index import open_db, build_index
from memd.store import read_note, list_notes


# A legacy, corpus-shaped note: underscore filename, name:/description: legacy
# frontmatter, NO slug: field, an H1 whose slugify != the filename stem. This is
# the shape of every note in such a corpus.
LEGACY_FOO = """\
---
name: project-foo-bar
description: Foo bar legacy fact, no slug field, H1 title differs from filename
type: project
metadata:
  node_type: memory
  source: hermes
---
# Foo Bar Widget

The foo bar widget runs on host vmhost at 10.10.1.99 and uses the gizmo daemon
to twiddle the frobnicator. This is a stable long-standing fact.
"""

LEGACY_FOO_FILENAME = "project_foo_bar.md"
LEGACY_FOO_SLUG = "foo-bar-widget"   # slugify of the H1 — NOT the filename stem


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _no_push(clone):
    return None


def _fake_embed(texts, cfg):
    return [[1.0] * 768 for _ in texts]


@pytest.fixture
def legacy_clone(tmp_path):
    """A git clone seeded with ONE legacy underscore-named note (no slug:)."""
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@memd")
    _git(repo, "config", "user.name", "memd-test")
    (repo / LEGACY_FOO_FILENAME).write_text(LEGACY_FOO)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed legacy note")
    return repo


@pytest.fixture
def legacy_config(legacy_clone, tmp_path, monkeypatch):
    from memd.config import Config
    monkeypatch.setenv("MEMD_CLONE", str(legacy_clone))
    monkeypatch.setenv("MEMD_DB", str(tmp_path / "memd.db"))
    monkeypatch.setenv("MEMD_PROFILE", "amber")
    monkeypatch.setenv("MEMD_TOKEN", "test-token")
    return Config.from_env()


@pytest.fixture(autouse=True)
def _patch_io(monkeypatch):
    monkeypatch.setattr(index_mod, "embed", _fake_embed)
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)


def _md_files(clone):
    return sorted(p.name for p in clone.glob("*.md"))


def test_legacy_slug_differs_from_filename(legacy_clone):
    """Sanity: the representative fixture really is corpus-shaped (slug != stem)."""
    from pathlib import Path
    n = read_note(legacy_clone, LEGACY_FOO_SLUG)
    assert n is not None, "legacy note must be findable by its slugified-H1 slug"
    assert Path(n.path).name == LEGACY_FOO_FILENAME
    assert n.slug != LEGACY_FOO_FILENAME[:-3]  # slug != filename stem


def test_conflict_supersede_rewrites_original_file_not_new_slug_file(legacy_config):
    """A conflicting save must set superseded_by on the ORIGINAL underscore file,
    never fork a `<slug>.md`. The matched note keeps its real .path."""
    clone = legacy_config.clone
    db = open_db(legacy_config.db); build_index(db, legacy_config); db.close()

    res = save(
        {"title": "Foo Bar Widget",
         "body": "CONTRADICTS: the foo bar widget now runs on lapbox, gizmo gone",
         "host": "vmhost", "conflict": True},
        profile="amber", cfg=legacy_config,
    )
    assert res.action == "superseded"

    # The ORIGINAL file is still present and is now marked superseded_by the new slug.
    from pathlib import Path
    old = read_note(clone, LEGACY_FOO_SLUG)
    assert old is not None, "original note must still exist"
    assert Path(old.path).name == LEGACY_FOO_FILENAME, \
        "supersede must rewrite the ORIGINAL underscore file in place"
    assert old.superseded_by == res.slug

    # No orphan `<slug>.md` was synthesized for the OLD note.
    assert "foo-bar-widget.md" not in _md_files(clone)
    assert "foo_bar_widget.md" not in _md_files(clone)


def test_in_place_update_rewrites_original_file(legacy_config):
    """A strong-match update of an existing legacy note rewrites its REAL file,
    rather than forking a `<slug>.md` duplicate."""
    clone = legacy_config.clone
    db = open_db(legacy_config.db); build_index(db, legacy_config); db.close()

    res = save(
        {"title": "Foo Bar Widget",
         "body": "The foo bar widget runs on host vmhost at 10.10.1.99 and uses "
                 "the gizmo daemon to twiddle the frobnicator UPDATED NEW DETAIL",
         "host": "vmhost"},
        profile="amber", cfg=legacy_config,
    )
    assert res.action == "updated"
    assert res.slug == LEGACY_FOO_SLUG

    # The original underscore file carries the new body; no duplicate file forked.
    from pathlib import Path
    assert _md_files(clone) == [LEGACY_FOO_FILENAME]
    n = read_note(clone, LEGACY_FOO_SLUG)
    assert n is not None
    assert Path(n.path).name == LEGACY_FOO_FILENAME
    assert "UPDATED NEW DETAIL" in n.body


def test_one_index_row_per_slug_after_supersede(legacy_config):
    """After a supersede, the index has exactly ONE row per slug and the original
    note is pruned (it lives only in git as superseded)."""
    clone = legacy_config.clone
    db = open_db(legacy_config.db); build_index(db, legacy_config); db.close()

    res = save(
        {"title": "Foo Bar Widget",
         "body": "CONTRADICTS: the foo bar widget now runs on lapbox, gizmo gone",
         "host": "vmhost", "conflict": True},
        profile="amber", cfg=legacy_config,
    )

    # Live notes on disk: original (superseded) + the new note. Exactly one live
    # note per slug; no duplicate-slug collision.
    notes = list_notes(clone)
    slugs = [n.slug for n in notes]
    assert len(slugs) == len(set(slugs)), f"duplicate-slug files on disk: {slugs}"

    # Index: rebuild and assert exactly one row, the new (non-superseded) note.
    db = open_db(legacy_config.db)
    try:
        from memd.index import reindex
        reindex(db, legacy_config)
        rows = db.execute("SELECT slug, COUNT(*) FROM notes GROUP BY slug").fetchall()
        for slug, count in rows:
            assert count == 1, f"slug {slug} has {count} index rows"
        live = db.execute(
            "SELECT slug FROM notes WHERE superseded_by IS NULL"
        ).fetchall()
        live_slugs = {r[0] for r in live}
        assert res.slug in live_slugs
        assert LEGACY_FOO_SLUG not in live_slugs  # superseded original is pruned
    finally:
        db.close()


def test_single_filename_helper_no_fragmentation(legacy_config):
    """A brand-new note's file uses the SHARED underscore-convention helper, so
    save_lint, _write_note and _set_superseded agree on ONE filename per slug
    (no hyphen/underscore fork)."""
    clone = legacy_config.clone
    db = open_db(legacy_config.db); build_index(db, legacy_config); db.close()

    res = save(
        {"title": "Totally New Topic", "body": "an unrelated brand new fact",
         "host": "vmhost"},
        profile="amber", cfg=legacy_config,
    )
    assert res.action == "created"
    assert res.slug == "totally-new-topic"
    # underscore convention, matching the corpus + save_lint
    assert "totally_new_topic.md" in _md_files(clone)
    assert "totally-new-topic.md" not in _md_files(clone)
    # the on-disk path the result reports IS the file that exists
    from pathlib import Path
    assert Path(res.path).name == "totally_new_topic.md"
