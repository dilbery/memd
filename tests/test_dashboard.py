"""Browser entry point, private note listing, and existing MCP dispatch."""
from pathlib import Path
import re

from fastapi.testclient import TestClient
import pytest

from memd import server, refresh
from memd.index import open_db

WEB = Path(server.__file__).resolve().parent / "web"


def test_dashboard_assets_and_generic_help():
    client = TestClient(server.app)
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "Memory index" in page.text and "Onboarding" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    for name in ("app.js", "style.css", "icon.svg", "health.js"):
        response = client.get("/ui/assets/" + name)
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/ui/assets/server.py").status_code == 404
    help_text = client.get("/help").text
    for personal in ("Amber", "Cobalt", "apphost", "gpuhost", "10.10.1.10"):
        assert personal not in page.text + help_text


def test_inbox_script_is_served_and_unknown_assets_are_not():
    client = TestClient(server.app)
    response = client.get("/ui/assets/inbox.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy and "unsafe-inline" not in policy
    assert response.text == (WEB / "inbox.js").read_text()
    # The allowlist is by exact name: nothing else in the folder, or beside it, is served.
    for name in ("inbox.css", "inbox.js.map", "inbox", "INBOX.JS", "index.html", "server.py", "..%2Fserver.py"):
        assert client.get("/ui/assets/" + name).status_code == 404, name


def test_index_has_the_inbox_tab_and_loads_its_script():
    client = TestClient(server.app)
    html = client.get("/").text
    scripts = re.findall(r'<script src="/ui/assets/([a-z]+\.js)" defer></script>', html)
    assert scripts == ["app.js", "admin.js", "health.js", "entities.js", "inbox.js"]
    # Hidden until a signed-in session holds review: a token unlock never shows it.
    assert '<a href="#inbox" id="tab-inbox" hidden>Inbox</a>' in html
    assert re.search(r'<section id="inbox-panel"[^>]* hidden>', html)
    for control in ('id="inbox-store"', 'id="inbox-status"', 'id="inbox-list"', 'id="inbox-view"',
                    'id="inbox-approve"', 'id="inbox-reject"', 'id="inbox-reject-reason"'):
        assert control in html, control
    # Everything the page references under /ui/assets/ is on the allowlist.
    for name in set(re.findall(r'(?:src|href)="/ui/assets/([^"]+)"', html)):
        assert client.get("/ui/assets/" + name).status_code == 200, name


def test_dashboard_has_no_inline_script_or_style():
    html = TestClient(server.app).get("/").text
    tags = re.findall(r"<[a-zA-Z][^>]*>", html)
    assert "<style" not in html.lower()
    for tag in tags:
        assert not re.search(r"\sstyle\s*=", tag), tag
        assert not re.search(r"\son[a-z]+\s*=", tag), tag            # onclick= and friends
        assert "javascript:" not in tag.lower(), tag
    opened = [tag for tag in tags if tag.lower().startswith("<script")]
    assert opened and all(re.fullmatch(r'<script src="/ui/assets/[a-z]+\.js" defer>', tag) for tag in opened)
    assert all(not body.strip() for body in re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.S))


def test_inbox_script_renders_text_only():
    source = (WEB / "inbox.js").read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                 "new Function", "setAttribute(\"on", "javascript:"):
        assert sink not in source, sink


def test_store_dialog_offers_the_review_choice():
    html = TestClient(server.app).get("/").text
    dialog = html[html.index('<dialog id="store-dialog"'):html.index('<dialog id="user-dialog"')]
    assert '<fieldset id="store-review"' in dialog and "<legend>Review</legend>" in dialog
    # One group of three radio buttons, the policy as its value, "No review" until a store says otherwise.
    radios = re.findall(r'<label class="check"><input id="([a-z-]+)" type="radio" name="([a-z-]+)" value="([a-z-]+)"( checked)?'
                        r' aria-describedby="([a-z-]+)">([^<]+)</label>', dialog)
    assert [(value, bool(checked), label) for _, _, value, checked, _, label in radios] == [
        ("open", True, "No review"), ("agents", False, "Agent saves need review"),
        ("four-eyes", False, "All saves need review by someone else")]
    assert {name for _, name, *_ in radios} == {"review-policy"}
    # Each option is described by its own line of help.
    for ident, _, value, _, described, _ in radios:
        assert (ident, described) == ("store-review-" + value, f"store-review-{value}-help")
        assert re.search(rf'<p id="{described}" class="small muted">[^<]{{30,}}</p>', dialog), described
    # What "four-eyes" means for this store is said in a live region, and only once it applies.
    assert re.search(r'<div id="store-review-notes" role="status"><p id="store-review-few" class="notice" hidden></p>'
                     r'<p id="store-review-unreviewed" class="notice" hidden>[^<]+</p></div>', dialog)
    # The inbox's notice, and the reason its text fields are read-only there, start hidden too.
    for ident in ("inbox-policy", "inbox-text-policy"):
        assert re.search(rf'<p id="{ident}" class="[a-z ]+" hidden>[^<]+</p>', html), ident


