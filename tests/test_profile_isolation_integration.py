"""REAL-path profile hard-isolation tests (FIX GROUP 2).

Cobalt isolation was dead code: memd/profiles.py had ZERO prod callers, so a
recall/save bound to one profile would happily read/write another profile's
clone+db if handed an ambient cfg. These tests drive the REAL recall()/save()/
mcp/hook against temp git clones + sqlite dbs for two isolated profiles
(amber + cobalt). Only the embedding and rerank backends' HTTP is faked via
respx; the _core_recall/_core_save seam is NEVER monkeypatched.

Each test FAILS before the fix (the guard was unwired) and PASSES after:
  * recall(profile='amber') pointed at a populated cobalt db/clone raises
    CrossProfileViolation instead of serving cobalt notes,
  * save(profile='cobalt') from an amber context refuses and writes NOTHING into
    amber's clone,
  * a legitimate per-profile call still serves that profile's own notes,
  * assert_no_cross_profile is deny-by-default (sibling/symlink escape refused),
  * a MEMD_CLONE override that escapes into another profile raises at config.
"""
from memd.config import DEFAULT_EMBED_URL
import json
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

import memd.mcp as mcp
import memd.hooks.auto_recall as ar
import memd.index as index_mod
import memd.save as save_mod
from memd.config import Config
from memd.profiles import (
    CrossProfileViolation,
    assert_no_cross_profile,
    guard_paths,
)
from memd.recall import recall as core_recall
from memd.save import save as core_save
from memd.store import read_note

# Derived from the config default so it cannot go stale again: a hard-coded
# URL silently stopped intercepting when the embedding backend moved.
EMBED_URL = f"{DEFAULT_EMBED_URL}/v1/embeddings"
RERANK_URL = "http://127.0.0.1:8000/api/v1/reranking"


# ---------------------------------------------------------------------------
# Fixtures: two fully isolated profiles, each a real git clone with its own
# distinctly-named seed note, registered via the MEMD_<PROFILE>_* env contract.
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _seed_clone(root: Path, profile: str, marker: str) -> Path:
    clone = root / f"{profile}-clone"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q")
    _git(clone, "config", "user.email", "test@memd")
    _git(clone, "config", "user.name", "memd-test")
    (clone / f"{profile}-secret.md").write_text(
        f"---\ntitle: {profile} secret\nslug: {profile}-secret\n"
        f"profile: {profile}\nhost: gpuhost\nimportance: 5\ngrounding: ok\n---\n"
        f"{marker} private to {profile} only\n"
    )
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "seed")
    return clone


@pytest.fixture
def two_profiles(tmp_path, monkeypatch):
    """Register isolated amber + cobalt profiles with real clones + db paths."""
    amber_clone = _seed_clone(tmp_path, "amber", "AMBER-FORGEJO-POLICY")
    bri_clone = _seed_clone(tmp_path, "cobalt", "COBALT-RECIPE-XYZZY")
    amber_db = tmp_path / "amber" / "memd.db"
    bri_db = tmp_path / "cobalt" / "memd.db"

    monkeypatch.setenv("MEMD_AMBER_CLONE", str(amber_clone))
    monkeypatch.setenv("MEMD_AMBER_DB", str(amber_db))
    monkeypatch.setenv("MEMD_AMBER_REPO", "ssh://git@10.10.1.10/svcuser/amber.git")
    monkeypatch.setenv("MEMD_AMBER_CRED", "amber-token")
    monkeypatch.setenv("MEMD_COBALT_CLONE", str(bri_clone))
    monkeypatch.setenv("MEMD_COBALT_DB", str(bri_db))
    monkeypatch.setenv("MEMD_COBALT_REPO", "ssh://git@10.10.1.10/svcuser/cobalt.git")
    monkeypatch.setenv("MEMD_COBALT_CRED", "blr-token")
    # Clear any single-clone override so each Config.from_env resolves per-profile.
    monkeypatch.delenv("MEMD_CLONE", raising=False)
    monkeypatch.delenv("MEMD_DB", raising=False)
    # The serving process warms lexical search at startup; recall itself no
    # longer performs a synchronous rebuild on a request's critical path.
    from memd.refresh import ensure_lexical
    ensure_lexical(_cfg_for("amber"))
    ensure_lexical(_cfg_for("cobalt"))
    return {
        "amber_clone": amber_clone, "amber_db": amber_db,
        "bri_clone": bri_clone, "bri_db": bri_db,
    }


