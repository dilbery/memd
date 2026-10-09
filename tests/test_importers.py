"""mem-import (memd.importers): exports, Markdown folders and PRs into the review inbox.

Hermetic: small synthetic exports in tests/fixtures/import, temp Git/SQLite
stores, respx for the chat model and the GitHub API; the socket guard blocks
everything else.
"""
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

import memd.save as save_mod
from memd import actor, importers, inbox, recall as recall_mod
from memd.config import Config

FIXTURES = Path(__file__).parent / "fixtures" / "import"
LLM_URL = "http://llm.test/v1/chat/completions"
PULLS = "https://api.github.com/repos/example-org/widgets/pulls"


def head(clone):
    return subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def store(git_clone, tmp_path, monkeypatch):
    """The conftest clone with a lexical index; model backends unreachable."""
    from memd.refresh import ensure_lexical
    db = tmp_path / "memd.db"
    for key, value in {"MEMD_CLONE": str(git_clone), "MEMD_DB": str(db), "MEMD_PROFILE": "amber",
                       "MEMD_AMBER_CLONE": str(git_clone), "MEMD_AMBER_DB": str(db),
                       "MEMD_LOCAL_HOST": "any",
                       "MEMD_EMBED_URL": "http://127.0.0.1:9",
                       "MEMD_RERANK_URL": "http://127.0.0.1:9"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    monkeypatch.setattr(recall_mod, "embed_with_deadline", lambda *a, **k: None)
    monkeypatch.setattr(recall_mod, "rerank", lambda *a, **k: None)
    monkeypatch.setattr(save_mod, "_vector_near_matches", lambda cfg, body: [])
    monkeypatch.setattr(save_mod, "_pull_rebase_push", lambda clone: None)
    actor.set_actor("")
    cfg = Config.from_env(env_file=None)
    ensure_lexical(cfg)
    return cfg


def _zip(tmp_path, folder, name="export.zip"):
    """The fixture folder zipped under a top-level directory, as exports often are."""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for file in sorted((FIXTURES / folder).iterdir()):
            zf.write(file, f"export-2026/{file.name}")
        zf.writestr("__MACOSX/export-2026/._memories.json", "junk")
    return path


def _run(capsys, *argv):
    code = importers.main(list(argv))
    captured = capsys.readouterr()
    return code, captured


def _json_run(capsys, *argv):
    code = importers.main([*argv, "--json"])
    captured = capsys.readouterr()
    return code, json.loads(captured.out) if captured.out.strip() else None, captured.err


def _reply(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _pending(cfg):
    return inbox.list_candidates(cfg, "amber", status="pending", limit=200)["items"]


# --------------------------------------------------------------------------- ChatGPT


def test_chatgpt_parsers_are_defensive_and_keep_only_the_visible_text():
    export = importers.Export(FIXTURES / "chatgpt")
    errors = []
    items = [item for _, item in importers.chatgpt_memories(export, errors)]
    assert [i.ref for i in items] == ["memory mem-001", "memory mem-002", "memory mem-003",
                                      f"memory {importers._digest('Uses zsh as the login shell on every workstation.')}"]
    assert items[0].observed_at == "2026-03-04" and items[1].observed_at == "2026-01-01"
    assert items[0].tags == ["chatgpt-memory"] and items[0].title.startswith("Prefers tabs")
    assert len(errors) == 2 and any("entry 3 has no text" in e for e in errors)
    conversations = importers.chatgpt_conversations(export, errors)
    first, broken = conversations
    assert first.ref == "conversation conv-aaa" and first.title == "Proxy setup"
    assert [role for role, _ in first.messages] == ["User", "Assistant"]
    assert "abandoned" not in json.dumps(first.messages) and "tool output" not in json.dumps(first.messages)
    assert first.messages[1][1] == "The reverse proxy runs on vmhost; its config is /etc/nginx/site.conf."
    assert broken.messages == [] and any("conversation 2 is not an object" in e for e in errors)


def test_chatgpt_branch_without_current_node_falls_back_to_time_order():
    mapping = {"b": {"message": {"author": {"role": "assistant"}, "create_time": 2,
                                 "content": {"content_type": "text", "parts": ["second"]}}},
               "a": {"message": {"author": {"role": "user"}, "create_time": 1,
                                 "content": {"content_type": "text", "parts": ["first"]}}},
               "c": {"message": {"author": {"role": "user"}, "create_time": "bad",
                                 "content": {"content_type": "code", "text": "print()"}}},
               "loop": {"parent": "loop", "message": None}}
    texts = [importers._chatgpt_text(m) for m in importers._chatgpt_branch(mapping, None)]
    assert texts[-2:] == ["first", "second"] and "print()" not in texts
    assert importers._chatgpt_branch(mapping, "loop") == []        # a parent cycle ends
    assert importers._chatgpt_branch("nope", "a") == []


def test_chatgpt_zip_import_redacts_tags_and_never_duplicates_on_rerun(store, tmp_path, capsys):
    export = _zip(tmp_path, "chatgpt")
    before = head(store.clone)
    code, report, err = _json_run(capsys, "chatgpt", str(export))
    assert code == 0, err
    assert len(report["filed"]) == 4 and report["read"] == 4 and not report["capped"]
    assert report["conversations"] == 2 and report["conversations_distilled"] == 0
    assert "--distill" in report["notes"][0]
    assert len(report["errors"]) == 3
    items = {i["title"]: inbox.get(store, "amber", i["id"]) for i in report["filed"]}
    key = next(v for t, v in items.items() if t.startswith("Deploy key"))
    assert "placeholderplaceholder" not in key["body"] and "[REDACTED]" in key["body"]
    assert key["source"] == "import:chatgpt" and key["proposer"] == "mem-import"
    assert key["meta"]["source"] == "chatgpt export export.zip memory mem-003"
    assert key["meta"]["tags"] == ["chatgpt-memory", "import", "import:chatgpt"]
    nfs = next(v for t, v in items.items() if "NFS" in v["body"])
    assert nfs["meta"]["observed_at"] == "2026-01-01"
    assert head(store.clone) == before                        # nothing saved before review

    # Decide two of them; a re-run files nothing, whatever their status.
    ids = [i["id"] for i in report["filed"]]
    inbox.approve(store, "amber", ids[0], reviewer="owner")
    inbox.reject(store, "amber", ids[1], reviewer="owner", reason="no")
    code, again, _ = _json_run(capsys, "chatgpt", str(export))
    assert code == 0 and again["filed"] == []
    assert {s["duplicate_of"] for s in again["skipped"]} == {"already imported"}
    # Another label changes the source, but the same text is still a duplicate.
    code, relabelled, _ = _json_run(capsys, "chatgpt", str(export), "--source-label", "laptop chatgpt")
    assert code == 0 and relabelled["filed"] == []
    assert all(s["duplicate_of"] != "already imported" for s in relabelled["skipped"])


def test_dry_run_writes_nothing(store, tmp_path, capsys):
    before = head(store.clone)
    code, captured = _run(capsys, "chatgpt", str(FIXTURES / "chatgpt"), "--dry-run")
    assert code == 0
    assert "would file 4" in captured.out and "would file: Prefers tabs" in captured.out
    assert "placeholderplaceholder" not in captured.out
    assert not inbox.inbox_path(store, "amber").exists()
    code, captured = _run(capsys, "markdown", str(FIXTURES / "markdown"), "--dry-run")
    assert code == 0 and "would file" in captured.out
    assert not inbox.inbox_path(store, "amber").exists() and head(store.clone) == before


def test_cap_respects_max_and_the_pending_limit(store, tmp_path, capsys, monkeypatch):
    code, report, _ = _json_run(capsys, "chatgpt", str(FIXTURES / "chatgpt"), "--max", "2")
    assert code == 0 and len(report["filed"]) == 2 and report["capped"] and report["cap"] == 2
    monkeypatch.setattr(inbox, "MAX_PENDING", 3)
    code, report, _ = _json_run(capsys, "chatgpt", str(FIXTURES / "chatgpt"))
    assert code == 0 and report["cap"] == 1 and len(report["filed"]) == 1 and report["capped"]
    code, report, _ = _json_run(capsys, "chatgpt", str(FIXTURES / "chatgpt"))
    assert report["cap"] == 0 and report["filed"] == [] and report["capped"]
    assert len(_pending(store)) == 3
    with pytest.raises(SystemExit):
        importers.main(["chatgpt", str(FIXTURES / "chatgpt"), "--max", "0"])


def test_malformed_exports_are_reported_not_crashed(store, tmp_path, capsys):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "conversations.json").write_text("{not json")
    (bad / "memories.json").write_text(json.dumps({"unexpected": True}))
    code, report, _ = _json_run(capsys, "chatgpt", str(bad))
    assert code == 1 and report["filed"] == [] and report["ok"] is False
    assert any("not valid JSON" in e for e in report["errors"])
    assert any("no list of memories" in e for e in report["errors"])
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "user.json").write_text("{}")
    assert importers.main(["claude", str(empty)]) == 1
    assert "no memories" in capsys.readouterr().err
    assert importers.main(["chatgpt", str(tmp_path / "missing.zip")]) == 1
    assert "no such file" in capsys.readouterr().err
    single = tmp_path / "my-export.json"                 # one JSON file is a memories file
    single.write_text(json.dumps(["The printer on the second floor needs A4 paper only."]))
    code, report, _ = _json_run(capsys, "chatgpt", str(single))
    assert code == 0 and len(report["filed"]) == 1
    torn = tmp_path / "torn.zip"
    torn.write_bytes(_zip(tmp_path, "chatgpt").read_bytes()[:200])
    assert importers.main(["chatgpt", str(torn)]) == 1


