import os
import pytest

from memd.integrations.degraded_grep import grep_recall, MemdCloneForbidden


def _write_note(d, name, title, body):
    (d / name).write_text(
        f"---\ntitle: {title}\nslug: {name[:-3]}\n---\n{body}\n", encoding="utf-8"
    )


def test_grep_recall_returns_matching_notes(tmp_path):
    _write_note(tmp_path, "a.md", "GPU thrash", "model server eviction thrash fix")
    _write_note(tmp_path, "b.md", "Network", "switch vlan topology and routing")
    hits = grep_recall("thrash", str(tmp_path), top_n=4)
    assert len(hits) == 1
    assert hits[0]["slug"] == "a"
    assert "thrash" in hits[0]["body"].lower()


def test_grep_recall_caps_top_n(tmp_path):
    for i in range(10):
        _write_note(tmp_path, f"n{i}.md", f"Note {i}", "shared keyword present here")
    hits = grep_recall("keyword", str(tmp_path), top_n=3)
    assert len(hits) == 3


def test_grep_recall_refuses_memd_clone(tmp_path, monkeypatch):
    memd_clone = tmp_path / "memd-clone"
    memd_clone.mkdir()
    _write_note(memd_clone, "x.md", "X", "secret in flight half-written tree")
    monkeypatch.setenv("MEMD_CLONE", str(memd_clone))
    with pytest.raises(MemdCloneForbidden):
        grep_recall("secret", str(memd_clone), top_n=4)


def test_grep_recall_missing_checkout_returns_empty(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert grep_recall("anything", str(missing), top_n=4) == []
