"""Review inbox (memd.inbox): proposals wait for a person before they are written.

Hermetic: temp Git/SQLite stores, a respx-mocked chat endpoint, no model
backends; the socket guard blocks everything else.
"""
import asyncio
import contextvars
import dataclasses
import json
import os
import sqlite3
import stat
import subprocess
import types
import uuid

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import memd.mcp as mcp_mod
import memd.save as save_mod
import memd.server as server_mod
from memd import access, actor, control, inbox, recall as recall_mod, registry, share
from memd.config import Config
from memd.identity import bind_identity
from memd.store import list_notes, read_note
from tests.test_share import _agent, team  # noqa: F401  (fixture)

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


def _as_request(fn, *, label="", principal=None, registry_token=None, identity=None):
    """Run fn in its own context, bound the way a remote request binds its caller."""
    def run():
        actor.set_actor(label)
        access.remote_request.set(True)
        access.current.set(principal)
        access.delegated.set(principal is None)
        registry.principal.set(registry_token)
        if identity:
            bind_identity(identity)
        return fn()
    return contextvars.copy_context().run(run)


def _token_reviewer(label, profile="amber"):
    """What reviewer_for gives a bearer that has this label and no recorded credential."""
    return _as_request(lambda: inbox.reviewer_for(profile), label=label)


def _stored_ids(cfg, cid, profile="amber"):
    """(proposer_account, proposer_credential, reviewer_account) as the inbox file holds them."""
    conn = sqlite3.connect(inbox.inbox_path(cfg, profile))
    try:
        return conn.execute("SELECT proposer_account, proposer_credential, reviewer_account "
                            "FROM candidates WHERE id=?", (cid,)).fetchone()
    finally:
        conn.close()


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
    out = inbox.approve(store, "amber", cid, reviewer=inbox.operator_reviewer("owner"),
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
        inbox.approve(store, "amber", cid, reviewer=inbox.operator_reviewer("owner"))
    with pytest.raises(inbox.NotPending):
        inbox.reject(store, "amber", cid, reviewer=inbox.operator_reviewer("owner"))
    with pytest.raises(ValueError):
        inbox.approve(store, "amber", inbox.propose({"body": "another fact entirely"}, "amber", cfg=store)["id"],
                      reviewer=inbox.operator_reviewer("owner"), edits={"saved_by": "forged"})


def test_failed_approval_stays_pending_with_error_and_reject_writes_nothing(store):
    cid = inbox.propose({"title": "Retire", "body": "Replace a missing note.", "supersedes": "no-such-note"},
                        "amber", cfg=store)["id"]
    assert "supersedes" in inbox.get(store, "amber", cid)["lint"]["error"]
    with pytest.raises(ValueError):
        inbox.approve(store, "amber", cid, reviewer=inbox.operator_reviewer("owner"))
    item = inbox.get(store, "amber", cid)
    assert item["status"] == "pending" and "supersedes" in item["last_error"]
    before = head(store.clone)
    out = inbox.reject(store, "amber", cid, reviewer=inbox.operator_reviewer("owner"), reason="wrong target")
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
    assert inbox.save_routes_to_inbox("amber") is False      # a local caller always saves directly


# --------------------------------------------------------------------------- review hardening


def test_token_reviewer_may_change_only_tags_and_importance(store, monkeypatch):
    """With "others", one token must not rewrite another's candidate and approve it alone."""
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "others")
    cid = inbox.propose(dict(FACT), "amber", cfg=store, proposer="agent-a")["id"]
    for edits in ({"body": "Written only by agent-b."}, {"slug": "repo-hosting-policy"},
                  {"supersedes": "repo-hosting-policy"}):
        with pytest.raises(PermissionError, match="only importance and tags"):
            inbox.approve(store, "amber", cid, reviewer=_token_reviewer("agent-b"), edits=edits)
    assert inbox.get(store, "amber", cid)["status"] == "pending"
    # Resending the proposed values unchanged is not a rewrite.
    out = inbox.approve(store, "amber", cid, reviewer=_token_reviewer("agent-b"),
                        edits={"title": FACT["title"], "importance": 2, "tags": ["backup", "nightly"]})
    note = read_note(store.clone, out["receipt"]["slug"])
    assert note.saved_by == "agent-a" and note.importance == 2 and note.body == FACT["body"]


