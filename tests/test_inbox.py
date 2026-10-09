"""Review inbox (memd.inbox): proposals wait for a person before they are written.

Hermetic: temp Git/SQLite stores, a respx-mocked chat endpoint, no model
backends; the socket guard blocks everything else.
"""
import asyncio
import json
import os
import stat
import subprocess

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import memd.mcp as mcp_mod
import memd.save as save_mod
import memd.server as server_mod
from memd import actor, inbox, recall as recall_mod
from memd.config import Config
from memd.store import list_notes, read_note

TOKEN = "i" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MCP_HEADERS = {**AUTH, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
LLM_URL = "http://llm.test/v1/chat/completions"


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
                       "MEMD_TOKEN": TOKEN, "MEMD_LOCAL_HOST": "any",
                       "MEMD_EMBED_URL": "http://127.0.0.1:9",
                       "MEMD_RERANK_URL": "http://127.0.0.1:9"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(recall_mod, "embed_with_deadline", lambda *a, **k: None)
    monkeypatch.setattr(recall_mod, "rerank", lambda *a, **k: None)
    monkeypatch.setattr(save_mod, "_vector_near_matches", lambda cfg, body: [])
    monkeypatch.setattr(save_mod, "_pull_rebase_push", lambda clone: None)
    actor.set_actor("")
    cfg = Config.from_env(env_file=None)
    ensure_lexical(cfg)
    return cfg


def _mcp(client, name, arguments, headers=MCP_HEADERS):
    r = client.post("/mcp/", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


FACT = {"title": "Backup window", "body": "Nightly backups of the archive run at 02:30 on lapbox.",
        "tags": ["backup"], "importance": 4, "host": "lapbox"}


# --------------------------------------------------------------------------- storage


def test_propose_stores_candidate_owner_only_with_lint_and_dedupes(store):
    before = head(store.clone)
    out = inbox.propose(dict(FACT), "amber", cfg=store, proposer="agent-a")
    assert out["ok"] and out["status"] == "pending" and not out["duplicate"]
    assert out["lint"]["action"] == "create" and out["lint"]["slug"] == "backup-window"
    path = inbox.inbox_path(store, "amber")
    assert path == store.db.with_name("memd.inbox.db")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert head(store.clone) == before and read_note(store.clone, "backup-window") is None
    again = inbox.propose({**FACT, "body": "  Nightly backups of the archive   run at 02:30 on lapbox. "},
                          "amber", cfg=store)
    assert again["duplicate"] and again["id"] == out["id"]
    listed = inbox.list_candidates(store, "amber")
    assert listed["counts"] == {"pending": 1, "approved": 0, "rejected": 0}
    [item] = listed["items"]
    assert item["meta"] == {"host": "lapbox", "importance": 4, "tags": ["backup"]}
    assert item["source"] == "agent" and item["proposer"] == "agent-a"
    full = inbox.get(store, "amber", out["id"])
    assert full["body"] == FACT["body"] and full["receipt"] is None
    with pytest.raises(FileNotFoundError):
        inbox.get(store, "amber", "0" * 16)
    with pytest.raises(ValueError):
        inbox.get(store, "amber", "../etc")


def test_dry_run_reports_identity_duplicates_and_errors_without_writing(store):
    before = head(store.clone)
    files = sorted(p.name for p in store.clone.iterdir())
    update = save_mod.dry_run({"slug": "vmhost-proxmox-vm", "body": "Trackr VM 210 now boots on start."},
                              "amber", cfg=store)
    assert update["action"] == "update" and update["error"] is None
    restated = save_mod.dry_run({"title": "Repos policy", "body": "ALL new repos go to Forgejo only "
                                 "(svcuser/ on 10.10.1.10:3000); never GitHub."}, "amber", cfg=store)
    assert [d["slug"] for d in restated["near_duplicates"]] == ["repo-hosting-policy"]
    assert "repo-hosting-policy" in restated["related"]
    bad = save_mod.dry_run({"title": "x", "body": "replacement", "supersedes": "no-such-note"},
                           "amber", cfg=store)
    assert "supersedes must name" in bad["error"]
    stale = save_mod.dry_run({"slug": "vmhost-proxmox-vm", "body": "b", "expected_revision": "0" * 40},
                             "amber", cfg=store)
    assert "note changed" in stale["error"]
    sup = save_mod.dry_run({"title": "Forgejo policy v2", "body": "Repos go to Forgejo.",
                            "supersedes": "repo-hosting-policy"}, "amber", cfg=store)
    assert sup["action"] == "supersede" and sup["supersedes"] == "repo-hosting-policy"
    assert head(store.clone) == before and sorted(p.name for p in store.clone.iterdir()) == files


def test_approve_with_edits_writes_through_save_and_records_receipt(store):
    cid = inbox.propose(dict(FACT), "amber", cfg=store, proposer="agent-a")["id"]
    out = inbox.approve(store, "amber", cid, reviewer="owner",
                        edits={"title": "Archive backup window", "tags": ["backup", "archive"], "host": None})
    assert out["status"] == "approved" and out["edited"]
    receipt = out["receipt"]
    assert receipt["saved"] and receipt["action"] == "created" and receipt["revision"]
    note = read_note(store.clone, receipt["slug"])
    assert note.title == "Archive backup window" and note.tags == ["backup", "archive"]
    assert note.saved_by == "agent-a"            # attributed to the proposer
    assert note.importance == 4
    item = inbox.get(store, "amber", cid)
    assert (item["status"], item["reviewer"], item["slug"], item["revision"]) == \
        ("approved", "owner", receipt["slug"], receipt["revision"])
    assert item["receipt"]["slug"] == receipt["slug"]
    assert item["original"]["title"] == "Backup window"
    with pytest.raises(inbox.NotPending):
        inbox.approve(store, "amber", cid, reviewer="owner")
    with pytest.raises(inbox.NotPending):
        inbox.reject(store, "amber", cid, reviewer="owner")
    with pytest.raises(ValueError):
        inbox.approve(store, "amber", inbox.propose({"body": "another fact entirely"}, "amber", cfg=store)["id"],
                      reviewer="owner", edits={"saved_by": "forged"})


def test_failed_approval_stays_pending_with_error_and_reject_writes_nothing(store):
    cid = inbox.propose({"title": "Retire", "body": "Replace a missing note.", "supersedes": "no-such-note"},
                        "amber", cfg=store)["id"]
    assert "supersedes" in inbox.get(store, "amber", cid)["lint"]["error"]
    with pytest.raises(ValueError):
        inbox.approve(store, "amber", cid, reviewer="owner")
    item = inbox.get(store, "amber", cid)
    assert item["status"] == "pending" and "supersedes" in item["last_error"]
    before = head(store.clone)
    out = inbox.reject(store, "amber", cid, reviewer="owner", reason="wrong target")
    assert out["status"] == "rejected" and head(store.clone) == before
    item = inbox.get(store, "amber", cid)
    assert item["reason"] == "wrong target" and item["decided_at"]
    assert inbox.list_candidates(store, "amber", status="rejected")["total"] == 1


# --------------------------------------------------------------------------- HTTP and MCP


def test_http_propose_and_token_cannot_review_by_default(store):
    with TestClient(server_mod.create_token_app()) as client:
        assert client.post("/propose", json=FACT).status_code == 401
        r = client.post("/propose", headers=AUTH, json=FACT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["proposed"] and len(body["id"]) == 16 and body["lint"]["action"] == "create"
        assert client.post("/propose", headers=AUTH, json={"tags": ["x"]}).status_code == 400
        for method, path in (("get", "/inbox"), ("get", f"/inbox/{body['id']}"),
                             ("post", f"/inbox/{body['id']}/approve"), ("post", f"/inbox/{body['id']}/reject")):
            response = getattr(client, method)(path, headers=AUTH)
            assert response.status_code == 403, (path, response.text)
    assert inbox.get(store, "amber", body["id"])["proposer"] == "legacy"
    assert read_note(store.clone, "backup-window") is None


def test_token_review_setting_never_lets_a_token_approve_its_own_proposal(store, monkeypatch):
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "others")
    with TestClient(server_mod.create_token_app()) as client:
        cid = client.post("/propose", headers=AUTH, json=FACT).json()["id"]
        assert client.get("/inbox", headers=AUTH).json()["total"] == 1
        r = client.post(f"/inbox/{cid}/approve", headers=AUTH, json={})
        assert r.status_code == 403 and "own proposal" in r.json()["detail"]
        monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "all")
        r = client.post(f"/inbox/{cid}/approve", headers=AUTH, json={"edits": {"importance": 2}})
        assert r.status_code == 200, r.text
        assert r.json()["receipt"]["saved"] is True
        assert client.post(f"/inbox/{cid}/approve", headers=AUTH, json={}).status_code == 409
        assert client.post(f"/inbox/{cid}/approve", headers=AUTH, json={"bogus": 1}).status_code == 400
    assert read_note(store.clone, "backup-window").importance == 2


def test_mcp_propose_tool_mirrors_save_schema(store):
    tools = {t.name: t for t in asyncio.run(mcp_mod.list_tools())}
    assert tools["propose"].input_schema == tools["save"].input_schema
    assert tools["propose"].input_schema is not tools["save"].input_schema
    with TestClient(server_mod.create_token_app()) as client:
        out = _mcp(client, "propose", {"content": "The archive NAS exports /srv/share over NFS v4."})
    assert out["isError"] is False
    cid = out["structuredContent"]["id"]
    item = inbox.get(store, "amber", cid)
    assert item["status"] == "pending" and item["proposer"] == "legacy"
    assert not any("NFS" in n.body for n in list_notes(store.clone))


def test_save_mode_inbox_routes_agent_saves_to_the_inbox(store, monkeypatch):
    monkeypatch.setenv("MEMD_SAVE_MODE", "inbox")
    before = head(store.clone)
    with TestClient(server_mod.create_token_app()) as client:
        r = client.post("/save", headers=AUTH, json=FACT)
        assert r.status_code == 202, r.text
        receipt = r.json()
        assert receipt["queued"] and receipt["saved"] is False and receipt["action"] == "proposed"
        assert receipt["slug"] == "backup-window" and "Queued for review" in receipt["warnings"][0]
        out = _mcp(client, "save", {"title": "Printer", "body": "The office printer is on the guest VLAN."})
        assert out["isError"] is False and out["structuredContent"]["queued"] is True
    assert head(store.clone) == before
    assert inbox.list_candidates(store, "amber")["total"] == 2
    assert {i["source"] for i in inbox.list_candidates(store, "amber")["items"]} == {"save"}
    # Direct mode (the default) is unchanged.
    monkeypatch.setenv("MEMD_SAVE_MODE", "direct")
    with TestClient(server_mod.create_token_app()) as client:
        assert client.post("/save", headers=AUTH, json=FACT).json()["saved"] is True


# --------------------------------------------------------------------------- account sessions


@pytest.fixture
def accounts(config, tmp_path, monkeypatch):
    from memd import control, sources
    from memd.refresh import ensure_lexical
    monkeypatch.setenv("MEMD_ADMIN_DB", str(tmp_path / "control" / "admin.db"))
    monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "1")
    monkeypatch.setenv("MEMD_LOCAL_HOST", "any")
    monkeypatch.setattr(sources._jobs, "submit", lambda fn, *args: fn(*args))
    monkeypatch.setattr(save_mod, "_pull_rebase_push", lambda clone: None)
    monkeypatch.setattr(save_mod, "_vector_near_matches", lambda cfg, body: [])
    control.initialize()
    control.register_existing()
    control.create_user("owner", "test-password-long-enough", "admin")
    ensure_lexical(config)
    app = server_mod.create_token_app()
    with TestClient(app) as client:
        login = client.post("/ui/login", json={"username": "owner", "password": "test-password-long-enough"})
        assert login.status_code == 200
        csrf = login.json()["csrf"]
        created = client.post("/admin/users", headers={"X-CSRF-Token": csrf}, json={"username": "agent-user"}).json()
        client.put(f'/admin/users/{created["id"]}/grants', headers={"X-CSRF-Token": csrf},
                   json={"grants": {"amber": "write"}})
        issued = client.post("/admin/tokens", headers={"X-CSRF-Token": csrf},
                             json={"label": "laptop-agent", "user_id": created["id"], "store_id": "amber",
                                   "scope": "write"}).json()
        yield client, app, csrf, issued["token"], config