def test_page_scripts_name_the_review_policy_as_the_api_does():
    scripts = {name: (WEB / name).read_text() for name in ("app.js", "admin.js", "inbox.js")}
    # The key PUT /admin/stores takes, the fields the overview, /ui/me and /ui/stores report, and the three values.
    assert "config.review_policy = " in scripts["admin.js"] and ".reviewers" in scripts["admin.js"]
    assert ".review_policy" in scripts["app.js"]
    for value in ('"open"', '"agents"', '"four-eyes"'):
        assert value in scripts["app.js"], value
    # A store's card names its policy in the words of the dialog's option.
    html = (WEB / "index.html").read_text()
    for label in ("Agent saves need review", "All saves need review by someone else"):
        assert f'"{label}"' in scripts["admin.js"] and f">{label}</label>" in html, label
    # What a server says about a store or a candidate is set as text.
    for name, source in scripts.items():
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                     "new Function", "setAttribute(\"on", "javascript:"):
            assert sink not in source, (name, sink)


@pytest.fixture
def reviewer(config, tmp_path, monkeypatch):
    """An administrator's browser session (it reviews every store) and an agent token that proposes."""
    from memd import control, save, sources
    monkeypatch.setenv("MEMD_ADMIN_DB", str(tmp_path / "control" / "admin.db"))
    monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "1")
    monkeypatch.setenv("MEMD_LOCAL_HOST", "any")
    monkeypatch.setattr(sources._jobs, "submit", lambda fn, *args: fn(*args))
    monkeypatch.setattr(save, "_pull_rebase_push", lambda clone: None)
    monkeypatch.setattr(save, "_vector_near_matches", lambda cfg, body: [])
    control.initialize()
    control.register_existing()
    owner = control.create_user("owner", "test-password-long-enough", "admin")
    refresh.ensure_lexical(config)
    app = server.create_token_app()
    with TestClient(app) as client:
        login = client.post("/ui/login", json={"username": "owner", "password": "test-password-long-enough"})
        assert login.status_code == 200
        client.headers["X-CSRF-Token"] = login.json()["csrf"]
        issued = client.post("/admin/tokens", json={"label": "laptop-agent", "user_id": owner,
                                                    "store_id": "amber", "scope": "write"})
        assert issued.status_code == 200, issued.text
        yield client, TestClient(app), {"Authorization": "Bearer " + issued.json()["token"]}