def test_a_token_rewrite_under_all_is_credited_to_the_token(store, monkeypatch):
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "all")
    cid = inbox.propose(dict(FACT), "amber", cfg=store, proposer="agent-a")["id"]
    out = inbox.approve(store, "amber", cid, reviewer=_token_reviewer("agent-b"),
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


# --------------------------------------------------------------------------- caller ids and reviewers


def test_caller_ids_follow_the_kind_of_credential():
    session = access.Principal("u-1", "amber", False, {"amber": "write"}, "amber", session=True, csrf="c")
    token = access.Principal("u-1", "laptop", False, {"amber": "write"}, "amber", token_id="t-9")
    digest = "0123456789ab" * 2     # a made-up digest, as a legacy principal carries
    legacy = access.Principal("legacy:" + digest[:12], "legacy", False, {"amber": "write"}, "amber",
                              token_id=digest)

    def who(**bound):
        return _as_request(actor.caller, **bound)
    assert who(label="amber", principal=session) == actor.Caller("amber", "u-1", "session")
    assert who(label="laptop", principal=token) == actor.Caller("laptop", "u-1", "t-9")
    assert who(label="legacy", principal=legacy) == actor.Caller("legacy", None, "legacy:0123456789ab")
    assert who(label="ci", registry_token="r-1") == actor.Caller("ci", None, "r-1")
    # A personal registry token is both: its bound identity and its own id.
    assert who(label="phone", registry_token="r-2", identity="Cobalt@Example.com") == \
        actor.Caller("phone", "identity:cobalt@example.com", "r-2")
    assert who(label="cobalt@example.com", identity="cobalt@example.com") == \
        actor.Caller("cobalt@example.com", "identity:cobalt@example.com", "identity:cobalt@example.com")
    assert who(label="open") == actor.Caller("open", None, None)

    def local():
        actor.set_actor("mem-inbox distill")
        bind_identity("cobalt@example.com")     # a per-user store selected in process is not a credential
        return actor.caller()
    assert contextvars.copy_context().run(local) == actor.Caller("mem-inbox distill", None, None)


def test_candidates_record_a_legacy_token_and_leave_a_local_caller_without_ids(store):
    local = inbox.propose(dict(FACT), "amber", cfg=store, proposer="mem-import")["id"]
    assert _stored_ids(store, local) == (None, None, None)
    with TestClient(server_mod.create_token_app()) as client:
        over_http = client.post("/propose", headers=AUTH, json={"body": "The office printer is on the guest VLAN."})
        assert over_http.status_code == 200, over_http.text
        over_mcp = _mcp(client, "propose", {"content": "The archive NAS exports /srv/share over NFS v4."})
    credential = "legacy:" + control.digest(TOKEN)[:12]
    for cid in (over_http.json()["id"], over_mcp["structuredContent"]["id"]):
        assert _stored_ids(store, cid) == (None, credential, None)
        assert inbox.get(store, "amber", cid)["proposer"] == "legacy"


def test_a_payload_cannot_name_its_proposer(store):
    forged = {**FACT, "proposer": "someone-else", "proposer_account": "u-1", "proposer_credential": "t-1",
              "account": "u-1", "credential": "t-1", "caller": {"account": "u-1", "credential": "t-1"}}
    with TestClient(server_mod.create_token_app()) as client:
        r = client.post("/propose", headers=AUTH, json=forged)
    assert r.status_code == 200, r.text
    assert _stored_ids(store, r.json()["id"]) == (None, "legacy:" + control.digest(TOKEN)[:12], None)
    assert inbox.get(store, "amber", r.json()["id"])["proposer"] == "legacy"


def test_registry_tokens_are_told_apart_by_their_own_ids(store, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMD_CONTROL_DB", str(tmp_path / "control" / "control.db"))
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "others")
    tokens = registry.Registry()
    tokens.initialize()

    def issue():
        made = tokens.issue(actor="operator", operation_id=str(uuid.uuid4()), label="shared", owner="team",
                            purpose="review", stores=["amber"], operations=["save"])
        return {"Authorization": "Bearer " + made["secret"]}, made["token"]["id"]
    (first, first_id), (second, second_id) = issue(), issue()
    with TestClient(server_mod.create_token_app()) as client:
        mine = client.post("/propose", headers=first, json=FACT)
        assert mine.status_code == 200, mine.text
        theirs = _mcp(client, "propose", {"content": "The archive NAS exports /srv/share over NFS v4."},
                      headers={**MCP_HEADERS, **second})["structuredContent"]["id"]
        mine = mine.json()["id"]
        # A registry token has no account: its own id is the credential.
        assert _stored_ids(store, mine) == (None, first_id, None)
        assert _stored_ids(store, theirs) == (None, second_id, None)
        r = client.post(f"/inbox/{mine}/approve", headers=first, json={})
        assert r.status_code == 403 and "own proposal" in r.json()["detail"]
        assert client.post(f"/inbox/{mine}/approve", headers=second, json={}).status_code == 200
        assert client.post(f"/inbox/{theirs}/reject", headers=second, json={}).status_code == 403
    assert _stored_ids(store, mine) == (None, first_id, None)
    assert inbox.get(store, "amber", mine)["reviewer"] == "shared"


def test_a_queued_publish_records_who_published(team, capsys):
    admin = team["admin"]
    assert admin.put("/admin/stores/team", json={"config": {"publish_review": True}}).status_code == 200
    agent = _agent(team, {"team": "write"})
    with control.db() as c:
        owner = c.execute("SELECT id FROM users WHERE username='owner'").fetchone()[0]
        token_id = c.execute("SELECT id FROM tokens WHERE digest=?",
                             (control.digest(agent.headers["Authorization"][len("Bearer "):]),)).fetchone()[0]
    by_token = agent.post("/publish", json={"slug": "archive-backup-window", "target_store": "team"})
    by_session = admin.post("/publish", json={"slug": "repo-hosting-policy", "target_store": "team"})
    assert by_token.status_code == 202 and by_session.status_code == 202, (by_token.text, by_session.text)
    cfg = share._cfg("team")
    assert _stored_ids(cfg, by_token.json()["inbox_id"], "team") == (team["user"]["id"], token_id, None)
    assert _stored_ids(cfg, by_session.json()["inbox_id"], "team") == (owner, "session", None)
    # The local CLI is the operator: what it queues has no ids.
    assert share.main(["publish", "vmhost-proxmox-vm", "team", "--from", "amber", "--json"]) == 0
    by_cli = json.loads(capsys.readouterr().out)
    assert by_cli["queued"] and _stored_ids(cfg, by_cli["inbox_id"], "team") == (None, None, None)


def test_candidates_record_accounts_and_decisions_are_audited(accounts):
    client, app, csrf, agent, cfg = accounts
    api = TestClient(app)
    with control.db() as c:
        owner = c.execute("SELECT id FROM users WHERE username='owner'").fetchone()[0]
        token_id, member = c.execute("SELECT id, user_id FROM tokens WHERE label='laptop-agent'").fetchone()
    by_token = api.post("/propose", headers={"Authorization": "Bearer " + agent},
                        json={"body": "The garage door opener is on the IoT VLAN."})
    by_session = client.post("/propose", headers={"X-CSRF-Token": csrf},
                             json={"body": "The owner proposes this from the browser."})
    assert by_token.status_code == 200 and by_session.status_code == 200, (by_token.text, by_session.text)
    by_token, by_session = by_token.json()["id"], by_session.json()["id"]
    assert _stored_ids(cfg, by_token) == (member, token_id, None)
    assert _stored_ids(cfg, by_session) == (owner, "session", None)
    assert member != owner
    assert client.post(f"/inbox/{by_token}/approve", headers={"X-CSRF-Token": csrf}, json={}).status_code == 200
    assert client.post(f"/inbox/{by_session}/reject", headers={"X-CSRF-Token": csrf},
                       json={"reason": "not needed"}).status_code == 200
    # The decision keeps the reviewing account beside the reviewer label ...
    assert _stored_ids(cfg, by_token) == (member, token_id, owner)
    assert _stored_ids(cfg, by_session) == (owner, "session", owner)
    assert client.get(f"/inbox/{by_token}").json()["reviewer"] == "owner"
    # ... and is in the audit log. A refused or repeated decision is not.
    assert client.post(f"/inbox/{by_token}/approve", headers={"X-CSRF-Token": csrf}, json={}).status_code == 409
    with control.db() as c:
        rows = [tuple(r) for r in c.execute("SELECT actor, action, target FROM audit "
                                            "WHERE action LIKE 'inbox.%' ORDER BY id")]
    assert rows == [("owner", "inbox.approve", f"amber:{by_token}"), ("owner", "inbox.reject", f"amber:{by_session}")]


def test_two_tokens_with_one_label_are_different_proposers(accounts, monkeypatch):
    client, app, csrf, _, cfg = accounts
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "others")
    with control.db() as c:
        member = c.execute("SELECT id FROM users WHERE username='agent-user'").fetchone()[0]

    def issue():
        r = client.post("/admin/tokens", headers={"X-CSRF-Token": csrf},
                        json={"label": "shared", "user_id": member, "store_id": "amber", "scope": "write"})
        assert r.status_code == 200, r.text
        return {"Authorization": "Bearer " + r.json()["token"]}, r.json()["id"]
    (first, first_id), (second, second_id) = issue(), issue()
    api = TestClient(app)
    mine = api.post("/propose", headers=first, json={"body": "Proposed by the first token."}).json()["id"]
    theirs = api.post("/propose", headers=second, json={"body": "Proposed by the second token."}).json()["id"]
    assert [i["proposer"] for i in api.get("/inbox", headers=first).json()["items"]] == ["shared", "shared"]
    assert _stored_ids(cfg, mine)[:2] == (member, first_id) and _stored_ids(cfg, theirs)[:2] == (member, second_id)
    for headers, own in ((first, mine), (second, theirs)):
        for decision in ("approve", "reject"):
            r = api.post(f"/inbox/{own}/{decision}", headers=headers, json={})
            assert r.status_code == 403 and "own proposal" in r.json()["detail"], (decision, r.text)
    # The label would have refused these; the credentials differ, so they pass.
    assert api.post(f"/inbox/{mine}/approve", headers=second, json={}).status_code == 200
    assert api.post(f"/inbox/{theirs}/reject", headers=first, json={}).status_code == 200
    assert read_note(cfg.clone, api.get(f"/inbox/{mine}", headers=first).json()["slug"]).saved_by == "shared"