def test_account_session_reviews_and_agent_token_cannot(accounts):
    client, app, csrf, agent, cfg = accounts
    api = TestClient(app)
    agent_auth = {"Authorization": "Bearer " + agent}
    out = _mcp(api, "propose", {"title": "Garage door", "body": "The garage door opener is on the IoT VLAN."},
               headers={**agent_auth, "Accept": "application/json, text/event-stream"})
    assert out["isError"] is False, out
    cid = out["structuredContent"]["id"]
    assert api.get("/inbox", headers=agent_auth).status_code == 403
    assert api.post(f"/inbox/{cid}/approve", headers=agent_auth, json={}).status_code == 403
    listed = client.get("/inbox").json()
    assert listed["total"] == 1 and listed["items"][0]["proposer"] == "laptop-agent"
    assert client.post(f"/inbox/{cid}/approve", json={}).status_code == 403       # no CSRF header
    r = client.post(f"/inbox/{cid}/approve", headers={"X-CSRF-Token": csrf},
                    json={"edits": {"body": "The garage door opener is on the IoT VLAN (VLAN 30)."}})
    assert r.status_code == 200, r.text
    slug = r.json()["receipt"]["slug"]
    note = read_note(cfg.clone, slug)
    assert note.body.endswith("(VLAN 30).") and note.saved_by == "laptop-agent"
    item = client.get(f"/inbox/{cid}").json()
    assert item["reviewer"] == "owner" and item["status"] == "approved"