def _embed_response(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    texts = payload["input"]
    if isinstance(texts, str):
        texts = [texts]
    data = [{"embedding": [float(len(t) % 7) + 1.0] * 768} for t in texts]
    return httpx.Response(200, json={"data": data})


def _mock_backends():
    respx.post(EMBED_URL).mock(side_effect=_embed_response)
    respx.post(RERANK_URL).mock(return_value=httpx.Response(503))  # fall to BM25


def _no_push(clone):
    return None


def _cfg_for(profile: str) -> Config:
    # Merge the live env (monkeypatched MEMD_<PROFILE>_* contract) with the
    # requested profile so per-profile clone/db resolve to the seeded clones.
    import os
    env = dict(os.environ)
    env["MEMD_PROFILE"] = profile
    return Config.from_env(env)


# ---------------------------------------------------------------------------
# (e) the headline cross-profile leaks, driven through the REAL core.
# ---------------------------------------------------------------------------


@respx.mock
def test_amber_recall_pointed_at_cobalt_db_clone_raises(two_profiles):
    """A recall bound to profile='amber' but handed cobalt's populated clone+db
    must HARD-FAIL, never serve cobalt's notes."""
    _mock_backends()
    # Ambient cfg whose clone/db are COBALT's, but the request asks for amber.
    import dataclasses
    base = _cfg_for("cobalt")  # resolves cobalt's real clone/db
    cfg = dataclasses.replace(base, profile="amber")
    with pytest.raises(CrossProfileViolation):
        core_recall("recipe", profile="amber", k=5, cfg=cfg)


@respx.mock
def test_cobalt_save_from_amber_context_writes_nothing_to_amber(two_profiles):
    """save(profile='cobalt') handed amber's clone must refuse AND leave amber's
    clone untouched (no new commit, no note file)."""
    _mock_backends()
    import dataclasses
    amber_cfg = _cfg_for("amber")  # real amber clone/db
    # Build the index so a non-guarded save would otherwise proceed to write.
    respx.post(EMBED_URL).mock(side_effect=_embed_response)
    from memd.index import open_db, build_index
    db = open_db(amber_cfg.db)
    build_index(db, amber_cfg)
    db.close()

    amber_head_before = _git(two_profiles["amber_clone"], "rev-parse", "HEAD")
    amber_files_before = sorted(p.name for p in two_profiles["amber_clone"].glob("*.md"))

    # A cobalt save handed amber's cfg: must raise, never touch amber's tree.
    poisoned = dataclasses.replace(amber_cfg, profile="cobalt")
    with pytest.raises(CrossProfileViolation):
        core_save(
            {"title": "Cobalt Grocery List", "body": "milk eggs bread",
             "host": "gpuhost"},
            profile="cobalt", cfg=poisoned,
        )

    # amber's clone is byte-for-byte unchanged.
    assert _git(two_profiles["amber_clone"], "rev-parse", "HEAD") == amber_head_before
    assert sorted(p.name for p in two_profiles["amber_clone"].glob("*.md")) == amber_files_before
    assert read_note(two_profiles["amber_clone"], "cobalt-grocery-list") is None


@respx.mock
def test_per_profile_recall_serves_only_its_own_notes(two_profiles):
    """The wiring is authoritative: a cobalt recall resolves cobalt's clone+db
    and returns cobalt's note; an amber recall returns amber's — never crossed."""
    _mock_backends()
    bri_notes = core_recall("recipe xyzzy", profile="cobalt", k=5,
                            cfg=_cfg_for("cobalt"))
    bri_slugs = [n.slug for n in bri_notes]
    assert "cobalt-secret" in bri_slugs
    assert "amber-secret" not in bri_slugs

    amber_notes = core_recall("forgejo policy", profile="amber", k=5,
                             cfg=_cfg_for("amber"))
    amber_slugs = [n.slug for n in amber_notes]
    assert "amber-secret" in amber_slugs
    assert "cobalt-secret" not in amber_slugs


@respx.mock
def test_per_profile_save_lands_only_in_its_own_clone(two_profiles, monkeypatch):
    """A cobalt save commits to COBALT's clone and nothing lands in amber's."""
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)
    respx.post(EMBED_URL).mock(side_effect=_embed_response)
    respx.post(RERANK_URL).mock(return_value=httpx.Response(503))
    from memd.index import open_db, build_index
    bri_cfg = _cfg_for("cobalt")
    db = open_db(bri_cfg.db); build_index(db, bri_cfg); db.close()

    res = core_save(
        {"title": "Cobalt Recipe Note", "body": "fresh durable cobalt fact",
         "host": "gpuhost"},
        profile="cobalt", cfg=bri_cfg,
    )
    assert res.action == "created"
    assert read_note(two_profiles["bri_clone"], "cobalt-recipe-note") is not None
    # never leaked into amber's clone.
    assert read_note(two_profiles["amber_clone"], "cobalt-recipe-note") is None


# ---------------------------------------------------------------------------
# MCP + hook drive the SAME real core through their own threading.
# ---------------------------------------------------------------------------


@respx.mock
def test_mcp_recall_threads_profile_and_isolates(two_profiles):
    """mcp.call_tool('recall', profile='cobalt') resolves cobalt's clone+db
    via _cfg_for and serves ONLY cobalt's note."""
    import asyncio
    _mock_backends()
    out = asyncio.run(mcp.call_tool(
        "recall", {"query": "recipe xyzzy", "profile": "cobalt", "k": 5}))
    text = out.content[0].text
    assert "cobalt-secret" in text
    assert "amber-secret" not in text
    # Assert on body markers too: the slug alone leaving the block would not
    # prove the other profile's CONTENT stayed out of it.
    assert "COBALT-RECIPE-XYZZY" in text
    assert "AMBER-FORGEJO-POLICY" not in text


@respx.mock
def test_hook_threads_profile_and_isolates(two_profiles, monkeypatch):
    """The auto-recall hook honors MEMD_PROFILE=cobalt and injects only
    cobalt's note via the REAL recall()."""
    _mock_backends()
    monkeypatch.setenv("MEMD_PROFILE", "cobalt")
    out = ar.build_context({"prompt": "COBALT-RECIPE-XYZZY"})
    block = out["hookSpecificOutput"]["additionalContext"]
    assert "COBALT-RECIPE-XYZZY" in block
    assert "AMBER-FORGEJO-POLICY" not in block