def test_a_candidate_without_ids_is_matched_by_label(store, monkeypatch):
    """Filed by a local caller, or before ids were recorded: the label is all there is."""
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "others")
    same = inbox.propose(dict(FACT), "amber", cfg=store, proposer="legacy")["id"]
    other = inbox.propose({"body": "The office printer is on the guest VLAN."}, "amber", cfg=store,
                          proposer="mem-import")["id"]
    assert _stored_ids(store, same) == (None, None, None)
    with TestClient(server_mod.create_token_app()) as client:
        for decision in ("approve", "reject"):
            r = client.post(f"/inbox/{same}/{decision}", headers=AUTH, json={})
            assert r.status_code == 403 and "own proposal" in r.json()["detail"], (decision, r.text)
        assert client.post(f"/inbox/{other}/approve", headers=AUTH, json={}).status_code == 200
    assert inbox.get(store, "amber", same)["status"] == "pending"


def test_an_inbox_file_from_before_ids_gains_the_columns_and_keeps_its_rows(store, monkeypatch):
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "others")
    old_id = "0123456789abcdef"
    conn = sqlite3.connect(inbox.inbox_path(store, "amber"))
    conn.executescript(inbox._SCHEMA)
    conn.execute("ALTER TABLE candidates ADD COLUMN provenance TEXT")
    conn.execute("INSERT INTO candidates(id,created,source,proposer,title,body,content_hash) "
                 "VALUES(?,1.0,'agent','agent-a','Old','Filed before ids were recorded.','h')", (old_id,))
    conn.commit()
    conn.close()
    assert inbox.get(store, "amber", old_id)["proposer"] == "agent-a"
    assert _stored_ids(store, old_id) == (None, None, None)
    with pytest.raises(PermissionError, match="own proposal"):
        inbox.reject(store, "amber", old_id, reviewer=_token_reviewer("agent-a"))
    assert inbox.reject(store, "amber", old_id, reviewer=_token_reviewer("agent-b"))["status"] == "rejected"