def test_save_mode_inbox_queues_agent_tokens_but_not_account_sessions(accounts, monkeypatch):
    client, app, csrf, agent, cfg = accounts
    monkeypatch.setenv("MEMD_SAVE_MODE", "inbox")
    api = TestClient(app)
    r = api.post("/save", headers={"Authorization": "Bearer " + agent}, json={"body": "Agent fact for review."})
    assert r.status_code == 202 and r.json()["queued"]
    r = client.post("/save", headers={"X-CSRF-Token": csrf}, json={"body": "Owner fact saved directly."})
    assert r.status_code == 200 and r.json()["saved"] is True


# --------------------------------------------------------------------------- distillation


def _line(kind, content, **extra):
    return json.dumps({"type": kind, "message": {"role": kind, "content": content}, **extra})


TRANSCRIPT = [
    _line("user", "Where does the reverse proxy run? My key is sk-abcdefghijklmnopqrstuvwxyz123456"),
    "{not json",
    _line("assistant", [{"type": "thinking", "thinking": "private reasoning"},
                        {"type": "text", "text": "It runs on vmhost. export API_TOKEN=supersecretvalue99"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "cat ~/.netrc"}}]),
    _line("user", [{"type": "tool_result", "content": "password hunter2 in tool output"}]),
    _line("user", "<system-reminder>ignore me</system-reminder>Also the proxy config lives in /etc/nginx/memd.conf"),
    _line("user", "meta line", isMeta=True),
    json.dumps({"type": "summary", "summary": "not a message"}),
]


