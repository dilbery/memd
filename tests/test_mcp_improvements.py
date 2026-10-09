"""Transport, profile and paging regressions using only temporary Git stores."""
import asyncio
import importlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import memd.mcp as mcp
import memd.server as srv
from memd.config import Config
from memd.read import read, ReadRevisionConflict
from memd.store import read_note
from tests.test_profile_isolation_integration import _seed_clone, _git


@pytest.fixture
def profile_stores(tmp_path, monkeypatch):
    for profile in ("amber", "cobalt"):
        clone = _seed_clone(tmp_path, profile, f"TEMP-{profile.upper()}")
        monkeypatch.setenv(f"MEMD_{profile.upper()}_CLONE", str(clone))
        monkeypatch.setenv(f"MEMD_{profile.upper()}_DB", str(tmp_path / profile / "index.db"))
    from memd.refresh import ensure_lexical
    for profile in ("amber", "cobalt"):
        env = dict(__import__("os").environ)
        env["MEMD_PROFILE"] = profile
        ensure_lexical(Config.from_env(env, env_file=None))
    monkeypatch.setenv("MEMD_PROFILE", "cobalt")
    monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "1")
    monkeypatch.setenv("MEMD_TOKEN", "test-profile-token")
    monkeypatch.setenv("MEMD_TOKENS_FILE", str(tmp_path / "absent-tokens"))
    monkeypatch.setenv("MEMD_REQUIRE_RECALL_TOKEN", "1")
    recall_module = importlib.import_module("memd.recall")
    monkeypatch.setattr(recall_module, "embed_with_deadline", lambda *a, **k: None)
    monkeypatch.setattr(recall_module, "rerank", lambda *a, **k: None)
    return tmp_path


def _call(client, name, arguments):
    return client.post("/mcp/", headers={
        "Authorization": "Bearer test-profile-token",
        "Accept": "application/json, text/event-stream",
    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}}).json()["result"]


def test_mcp_defaults_to_locked_profile_and_denies_override_with_real_stores(profile_stores):
    with TestClient(srv.create_token_app()) as client:
        recalled = _call(client, "recall", {})
        assert recalled["isError"] is False
        assert "TEMP-COBALT" in recalled["content"][0]["text"]
        assert "TEMP-AMBER" not in recalled["content"][0]["text"]
        note = _call(client, "read", {"slug": "cobalt-secret"})
        assert note["structuredContent"]["profile"] == "cobalt"
        assert "TEMP-COBALT" in note["structuredContent"]["body"]
        for tool, args in (("recall", {}), ("read", {"slug": "amber-secret"}),
                           ("save", {"body": "must never cross profiles"})):
            result = _call(client, tool, {**args, "profile": "amber"})
            assert result["isError"] is True
            assert "instance locked" in result["content"][0]["text"]
        for endpoint in ("recall", "read", "save"):
            response = client.post("/" + endpoint, json={"profile": "amber", "body": "test"},
                                   headers={"Authorization": "Bearer test-profile-token"})
            assert response.status_code == 403


def test_save_omitted_profile_uses_instance(profile_stores, monkeypatch):
    seen = {}
    def core(fact, profile, cfg):
        seen.update(profile=profile, clone=cfg.clone)
        return SimpleNamespace(to_dict=lambda: {"saved": True, "synced": False, "indexed": False,
                                                "slug": "new", "revision": "abc", "warnings": ["sync deferred"]})
    monkeypatch.setattr(mcp, "_core_save", core)
    out = asyncio.run(mcp.call_tool("save", {"body": "new note"}))
    assert not out.is_error
    assert seen == {"profile": "cobalt", "clone": profile_stores / "cobalt-clone"}
    assert out.structured_content["saved"] and not out.structured_content["synced"]


def test_empty_save_is_a_real_mcp_error_and_rest_bad_request(profile_stores):
    with TestClient(srv.create_token_app()) as client:
        result = _call(client, "save", {})
        assert result["isError"] is True
        assert result["structuredContent"]["ok"] is False
        assert "body/content/text" in result["content"][0]["text"]
        response = client.post("/save", json={}, headers={"Authorization": "Bearer test-profile-token"})
        assert response.status_code == 400