def test_inbox_tab_requests_return_what_the_script_reads(reviewer):
    """The calls memd/web/inbox.js makes as a signed-in reviewer, and the fields it renders."""
    client, agent, token = reviewer
    keep, drop = (agent.post("/propose", headers=token, json=fact).json()["id"] for fact in (
        {"title": "Restore drill <b>steps</b>", "body": "Restore the newest verified backup.", "tags": ["backup"]},
        {"title": "Scratch observation", "body": "A passing remark."}))
    me = client.get("/ui/me").json()
    assert me["session"] is True and me["csrf"]
    assert [{"id", "name", "permission"} <= set(store) for store in me["stores"]] == [True]
    listed = client.get("/inbox?profile=amber&status=pending&offset=0&limit=20").json()
    assert listed["total"] == 2 and listed["counts"] == {"pending": 2, "approved": 0, "rejected": 0}
    first = listed["items"][0]
    assert {"id", "status", "created", "source", "proposer", "title", "meta", "lint", "reviewer",
            "decided_at", "reason", "slug", "last_error", "published_from", "excerpt"} <= set(first)
    assert (first["id"], first["proposer"], first["source"]) == (keep, "laptop-agent", "agent")
    assert first["title"] == "Restore drill <b>steps</b>" and first["excerpt"] == "Restore the newest verified backup."
    assert {"slug", "action", "related", "near_duplicates", "conflicts", "warnings", "error"} <= set(first["lint"])
    full = client.get(f"/inbox/{keep}?profile=amber").json()
    assert full["body"] == "Restore the newest verified backup." and full["meta"] == {"tags": ["backup"]}
    # Approve with the one changed field, as the form sends it.
    approved = client.post(f"/inbox/{keep}/approve", json={"profile": "amber", "edits": {"tags": ["backup", "drill"]}})
    assert approved.status_code == 200, approved.text
    receipt = approved.json()["receipt"]
    assert approved.json()["edited"] is True and receipt["slug"] and "synced" in receipt
    rejected = client.post(f"/inbox/{drop}/reject", json={"profile": "amber", "reason": "Not durable"})
    assert rejected.status_code == 200, rejected.text
    # A second decision is a 409 whose "detail" is a sentence: the page shows it as it is.
    again = client.post(f"/inbox/{drop}/approve", json={"profile": "amber"})
    assert again.status_code == 409 and "rejected" in again.json()["detail"]
    decided = client.get(f"/inbox/{drop}?profile=amber").json()
    assert (decided["status"], decided["reviewer"], decided["reason"]) == ("rejected", "owner", "Not durable")
    assert decided["decided_at"] >= decided["created"]
    saved = client.get(f"/inbox/{keep}?profile=amber").json()
    assert (saved["status"], saved["slug"], saved["meta"]["tags"]) == ("approved", receipt["slug"], ["backup", "drill"])
    assert saved["original"]["meta"] == {"tags": ["backup"]}
    by_status = client.get("/inbox?profile=amber&status=rejected&offset=0&limit=20").json()
    assert by_status["counts"] == {"pending": 0, "approved": 1, "rejected": 1} and by_status["items"][0]["id"] == drop
    # The note opens from the store the inbox names, as the "Saved as" link asks.
    note = client.post("/read", json={"slug": receipt["slug"], "profile": "amber", "limit": 16000})
    assert note.status_code == 200 and note.json()["body"] == "Restore the newest verified backup."
    # A refusal carries its reason in "detail": the token that proposed is not a reviewer.
    refused = agent.get("/inbox?profile=amber", headers=token)
    assert refused.status_code == 403 and isinstance(refused.json()["detail"], str) and refused.json()["detail"]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_notes_require_auth_before_opening_store(monkeypatch, headers):
    monkeypatch.setenv("MEMD_TOKEN", "test-token")
    monkeypatch.setattr(server, "open_db", lambda *a: pytest.fail("unauthorized database access"))
    assert TestClient(server.app).get("/ui/notes", headers=headers).status_code == 401


def test_notes_paginate_and_exclude_other_profiles_and_superseded(config, monkeypatch):
    refresh.ensure_lexical(config)
    db = open_db(config.db)
    db.execute("UPDATE notes SET superseded_by='replacement' WHERE slug='vmhost-proxmox-vm'")
    db.execute("INSERT INTO notes(slug,path,title,profile,body,git_blob) "
               "VALUES('private-other','private.md','Private','cobalt','secret','x')")
    db.commit(); db.close()
    client = TestClient(server.app)
    headers = {"Authorization": "Bearer " + config.token}
    first = client.get("/ui/notes?limit=1", headers=headers)
    second = client.get("/ui/notes?limit=1&offset=1", headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    a, b = first.json(), second.json()
    assert a["total"] == b["total"] == 2
    assert a["notes"][0]["slug"] != b["notes"][0]["slug"]
    assert {a["notes"][0]["slug"], b["notes"][0]["slug"]} == {"repo-hosting-policy", "gpuhost-inference-tuning"}
    assert client.get("/ui/notes?offset=999", headers=headers).json()["notes"] == []
    for query in ("offset=-1", "limit=0", "limit=101"):
        assert client.get("/ui/notes?" + query, headers=headers).status_code == 422
    monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "1")
    assert client.get("/ui/notes?profile=cobalt", headers=headers).status_code == 403


def test_root_mcp_and_legacy_path_still_dispatch(monkeypatch):
    monkeypatch.setenv("MEMD_TOKEN", "test-token")
    monkeypatch.setenv("MEMD_STARTUP_REFRESH", "0")
    with TestClient(server.create_token_app()) as client:
        for path in ("/", "/mcp/"):
            response = client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                   headers={"Authorization": "Bearer test-token",
                                            "Accept": "application/json, text/event-stream"})
            assert response.status_code == 200
            assert {t["name"] for t in response.json()["result"]["tools"]} >= {"recall", "read", "save"}
            assert client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