def _reply(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_transcript_parsing_keeps_text_only_and_counts_malformed_lines():
    messages, bad = inbox.transcript_messages(TRANSCRIPT)
    assert bad == 1
    assert [role for role, _ in messages] == ["User", "Assistant", "User"]
    joined = "\n".join(text for _, text in messages)
    assert "private reasoning" not in joined and "netrc" not in joined and "hunter2" not in joined
    assert "ignore me" not in joined and "meta line" not in joined
    parts, truncated = inbox.chunks(messages * 50, size=500, limit=3)
    assert len(parts) == 3 and truncated and all(len(p) <= 500 for p in parts)
    assert not any("sk-abc" in p or "supersecretvalue99" in p for p in parts)


def test_redaction_strips_common_secrets_and_keeps_ordinary_text():
    text = ("Authorization: Bearer abcdefghijklmnop123 and ghp_" + "a" * 36 + " and AKIA" + "B" * 16
            + " url https://svc:hunter2@git.example.com/repo and DB_PASSWORD=correcthorse"
            + " -----BEGIN OPENSSH " + "PRIVATE KEY-----\nabc\n-----END OPENSSH " + "PRIVATE KEY-----"
            + " jwt eyJhbGciOiJI.eyJzdWIiOiIxMjM0.c2lnbmF0dXJlMTI and memd_" + "x" * 30
            + " commit 0123456789abcdef0123456789abcdef01234567 on port 8077")
    out = inbox.redact(text)
    for secret in ("abcdefghijklmnop123", "ghp_", "AKIA", "hunter2", "correcthorse", "OPENSSH PRIVATE KEY-----\nabc",
                   "eyJhbGci", "memd_x"):
        assert secret not in out, secret
    assert "0123456789abcdef0123456789abcdef01234567" in out and "port 8077" in out
    assert "git.example.com/repo" in out


def test_parse_facts_validates_items_and_rejects_malformed_replies():
    good = inbox.parse_facts('```json\n{"facts": [{"title": "Proxy host", "body": "The reverse proxy runs on '
                             'vmhost.", "tags": ["proxy", 3], "importance": 9}, {"title": "", "body": "x"}, '
                             '"junk", {"title": "Key", "body": "Use token=abcdefghij for access."}]}\n```')
    assert [f["title"] for f in good] == ["Proxy host", "Key"]
    assert good[0]["importance"] == 5 and good[0]["tags"] == ["proxy"]
    assert "abcdefghij" not in good[1]["body"]
    assert inbox.parse_facts('Sure! {"facts": []} done') == []
    for bad in ("no json here", '{"facts": "nope"}', "[1, 2]", '{"facts": [}'):
        with pytest.raises(inbox.FactParseError):
            inbox.parse_facts(bad)


def test_distill_files_new_facts_dedupes_and_never_sends_secrets(store, monkeypatch):
    import dataclasses
    cfg = dataclasses.replace(store, llm_url="http://llm.test", llm_model="chat-model")
    inbox.propose({"title": "NFS export", "body": "The archive exports /srv/share over NFS v4."}, "amber", cfg=cfg)
    facts = {"facts": [
        {"title": "Reverse proxy host", "body": "The reverse proxy runs on vmhost; its config is /etc/nginx/memd.conf.",
         "tags": ["proxy"], "importance": 3},
        {"title": "Reverse proxy host", "body": "The reverse proxy runs on vmhost; its config is /etc/nginx/memd.conf.",
         "tags": ["proxy"], "importance": 3},
        {"title": "Repos policy", "body": "ALL new repos go to Forgejo only (svcuser/ on 10.10.1.10:3000); never GitHub.",
         "tags": [], "importance": 5},
        {"title": "NFS export", "body": "The archive exports /srv/share over NFS v4.", "tags": [], "importance": 3},
    ]}
    with respx.mock(assert_all_called=True) as router:
        route = router.post(LLM_URL).mock(return_value=_reply(json.dumps(facts)))
        report = inbox.distill(TRANSCRIPT, cfg=cfg, profile="amber", name="session.jsonl")
    sent = json.loads(route.calls.last.request.content)
    assert sent["response_format"]["type"] == "json_schema"
    prompt = json.dumps(sent["messages"])
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in prompt and "supersecretvalue99" not in prompt
    assert "hunter2" not in prompt and "/etc/nginx/memd.conf" in prompt
    assert report["malformed_lines"] == 1 and report["extracted"] == 4
    assert [f["title"] for f in report["filed"]] == ["Reverse proxy host"]
    dups = {s["title"]: s["duplicate_of"] for s in report["skipped"]}
    assert dups["Repos policy"] == "note repo-hosting-policy"
    assert dups["NFS export"].startswith("candidate ")
    item = inbox.get(cfg, "amber", report["filed"][0]["id"])
    assert item["source"] == "transcript" and item["meta"]["source"] == "transcript session.jsonl"
    assert item["proposer"] == "mem-inbox distill"


def test_distill_survives_malformed_model_replies_and_needs_a_model(store):
    import dataclasses
    with pytest.raises(RuntimeError):
        inbox.distill(TRANSCRIPT, cfg=store, profile="amber")
    cfg = dataclasses.replace(store, llm_url="http://llm.test", llm_model="chat-model")
    with respx.mock() as router:
        router.post(LLM_URL).mock(return_value=_reply("I could not find any facts, sorry."))
        report = inbox.distill(TRANSCRIPT, cfg=cfg, profile="amber")
    assert report["ok"] is False and report["errors"] and report["filed"] == []
    with respx.mock() as router:
        # A backend that refuses json_schema gets the plain prompt.
        router.post(LLM_URL).mock(side_effect=[httpx.Response(400, text="response_format unsupported"),
                                               _reply('{"facts": []}')])
        report = inbox.distill(TRANSCRIPT, cfg=cfg, profile="amber", dry_run=True)
    assert report["ok"] is True and report["errors"] == []
    assert inbox.list_candidates(cfg, "amber")["total"] == 0


def test_cli_list_show_approve_reject_and_distill_dry_run(store, capsys, tmp_path, monkeypatch):
    first = inbox.propose(dict(FACT), "amber", cfg=store, proposer="agent-a")["id"]
    second = inbox.propose({"title": "Wrong", "body": "The moon is made of cheese."}, "amber", cfg=store)["id"]
    assert inbox.main(["list"]) == 0
    out = capsys.readouterr().out
    assert first in out and second in out and "pending 2" in out
    assert inbox.main(["show", first]) == 0
    assert "Nightly backups" in capsys.readouterr().out
    assert inbox.main(["approve", first, "--title", "Archive backups", "--reviewer", "operator"]) == 0
    assert "approved" in capsys.readouterr().out
    assert read_note(store.clone, "archive-backups").title == "Archive backups"
    assert inbox.main(["reject", second, "--reason", "nonsense"]) == 0
    assert inbox.main(["--json", "list", "--status", "all"]) == 0
    listed = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert {i["status"] for i in listed["items"]} == {"approved", "rejected"}
    assert inbox.main(["approve", second]) == 1
    assert "already rejected" in capsys.readouterr().err
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(TRANSCRIPT))
    assert inbox.main(["distill", str(transcript)]) == 1       # no MEMD_LLM_URL
    assert "MEMD_LLM_URL" in capsys.readouterr().err
    monkeypatch.setenv("MEMD_LLM_URL", "http://llm.test")
    monkeypatch.setenv("MEMD_LLM_MODEL", "chat-model")
    with respx.mock() as router:
        router.post(LLM_URL).mock(return_value=_reply(json.dumps({"facts": [
            {"title": "Proxy config", "body": "The proxy config lives in /etc/nginx/memd.conf on vmhost.",
             "tags": [], "importance": 3}]})))
        assert inbox.main(["distill", "--dry-run", str(transcript)]) == 0
    assert "would file: Proxy config" in capsys.readouterr().out
    assert inbox.list_candidates(store, "amber")["total"] == 0