def test_distill_path_sends_only_redacted_visible_text_and_skips_on_rerun(store, tmp_path, capsys, monkeypatch):
    assert importers.main(["chatgpt", str(FIXTURES / "chatgpt"), "--distill", "--max", "10"]) == 1
    assert "MEMD_LLM_URL" in capsys.readouterr().err
    monkeypatch.setenv("MEMD_LLM_URL", "http://llm.test")
    monkeypatch.setenv("MEMD_LLM_MODEL", "chat-model")
    facts = {"facts": [{"title": "Reverse proxy host", "tags": ["proxy"], "importance": 3,
                        "body": "The reverse proxy runs on vmhost; its config is /etc/nginx/site.conf."}]}
    with respx.mock(assert_all_called=True) as router:
        route = router.post(LLM_URL).mock(return_value=_reply(json.dumps(facts)))
        code, report, err = _json_run(capsys, "chatgpt", str(FIXTURES / "chatgpt"), "--distill")
    assert code == 0, err
    assert route.call_count == 1                      # the empty conversation costs no call
    prompt = json.dumps(json.loads(route.calls.last.request.content)["messages"])
    assert "supersecretvalue99" not in prompt and "abandoned" not in prompt and "/etc/nginx/site.conf" in prompt
    assert report["conversations_distilled"] == 1 and len(report["filed"]) == 5
    fact = next(i for i in report["filed"] if i["title"] == "Reverse proxy host")
    item = inbox.get(store, "amber", fact["id"])
    assert item["source"] == "import:chatgpt"
    assert item["meta"]["source"] == "chatgpt export chatgpt conversation conv-aaa"
    assert {"import", "import:chatgpt", "chatgpt-conversation", "proxy"} <= set(item["meta"]["tags"])
    with respx.mock(assert_all_called=False) as router:
        route = router.post(LLM_URL).mock(return_value=_reply(json.dumps(facts)))
        code, report, _ = _json_run(capsys, "chatgpt", str(FIXTURES / "chatgpt"), "--distill")
    assert code == 0 and report["filed"] == [] and route.call_count == 0
    assert any(s["duplicate_of"] == "already distilled" for s in report["skipped"])