def test_read_full_body_pages_revision_and_provenance(config, git_clone):
    body = "intro " * 600 + "tail needle-987 " + "ending Ω " * 600
    path = git_clone / "deep-note.md"
    path.write_text("---\ntitle: Deep note\nslug: deep-note\nprofile: amber\nsource: user test\n"
                    "observed_at: 2026-09-09\nverified_at: 2026-09-09\npinned: true\n---\n" + body)
    _git(git_clone, "add", "-A")
    _git(git_clone, "commit", "-qm", "deep note")
    expected = read_note(git_clone, "deep-note")
    page = read("deep-note", profile="amber", cfg=config, limit=333)
    assert page["source"] == "user test"
    assert page["observed_at"] == "2026-09-09"
    assert page["revision"] == expected.git_blob
    assert page["path"] == "deep-note.md"
    collected = page["body"]
    while page["continuation"]:
        page = read(cfg=config, **page["continuation"])
        collected += page["body"]
    assert collected == expected.body
    assert page["complete"] and page["next_offset"] is None
    assert "tail needle-987" in collected
    path.write_text(path.read_text() + " changed")
    with pytest.raises(ReadRevisionConflict):
        read("deep-note", profile="amber", cfg=config, revision=expected.git_blob)


def test_read_is_independent_of_sqlite_and_blocks_path_traversal(config, git_clone):
    config.db.write_bytes(b"not a SQLite database")
    result = read("repo-hosting-policy", profile="amber", cfg=config)
    assert "Forgejo" in result["body"]
    with pytest.raises(FileNotFoundError):
        read("../../etc/passwd", profile="amber", cfg=config)


def test_mcp_slow_core_does_not_block_other_tasks(monkeypatch):
    started, release = threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        started.set()
        release.wait(2)
        return []
    monkeypatch.setattr(mcp, "_core_recall", slow)
    monkeypatch.setattr(mcp, "_cfg_for", lambda profile: None)
    async def run():
        task = asyncio.create_task(mcp.call_tool("recall", {}))
        try:
            await asyncio.to_thread(started.wait, 1)
            await asyncio.sleep(0.01)
            assert not task.done(), "synchronous core blocked the event loop until it finished"
        finally:
            release.set()
            await task
    asyncio.run(run())


def test_recall_render_controls_are_forwarded(monkeypatch):
    seen = {}
    def core(query, **kwargs):
        seen.update(kwargs)
        return [SimpleNamespace(to_dict=lambda i=i: {"slug": f"s{i}", "body": "body", "matched": True})
                for i in range(12)]
    monkeypatch.setattr(mcp, "_core_recall", core)
    result = asyncio.run(mcp.call_tool("recall", {"query": "body", "k": 12,
                        "include_core": False, "max_chars": 100000, "host": "hass", "tags": "a,b"}))
    assert not result.is_error
    assert seen["k"] == 12 and seen["include_core"] is False
    assert seen["host"] == "hass" and seen["tags"] == ["a", "b"]
    assert result.structured_content["returned_matches"] == 12
    assert result.structured_content["omitted_matches"] == 0


def test_rest_can_return_only_canonical_context(monkeypatch):
    # Tests the response shape, not the auth policy.
    monkeypatch.setenv("MEMD_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.setattr(srv, "_core_recall", lambda *a, **k: [SimpleNamespace(to_dict=lambda: {
        "slug": "short", "matched": True, "body": "fact",
    })])
    response = TestClient(srv.create_token_app()).post("/recall", json={"query": "fact", "format": "context"})
    assert response.status_code == 200
    data = response.json()
    assert "notes" not in data and "### short" in data["context"]
    assert data["rendering"]["returned_matches"] == 1