def test_settings_parse_conservatively(monkeypatch):
    assert inbox.save_mode({}) == "direct" and inbox.save_mode({"MEMD_SAVE_MODE": "INBOX"}) == "inbox"
    assert inbox.save_mode({"MEMD_SAVE_MODE": "maybe"}) == "direct"
    assert inbox.token_review_mode({}) == "off"
    assert inbox.token_review_mode({"MEMD_INBOX_TOKEN_REVIEW": "yes"}) == "off"
    assert inbox.token_review_mode({"MEMD_INBOX_TOKEN_REVIEW": "others"}) == "others"
    monkeypatch.setenv("MEMD_SAVE_MODE", "inbox")
    assert inbox.save_routes_to_inbox() is False      # a local caller always saves directly


# --------------------------------------------------------------------------- review hardening


def test_token_reviewer_may_change_only_tags_and_importance(store, monkeypatch):
    """With "others", one token must not rewrite another's candidate and approve it alone."""
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "others")
    cid = inbox.propose(dict(FACT), "amber", cfg=store, proposer="agent-a")["id"]
    for edits in ({"body": "Written only by agent-b."}, {"slug": "repo-hosting-policy"},
                  {"supersedes": "repo-hosting-policy"}):
        with pytest.raises(PermissionError, match="only importance and tags"):
            inbox.approve(store, "amber", cid, reviewer="agent-b", reviewer_is_token=True, edits=edits)
    assert inbox.get(store, "amber", cid)["status"] == "pending"
    # Resending the proposed values unchanged is not a rewrite.
    out = inbox.approve(store, "amber", cid, reviewer="agent-b", reviewer_is_token=True,
                        edits={"title": FACT["title"], "importance": 2, "tags": ["backup", "nightly"]})
    note = read_note(store.clone, out["receipt"]["slug"])
    assert note.saved_by == "agent-a" and note.importance == 2 and note.body == FACT["body"]