def test_two_openers_of_an_older_inbox_file_both_succeed(store, monkeypatch):
    """Both read the old column list; the slower one must not fail on a column the other added."""
    path = inbox.inbox_path(store, "amber")
    conn = sqlite3.connect(path)
    conn.executescript(inbox._SCHEMA)
    conn.close()
    connect, raced = sqlite3.connect, []

    class Slower(sqlite3.Connection):
        def execute(self, sql, *args):
            cursor = super().execute(sql, *args)
            if raced or not sql.startswith("PRAGMA table_info"):
                return cursor
            raced.append(True)
            seen = cursor.fetchall()
            inbox._connect(path).close()        # the other opener adds the columns first
            return seen
    with monkeypatch.context() as patched:
        patched.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, **kwargs, factory=Slower))
        assert inbox.list_candidates(store, "amber")["total"] == 0 and raced
    conn = sqlite3.connect(path)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(candidates)")]
    conn.close()
    assert sorted(columns[-4:]) == ["proposer_account", "proposer_credential", "provenance", "reviewer_account"]
    assert len(columns) == len(set(columns))


def test_decisions_need_a_reviewer_the_inbox_built(store):
    cid = inbox.propose(dict(FACT), "amber", cfg=store, proposer="agent-a")["id"]
    built = inbox.operator_reviewer("owner")

    class Lookalike(inbox.Reviewer):
        pass
    subclass = Lookalike("owner", operator=True)
    object.__setattr__(subclass, "_built", built._built)
    forged = ["owner", None, ("owner", False), inbox.Reviewer("owner", operator=True),
              dataclasses.replace(built, label="someone-else"), subclass,
              types.SimpleNamespace(label="owner", account=None, credential=None, is_token=False,
                                    can_write=True, operator=True, _built=built._built)]
    for reviewer in forged:
        with pytest.raises(PermissionError, match="reviewer the inbox built"):
            inbox.approve(store, "amber", cid, reviewer=reviewer)
        with pytest.raises(PermissionError, match="reviewer the inbox built"):
            inbox.reject(store, "amber", cid, reviewer=reviewer)
    assert inbox.get(store, "amber", cid)["status"] == "pending"
    assert read_note(store.clone, "backup-window") is None
    assert inbox.reject(store, "amber", cid, reviewer=built)["status"] == "rejected"
    # A remote request is never the operator, and the console reviews only its own store.
    with pytest.raises(PermissionError):
        _as_request(lambda: inbox.operator_reviewer("owner"), label="legacy")
    with pytest.raises(PermissionError):
        _as_request(lambda: inbox.console_reviewer("cobalt@example.com"), identity="amber@example.com")
    console = _as_request(lambda: inbox.console_reviewer("amber@example.com"), identity="amber@example.com")
    assert (console.label, console.account, console.is_token, console.operator) == \
        ("amber@example.com", "identity:amber@example.com", False, False)