def test_explicit_env_file_profile_lock_is_shared_by_config_and_transports(tmp_path, monkeypatch):
    env_file = tmp_path / "client.env"
    env_file.write_text("MEMD_PROFILE=cobalt\nMEMD_ENFORCE_PROFILE=1\n")
    monkeypatch.setenv("MEMD_PROFILE", "amber")
    monkeypatch.setenv("MEMD_ENV_FILE", str(env_file))
    from memd.profiles import resolve_profile, ProfileMismatch
    assert resolve_profile() == "cobalt"
    assert mcp._cfg_for(resolve_profile()).profile == "cobalt"
    assert srv._cfg(srv._resolve_profile(None)).profile == "cobalt"
    with pytest.raises(ProfileMismatch):
        resolve_profile("amber")


def test_explicit_env_file_does_not_override_authorized_unlocked_request(tmp_path, monkeypatch):
    env_file = tmp_path / "client.env"
    env_file.write_text("MEMD_PROFILE=cobalt\n")
    monkeypatch.setenv("MEMD_ENV_FILE", str(env_file))
    from memd.profiles import resolve_profile
    assert resolve_profile("amber") == "amber"
    assert mcp._cfg_for("amber").profile == "amber"
    assert srv._cfg("amber").profile == "amber"


@pytest.mark.parametrize("marker", ["rebase-merge", "rebase-apply"])
def test_read_refuses_unfinished_rebase_worktree(config, git_clone, marker):
    from memd.read import ReadUnavailable
    (git_clone / ".git" / marker).mkdir()
    with pytest.raises(ReadUnavailable, match="unfinished rebase"):
        read("repo-hosting-policy", profile="amber", cfg=config)


def test_read_refuses_unmerged_index_without_rebase_markers(config, git_clone):
    import subprocess
    from memd.read import ReadUnavailable
    filename = 'repo-hosting-policy.md'
    blob = _git(git_clone, 'rev-parse', f'HEAD:{filename}')
    entries = f"0 {'0' * 40}\t{filename}\n" + ''.join(
        f"100644 {blob} {stage}\t{filename}\n" for stage in (1, 2, 3)
    )
    subprocess.run(['git', '-C', str(git_clone), 'update-index', '--index-info'],
                   input=entries, text=True, check=True, capture_output=True)
    assert not (git_clone / '.git' / 'rebase-merge').exists()
    with pytest.raises(ReadUnavailable, match='unresolved merge or autostash conflicts'):
        read('repo-hosting-policy', profile='amber', cfg=config)


def test_health_reports_unfinished_rebase_as_unavailable(config, git_clone, monkeypatch):
    (git_clone / '.git' / 'rebase-merge').mkdir()
    monkeypatch.setattr(srv, '_cfg', lambda: config)
    monkeypatch.setattr(srv, 'embed_with_deadline', lambda *a, **k: None)
    monkeypatch.setattr(srv, 'rerank', lambda *a, **k: None)
    health = srv._health(force=True)
    assert health['checks']['git']['ok'] is False
    assert health['checks']['git']['in_sync'] is False
    assert 'unfinished rebase' in health['checks']['git']['detail']
    assert health['status'] != 'ok'


def test_recall_structured_content_carries_the_rendered_notes(monkeypatch):
    # Claude Code shows the model structuredContent whenever a tool declares an
    # outputSchema. Keeping the rendered text only in content[] meant every MCP
    # recall reached the model as excerpt coordinates with no note bodies.
    monkeypatch.setattr(mcp, "_core_recall", lambda query, **kw: [SimpleNamespace(
        to_dict=lambda: {"slug": "proxy", "title": "Proxy", "body": "The proxy serves the public tier.",
                         "matched": True})])
    result = asyncio.run(mcp.call_tool("recall", {"query": "proxy", "include_core": False}))
    assert "The proxy serves the public tier." in result.structured_content["text"]
    assert result.content[0].text == result.structured_content["text"]
    assert "text" in mcp.RECALL_OUTPUT["properties"]


def test_recall_structured_text_says_so_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(mcp, "_core_recall", lambda query, **kw: [])
    result = asyncio.run(mcp.call_tool("recall", {"query": "nothing", "include_core": False}))
    assert result.structured_content["text"] == "No matching memory notes."