def test_distill_respects_the_cap(store, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MEMD_LLM_URL", "http://llm.test")
    monkeypatch.setenv("MEMD_LLM_MODEL", "chat-model")
    facts = {"facts": [{"title": f"Fact {n}", "tags": [], "importance": 3,
                        "body": f"Distinct durable fact number {n} about service{n} on host{n}."} for n in range(4)]}
    with respx.mock() as router:
        router.post(LLM_URL).mock(return_value=_reply(json.dumps(facts)))
        code, report, _ = _json_run(capsys, "claude", str(FIXTURES / "claude"), "--distill", "--max", "7",
                                    "--dry-run")
    assert code == 0 and len(report["would_file"]) == 7 and report["capped"]
    assert not inbox.inbox_path(store, "amber").exists()


# --------------------------------------------------------------------------- Claude


def test_claude_parsers_read_memory_projects_and_conversations():
    export = importers.Export(FIXTURES / "claude")
    errors = []
    memories = [item for _, item in importers.claude_memories(export, errors)]
    assert [m.title for m in memories] == ["Work context", "Preferences",
                                           "The homelab project tracks vmhost and lapbox"]
    assert memories[2].ref.startswith("project p-0001 memory ")
    projects = [item for _, item in importers.claude_projects(export, errors)]
    assert [p.title for p in projects] == ["Project Homelab", "Homelab: network.md"]
    assert "Instructions:\nAnswer tersely" in projects[0].body and projects[0].observed_at == "2026-01-10"
    assert projects[1].ref == "project p-0001 document d-0001" and projects[1].observed_at == "2026-01-11"
    assert any("document 1 has no text" in e for e in errors) and any("project 2 is not" in e for e in errors)
    conversations = importers.claude_conversations(export, errors)
    assert [c.ref for c in conversations] == ["conversation c-0001", "conversation c-0002"]
    assert conversations[0].messages == [
        ("User", "We moved nightly backups to 02:30. password: hunter2hunter2"),
        ("Assistant", "Noted: nightly backups of lapbox now start at 02:30.")]
    assert any("conversation 2 is not" in e for e in errors)


def test_claude_zip_import(store, tmp_path, capsys):
    code, report, err = _json_run(capsys, "claude", str(_zip(tmp_path, "claude", "claude-data.zip")))
    assert code == 0, err
    assert [i["title"] for i in report["filed"]] == [
        "Work context", "Preferences", "The homelab project tracks vmhost and lapbox",
        "Project Homelab", "Homelab: network.md"]
    item = inbox.get(store, "amber", report["filed"][3]["id"])
    assert item["meta"]["source"] == "claude export claude-data.zip project p-0001"
    assert item["meta"]["tags"] == ["claude-project", "import", "import:claude"]
    code, again, _ = _json_run(capsys, "claude", str(_zip(tmp_path, "claude", "claude-data.zip")))
    assert again["filed"] == [] and len(again["skipped"]) == 5


# --------------------------------------------------------------------------- Markdown


def test_markdown_note_title_tags_dates_and_body():
    item, warning = importers.markdown_note((FIXTURES / "markdown/notes/backup-window.md").read_text(),
                                            "notes/backup-window.md")
    assert (item.title, item.tags, item.observed_at, item.importance) == \
        ("Backup window", ["backup", "archive"], "2026-04-01", 4) and warning is None
    item, _ = importers.markdown_note("# Reverse proxy\n\nBody text here.", "p.md")
    assert item.title == "Reverse proxy" and item.body == "Body text here."
    item, _ = importers.markdown_note("Intro line.\n\n# Later heading\n\nMore.", "x.md")
    assert item.title == "Later heading" and item.body.startswith("Intro line.")
    item, _ = importers.markdown_note("Plain text only.", "dir/shell_habits.md")
    assert item.title == "shell habits"
    item, warning = importers.markdown_note("---\ntitle: [unclosed\n---\nStill imported.", "b.md")
    assert item.title == "b" and item.body == "Still imported." and "malformed frontmatter" in warning
    item, _ = importers.markdown_note("---\ntags: '#one, two'\n---\nText.", "t.md")
    assert item.tags == ["one", "two"]
    assert importers.markdown_note("---\ntitle: x\n---\n", "e.md")[0] is None


@pytest.mark.parametrize("rel,patterns,is_dir,expected", [
    ("drafts/a.md", ["drafts/"], False, True),
    ("notes/drafts/a.md", ["drafts/"], False, True),
    ("drafts", ["drafts/"], False, False),
    ("drafts", ["drafts/"], True, True),
    ("notes/a.md", ["*.md"], False, True),
    ("notes/a.md", ["/notes/b.md"], False, False),
    ("notes/b.md", ["/notes/b.md"], False, True),
    ("x/y/z.md", ["**/y/*.md"], False, True),
    ("y/z.md", ["**/y/*.md"], False, True),
    ("notes/a.md", ["# comment", ""], False, False),
])
def test_exclude_patterns_are_gitignore_style(rel, patterns, is_dir, expected):
    assert importers._excluded(rel, patterns, is_dir=is_dir) is expected


def test_markdown_import_skips_excluded_binary_and_huge_files(store, tmp_path, capsys):
    folder = tmp_path / "vault"
    shutil.copytree(FIXTURES / "markdown", folder)
    (folder / "notes" / "blob.md").write_bytes(b"\x89PNG\x00\x00binary")
    (folder / "notes" / "latin.md").write_bytes("caf\xe9".encode("latin-1"))
    (folder / "notes" / "huge.md").write_text("x" * 5000)
    (folder / ".obsidian").mkdir()
    (folder / ".obsidian" / "hidden.md").write_text("Hidden settings note.")
    code, report, err = _json_run(capsys, "markdown", str(folder), "--exclude", "drafts/", "--max-bytes", "4096")
    assert code == 0, err
    titles = sorted(i["title"] for i in report["filed"])
    assert titles == ["Backup window", "Reverse proxy", "broken frontmatter", "shell habits"]
    reasons = {s["ref"]: s.get("reason") for s in report["skipped"]}
    assert reasons["notes/blob.md"] == "binary" and reasons["notes/latin.md"] == "not UTF-8"
    assert reasons["notes/huge.md"].startswith("larger than") and reasons["notes/empty.md"] == "empty"
    assert "drafts/draft.md" not in json.dumps(report)          # excluded directories are pruned
    assert any("malformed frontmatter" in e for e in report["errors"])
    proxy = inbox.get(store, "amber", next(i["id"] for i in report["filed"] if i["title"] == "Reverse proxy"))
    assert "abc123def456ghi" not in proxy["body"] and proxy["meta"]["source"] == "markdown vault notes/proxy.md"
    backup = inbox.get(store, "amber", next(i["id"] for i in report["filed"] if i["title"] == "Backup window"))
    assert backup["meta"]["tags"] == ["backup", "archive", "import", "import:markdown"]
    assert backup["meta"]["importance"] == 4 and backup["meta"]["observed_at"] == "2026-04-01"
    code, again, _ = _json_run(capsys, "markdown", str(folder), "--exclude", "drafts/", "--max-bytes", "4096")
    assert code == 0 and again["filed"] == []


def test_markdown_in_a_git_repo_uses_commit_dates_and_gitignore(store, tmp_path, capsys):
    folder = tmp_path / "repo"
    shutil.copytree(FIXTURES / "markdown", folder)
    (folder / ".gitignore").write_text("drafts/\n")
    (folder / "notes" / "untracked.md").write_text("An untracked but not ignored note about the NAS fans.")
    env = {**os.environ, "GIT_COMMITTER_DATE": "2025-12-24T12:00:00Z", "GIT_AUTHOR_DATE": "2025-12-24T12:00:00Z"}
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"],
                 ["add", "notes", ".gitignore"], ["reset", "-q", "notes/untracked.md"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(folder), *args], check=True, env=env, capture_output=True)
    code, report, _ = _json_run(capsys, "markdown", str(folder), "--dry-run", "--source-label", "wiki")
    assert code == 0
    facts = {f["title"]: f for f in report["would_file"]}
    assert "Draft" not in facts and "untracked" in facts
    assert facts["Reverse proxy"]["observed_at"] == "2025-12-24"        # from git log
    assert facts["Backup window"]["observed_at"] == "2026-04-01"        # frontmatter wins
    assert "observed_at" not in facts["untracked"]
    assert facts["Reverse proxy"]["source"] == "wiki notes/proxy.md"
    assert importers.main(["markdown", str(tmp_path / "nope")]) == 1