def test_a_token_rewrite_under_all_is_credited_to_the_token(store, monkeypatch):
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "all")
    cid = inbox.propose(dict(FACT), "amber", cfg=store, proposer="agent-a")["id"]
    out = inbox.approve(store, "amber", cid, reviewer="agent-b", reviewer_is_token=True,
                        edits={"body": "Backups moved to 03:00."})
    assert read_note(store.clone, out["receipt"]["slug"]).saved_by == "agent-b"


def test_provenance_is_part_of_candidate_identity(store):
    plain = inbox.propose(dict(FACT), "amber", cfg=store, proposer="agent-a")
    source = {"store": "cobalt", "slug": "backup-window", "revision": "a" * 40, "by": "b", "at": "2026-09-28"}
    published = inbox.propose(dict(FACT), "amber", cfg=store, proposer="agent-b", provenance=source)
    assert not published["duplicate"] and published["id"] != plain["id"]
    again = inbox.propose(dict(FACT), "amber", cfg=store, provenance=dict(source))
    assert again["duplicate"] and again["id"] == published["id"]
    other = inbox.propose(dict(FACT), "amber", cfg=store, provenance={**source, "store": "team"})
    assert not other["duplicate"]


def test_redaction_covers_pgp_blocks_and_quoted_multiword_secrets():
    pgp = ("-----BEGIN PGP " + "PRIVATE KEY BLOCK-----\nlQOYBGVx\nshort\n=AbCd\n"
           "-----END PGP " + "PRIVATE KEY BLOCK-----")
    out = inbox.redact(f"key: {pgp} and password: \"correct horse battery\" and api_key='two words here'")
    assert "lQOYBGVx" not in out and "=AbCd" not in out and "[REDACTED PRIVATE KEY]" in out
    assert "horse" not in out and "two words" not in out
    assert 'password: "[REDACTED]"' in out
