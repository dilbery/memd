"""The authenticated caller is recorded on the note and the commit."""
import asyncio
import subprocess

import pytest

from memd import actor
from memd.config import Config
from memd.normalize import normalize_fact
from memd.save import save
from memd.store import Note, dump_note, parse_text, read_note


def test_actor_default_is_empty():
    actor.set_actor("")
    assert actor.get_actor() == ""


def test_actor_survives_to_thread():
    """save() runs under asyncio.to_thread on the MCP path; contextvars are copied."""
    actor.set_actor("gateway-client")
    try:
        assert asyncio.run(asyncio.to_thread(actor.get_actor)) == "gateway-client"
    finally:
        actor.set_actor("")


def test_note_roundtrips_saved_by():
    text = dump_note(Note(title="t", slug="t", path="t.md", body="b", saved_by="litellm"))
    assert "saved_by: litellm" in text
    assert parse_text(text, path="t.md").saved_by == "litellm"


def test_note_without_actor_writes_no_key():
    assert "saved_by" not in dump_note(Note(title="t", slug="t", path="t.md", body="b"))


def test_normalize_drops_caller_supplied_saved_by():
    """A payload field is never identity."""
    assert "saved_by" not in normalize_fact({"title": "x", "body": "y", "saved_by": "forged"})


@pytest.fixture
def clone(tmp_path, monkeypatch):
    c = tmp_path / "clone"
    c.mkdir()
    subprocess.run(["git", "-C", str(c), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(c), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(c), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(c), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    monkeypatch.setenv("MEMD_AMBER_CLONE", str(c))
    monkeypatch.setenv("MEMD_AMBER_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("MEMD_LOCAL_HOST", "any")
    return c


def _last_message(clone):
    return subprocess.run(["git", "-C", str(clone), "log", "-1", "--format=%B"],
                          capture_output=True, text=True, check=True).stdout


def test_save_stamps_actor_in_frontmatter_and_commit(clone, tmp_path):
    cfg = Config(clone=clone, db=tmp_path / "m.db")
    actor.set_actor("claude-code-lapbox")
    try:
        r = save({"title": "Stamped", "body": "who saved this"}, profile="amber", cfg=cfg)
    finally:
        actor.set_actor("")
    assert r.saved
    assert read_note(clone, r.slug).saved_by == "claude-code-lapbox"
    assert "Saved-By: claude-code-lapbox" in _last_message(clone)


def test_save_without_actor_has_no_trailer(clone, tmp_path):
    cfg = Config(clone=clone, db=tmp_path / "m.db")
    actor.set_actor("")
    r = save({"title": "Unstamped", "body": "anonymous"}, profile="amber", cfg=cfg)
    assert read_note(clone, r.slug).saved_by == ""
    assert "Saved-By" not in _last_message(clone)


def test_update_restamps_with_the_current_caller(clone, tmp_path):
    cfg = Config(clone=clone, db=tmp_path / "m.db")
    actor.set_actor("first-caller")
    r = save({"title": "Shared", "body": "v1"}, profile="amber", cfg=cfg)
    actor.set_actor("second-caller")
    try:
        save({"slug": r.slug, "title": "Shared", "body": "v2"}, profile="amber", cfg=cfg)
    finally:
        actor.set_actor("")
    note = read_note(clone, r.slug)
    assert note.body == "v2" and note.saved_by == "second-caller"


def test_update_keeps_the_previous_stamp_when_no_caller_is_set(clone, tmp_path):
    cfg = Config(clone=clone, db=tmp_path / "m.db")
    actor.set_actor("original")
    r = save({"title": "Kept", "body": "v1"}, profile="amber", cfg=cfg)
    actor.set_actor("")
    save({"slug": r.slug, "title": "Kept", "body": "v2"}, profile="amber", cfg=cfg)
    assert read_note(clone, r.slug).saved_by == "original"
