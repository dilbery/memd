import datetime as dt

import pytest

import memd.reflect as reflect


def _note(slug, title, body, last_used):
    return {"slug": slug, "title": title, "body": body, "last_used": last_used}


def test_build_report_finds_dedup_and_stale(monkeypatch):
    now = dt.datetime(2026, 6, 16, tzinfo=dt.timezone.utc)
    notes = [
        _note("a", "GPU thrash fix", "model eviction thrash fix steps", "2026-06-15T00:00:00Z"),
        _note("b", "GPU thrash fix copy", "model eviction thrash fix steps", "2026-06-14T00:00:00Z"),
        _note("c", "Ancient note", "old fact nobody touched", "2025-01-01T00:00:00Z"),
    ]
    report = reflect.build_report(notes, now=now, stale_days=120)
    dup_slugs = {tuple(sorted(p)) for p in report["duplicates"]}
    assert ("a", "b") in dup_slugs
    assert "c" in report["stale"]
    # staleness NEVER yields a delete action
    assert "delete" not in str(report).lower()


def test_reflect_opens_branch_and_pr_and_pushes_summary(monkeypatch):
    calls = {"branch": None, "pr": None, "push": None, "merge": 0, "delete": 0}

    def fake_make_branch(report, draft):
        calls["branch"] = "reflect/2026-06-16"
        return calls["branch"]

    def fake_open_pr(branch, report):
        calls["pr"] = {"branch": branch, "n_dups": len(report["duplicates"])}
        return "https://git.example.com/svcuser/memory-store/pulls/42"

    def fake_pushover(summary):
        calls["push"] = summary

    def boom_merge(*a, **k):
        calls["merge"] += 1
        raise AssertionError("reflect must NEVER merge")

    def boom_delete(*a, **k):
        calls["delete"] += 1
        raise AssertionError("reflect must NEVER delete")

    monkeypatch.setattr(reflect, "_load_notes", lambda: [
        {"slug": "a", "title": "X", "body": "dup body here", "last_used": "2026-06-15T00:00:00Z"},
        {"slug": "b", "title": "Xc", "body": "dup body here", "last_used": "2026-06-14T00:00:00Z"},
    ])
    monkeypatch.setattr(reflect, "make_branch", fake_make_branch)
    monkeypatch.setattr(reflect, "open_pr", fake_open_pr)
    monkeypatch.setattr(reflect, "send_pushover", fake_pushover)
    monkeypatch.setattr(reflect, "_merge_guard", boom_merge, raising=False)
    monkeypatch.setattr(reflect, "_delete_guard", boom_delete, raising=False)

    url = reflect.reflect()
    assert url.endswith("/pulls/42")
    assert calls["branch"] == "reflect/2026-06-16"
    assert calls["pr"]["branch"] == "reflect/2026-06-16"
    assert "PR" in calls["push"] or "pull" in calls["push"].lower()
    assert calls["merge"] == 0
    assert calls["delete"] == 0


def test_reflect_source_has_no_merge_or_force_delete():
    src = (reflect.__file__)
    text = open(src, encoding="utf-8").read()
    # Belt-and-braces: the implementation must not call merge/force-delete.
    assert "merge_pr" not in text
    assert "git push --force" not in text
    assert "rm -rf" not in text


# ---------------------------------------------------------------------------
# (2c) LOW: _load_notes must recurse into subdirectories like recall/save
# (which use rglob / list_notes). A non-recursive glob('*.md') silently omits
# notes filed under a subdir, so the propose-only tidy never sees them.
# Driven over the REAL-corpus shape: underscore filenames, legacy
# name:/description: frontmatter, NO slug: field.
# ---------------------------------------------------------------------------

_SUBDIR_NOTE = """\
---
name: project-archived-fact
description: a fact filed under a subdirectory
type: project
metadata:
  node_type: memory
---
# Archived Fact

This archived fact lives in an archive/ subdirectory of the clone.
"""

_TOP_NOTE = """\
---
name: project-top-fact
description: a top-level fact
type: project
metadata:
  node_type: memory
---
# Top Fact

A plain top-level fact in the clone root.
"""


def _git(repo, *args):
    import subprocess
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_load_notes_recurses_into_subdirs(tmp_path, monkeypatch):
    # MEMD_CLONE is always a real git working tree (recall/save load it via
    # store.list_notes, which is git-aware); seed it the same way.
    clone = tmp_path / "clone"
    (clone / "archive").mkdir(parents=True)
    _git(clone, "init", "-q")
    _git(clone, "config", "user.email", "test@memd")
    _git(clone, "config", "user.name", "memd-test")
    (clone / "project_top_fact.md").write_text(_TOP_NOTE)
    (clone / "archive" / "project_archived_fact.md").write_text(_SUBDIR_NOTE)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "seed notes incl subdir")
    monkeypatch.setenv("MEMD_CLONE", str(clone))

    notes = reflect._load_notes()
    slugs = {n["slug"] for n in notes}
    # slug is slugify(title) from the H1, NOT the underscore filename stem.
    assert "top-fact" in slugs
    # BEFORE the fix the non-recursive glob('*.md') omits the subdir note;
    # AFTER the fix (rglob) it is included.
    assert "archived-fact" in slugs, f"subdir note dropped: {slugs}"