def test_reviewer_for_describes_sessions_tokens_and_the_operator(store, monkeypatch):
    session = access.Principal("u-1", "amber", False, {"amber": "write"}, "amber", session=True, csrf="c",
                               reviews=frozenset({"amber"}))
    reader = dataclasses.replace(session, grants={"amber": "read"}, reviews=frozenset())
    token = access.Principal("u-2", "laptop", False, {"amber": "write"}, "amber", token_id="t-9")
    made = _as_request(lambda: inbox.reviewer_for("amber"), label="amber", principal=session)
    assert made == inbox.Reviewer("amber", "u-1", "session", is_token=False, can_write=True, operator=False,
                                  store="amber")
    with pytest.raises(PermissionError, match="review access"):
        _as_request(lambda: inbox.reviewer_for("amber"), label="amber", principal=reader)
    with pytest.raises(PermissionError, match="Agent tokens cannot review"):
        _as_request(lambda: inbox.reviewer_for("amber"), label="laptop", principal=token)
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "others")
    made = _as_request(lambda: inbox.reviewer_for("amber"), label="laptop", principal=token)
    assert made == inbox.Reviewer("laptop", "u-2", "t-9", is_token=True, can_write=True, operator=False,
                                  store="amber")
    actor.set_actor("")
    assert inbox.reviewer_for("amber") == inbox.Reviewer("local", operator=True)


def test_decisions_are_audited_only_when_administration_is_enabled(store, tmp_path, monkeypatch):
    operator = inbox.operator_reviewer("cli:amber")
    admin_db = tmp_path / "control" / "admin.db"
    first = inbox.propose(dict(FACT), "amber", cfg=store)["id"]
    assert inbox.reject(store, "amber", first, reviewer=operator)["status"] == "rejected"
    assert not admin_db.exists()
    # An administration database that cannot take the row never fails the decision.
    monkeypatch.setenv("MEMD_ADMIN_DB", str(admin_db))
    control.initialize()
    with control.db() as c:
        c.execute("DROP TABLE audit")
    second = inbox.propose({"body": "The office printer is on the guest VLAN."}, "amber", cfg=store)["id"]
    assert inbox.reject(store, "amber", second, reviewer=operator)["status"] == "rejected"
    control.initialize()
    third = inbox.propose({"body": "The archive NAS exports /srv/share over NFS v4."}, "amber", cfg=store)["id"]
    assert inbox.approve(store, "amber", third, reviewer=operator)["receipt"]["saved"] is True
    assert _stored_ids(store, third) == (None, None, None)       # the operator has no account
    with control.db() as c:
        rows = [tuple(r) for r in c.execute("SELECT actor, action, target FROM audit ORDER BY id")]
    assert rows == [("cli:amber", "inbox.approve", f"amber:{third}")]
