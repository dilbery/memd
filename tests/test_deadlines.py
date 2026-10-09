"""Recall's model deadlines come from config, not from literals in the hot path."""
import subprocess

import pytest

from memd.config import Config


def test_defaults_match_previous_hard_coded_values():
    cfg = Config()
    assert (cfg.embed_deadline_ms, cfg.rerank_deadline_ms) == (800, 900)


def test_env_sets_deadlines():
    cfg = Config.from_env({"MEMD_EMBED_DEADLINE_MS": "1500",
                           "MEMD_RERANK_DEADLINE_MS": "2500"}, env_file=None)
    assert (cfg.embed_deadline_ms, cfg.rerank_deadline_ms) == (1500, 2500)


def test_garbage_and_out_of_range_are_clamped_or_defaulted():
    cfg = Config.from_env({"MEMD_EMBED_DEADLINE_MS": "abc",
                           "MEMD_RERANK_DEADLINE_MS": "999999"}, env_file=None)
    assert (cfg.embed_deadline_ms, cfg.rerank_deadline_ms) == (800, 10000)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    subprocess.run(["git", "-C", str(clone), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@t"], check=True)
    (clone / "a.md").write_text(
        "---\ntitle: alpha\nslug: alpha\nprofile: amber\nhost: any\n---\nalpha body text\n")
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-q", "-m", "i"], check=True)
    monkeypatch.setenv("MEMD_AMBER_CLONE", str(clone))
    monkeypatch.setenv("MEMD_AMBER_DB", str(tmp_path / "m.db"))
    return clone, tmp_path / "m.db"


def test_recall_passes_configured_deadlines(monkeypatch, seeded):
    from memd import recall as recall_mod
    from memd.refresh import ensure_lexical
    clone, db = seeded
    cfg = Config(clone=clone, db=db, embed_deadline_ms=1234, rerank_deadline_ms=4321)
    ensure_lexical(cfg)
    seen = {}

    # Both fakes return None: a truthy non-list from embed would be handed to
    # _vector_arm and serialised. None takes the documented "embed unavailable"
    # path, and the BM25 arm still yields candidates so rerank is still called.
    def fake_embed(q, *, cfg, ms):
        seen["embed"] = ms
        return None

    def fake_rerank(q, c, top_n, ms, *, cfg):
        seen["rerank"] = ms
        return None

    monkeypatch.setattr(recall_mod, "embed_with_deadline", fake_embed)
    monkeypatch.setattr(recall_mod, "rerank", fake_rerank)
    recall_mod.recall("alpha", profile="amber", k=3, cfg=cfg)
    assert seen == {"embed": 1234, "rerank": 4321}


def test_recall_defaults_are_the_documented_800_and_900(monkeypatch, seeded):
    from memd import recall as recall_mod
    from memd.refresh import ensure_lexical
    clone, db = seeded
    cfg = Config(clone=clone, db=db)
    ensure_lexical(cfg)
    seen = {}
    monkeypatch.setattr(recall_mod, "embed_with_deadline",
                        lambda q, *, cfg, ms: seen.__setitem__("embed", ms))
    monkeypatch.setattr(recall_mod, "rerank",
                        lambda q, c, top_n, ms, *, cfg: seen.__setitem__("rerank", ms))
    recall_mod.recall("alpha", profile="amber", k=3, cfg=cfg)
    assert seen == {"embed": 800, "rerank": 900}