# --------------------------------------------------------------------------- GitHub


def test_decision_text_keeps_decisions_rationale_and_breaking_changes():
    text = importers.decision_text(
        "<!-- template -->\n## Summary\nChanged things.\n\n## Why\nThe old approach leaked memory.\n\n"
        "## Checklist\n- [x] tests\n\n**Alternatives considered**\nA queue; too heavy.\n\n"
        "## Notes\nUnrelated paragraph.\n\nBREAKING CHANGE: the flag is renamed.")
    assert "## Summary" not in text and "Changed things" not in text and "template" not in text
    assert "### Why\nThe old approach leaked memory." in text
    assert "### Alternatives considered\nA queue; too heavy." in text
    assert "BREAKING CHANGE: the flag is renamed." in text and "Unrelated" not in text
    assert importers.decision_text("## Decision\n- [ ] todo\n") == ""
    assert importers.decision_text("") == ""


def test_github_prs_imports_merged_decisions_with_pr_url_as_source(store, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-value")
    page = json.loads((FIXTURES / "github" / "pulls-page1.json").read_text())
    with respx.mock(assert_all_called=True) as router:
        route = router.get(PULLS).mock(return_value=httpx.Response(200, json=page))
        code, report, err = _json_run(capsys, "github-prs", "example-org/widgets")
    assert code == 0, err
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer test-token-value"
    assert request.url.params["state"] == "closed" and request.url.params["page"] == "1"
    assert [i["title"] for i in report["filed"]] == ["Switch the index to sqlite-vec", "Drop Python 3.12",
                                                     "Rotate the deploy token"]
    reasons = {s["ref"]: s.get("reason") for s in report["skipped"]}
    assert reasons == {"https://github.com/example-org/widgets/pull/40": "no decision text"}
    first = inbox.get(store, "amber", report["filed"][0]["id"])
    assert first["source"] == "import:github-prs"
    assert first["meta"]["source"] == "https://github.com/example-org/widgets/pull/42"
    assert first["meta"]["observed_at"] == "2026-05-02"
    assert first["meta"]["tags"] == ["decision", "architecture", "import", "import:github-prs"]
    assert "sqlite-vec instead of a separate vector database" in first["body"]
    assert "Checklist" not in first["body"] and "PR template" not in first["body"]
    assert first["body"].endswith("Pull request #42 in example-org/widgets, merged 2026-05-02: "
                                  "https://github.com/example-org/widgets/pull/42")
    breaking = inbox.get(store, "amber", report["filed"][1]["id"])
    assert "BREAKING CHANGE" in breaking["body"] and "typo" not in breaking["body"]
    token = inbox.get(store, "amber", report["filed"][2]["id"])
    assert "placeholderplaceholder" not in token["body"]
    with respx.mock() as router:
        router.get(PULLS).mock(return_value=httpx.Response(200, json=page))
        code, again, _ = _json_run(capsys, "github-prs", "example-org/widgets")
    assert code == 0 and again["filed"] == []


def test_github_since_stops_paging_and_errors_exit_nonzero(store, tmp_path, capsys):
    page = json.loads((FIXTURES / "github" / "pulls-page1.json").read_text())
    with respx.mock() as router:
        route = router.get(PULLS).mock(return_value=httpx.Response(200, json=page))
        code, report, _ = _json_run(capsys, "github-prs", "example-org/widgets", "--since", "2026-04-19",
                                    "--dry-run")
    assert code == 0 and route.call_count == 1
    assert [f["title"] for f in report["would_file"]] == ["Switch the index to sqlite-vec", "Drop Python 3.12"]
    assert "authorization" not in route.calls.last.request.headers
    full = [dict(page[0], number=n, html_url=f"https://github.com/example-org/widgets/pull/{n}",
                 title=f"Decision {n}", body=f"## Decision\nChose option {n} for component{n} after load test {n * 7}.")
            for n in range(100, 200)]
    with respx.mock() as router:
        route = router.get(PULLS).mock(side_effect=[httpx.Response(200, json=full),
                                                    httpx.Response(200, json=[])])
        code, report, _ = _json_run(capsys, "github-prs", "example-org/widgets", "--dry-run", "--max", "150")
    assert code == 0 and route.call_count == 2 and len(report["would_file"]) == 100
    with respx.mock() as router:
        router.get(PULLS).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
        code, report, _ = _json_run(capsys, "github-prs", "example-org/widgets")
    assert code == 1 and "not found" in report["errors"][0]
    with respx.mock() as router:
        router.get(PULLS).mock(return_value=httpx.Response(403, headers={"x-ratelimit-remaining": "0"}))
        code, report, _ = _json_run(capsys, "github-prs", "example-org/widgets")
    assert code == 1 and "rate limited" in report["errors"][0]
    with respx.mock() as router:
        router.get(PULLS).mock(return_value=httpx.Response(200, json={"message": "odd"}))
        code, report, _ = _json_run(capsys, "github-prs", "example-org/widgets")
    assert code == 1 and "list of pull requests" in report["errors"][0]
    code, report, _ = _json_run(capsys, "github-prs", "not a repo")
    assert code == 1 and "OWNER/REPO" in report["errors"][0]
    code, report, _ = _json_run(capsys, "github-prs", "example-org/widgets", "--since", "last week")
    assert code == 1 and "YYYY-MM-DD" in report["errors"][0]
    assert not inbox.inbox_path(store, "amber").exists()


def test_github_stops_fetching_once_the_cap_is_reached(store, capsys):
    full = [{"number": n, "title": f"Decision {n}", "merged_at": "2026-05-01T00:00:00Z",
             "updated_at": "2026-05-01T00:00:00Z", "html_url": f"https://github.com/example-org/widgets/pull/{n}",
             "body": f"## Decision\nChose option {n} for component{n} after load test {n * 7 + 100}."}
            for n in range(100)]
    with respx.mock() as router:
        route = router.get(PULLS).mock(return_value=httpx.Response(200, json=full))
        code, report, _ = _json_run(capsys, "github-prs", "example-org/widgets", "--dry-run", "--max", "3")
    assert code == 0 and route.call_count == 1 and len(report["would_file"]) == 3 and report["capped"]


def test_console_script_declared():
    import tomllib
    data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["mem-import"] == "memd.importers:main"
