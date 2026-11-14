"""The proposals endpoints and the pending-duplicate lint, everywhere their neighbours are.

GET /proposals, GET /proposals/{id} and the ``proposals`` tool are listed and
counted where /propose and ``propose`` are: the endpoint index, /help and the
metrics name tables. ``mem-inbox`` prints a candidate's pending duplicates, the
propose tool's output schema describes them, and the personal onboarding page
says how to recall across several stores.

Hermetic: a temp Git/SQLite store behind one legacy token, no model backends;
the socket guard blocks everything else.
"""
import asyncio
import json
import re

from fastapi.testclient import TestClient

import memd.mcp as mcp_mod
import memd.server as server_mod
from memd import inbox, metrics
from tests.test_inbox import AUTH, MCP_HEADERS, store  # noqa: F401  (fixture)
from tests.test_metrics import parse, value

NO_SUCH_ID = "0" * 16
# One body under two titles: a reworded duplicate, not an identical one.
BODY = "Nightly backups of the archive volume run at 02:30 on lapbox and finish before 04:00 every morning."
FIRST = {"title": "Backup window", "body": BODY}
REWORDED = {"title": "Archive backup timing", "body": BODY}
UNRELATED = {"title": "Garage door", "body": "The garage door opener is on the IoT VLAN."}


# --------------------------------------------------------------------------- endpoint index and /help


def test_help_and_the_endpoint_index_list_the_proposals_endpoints():
    text = TestClient(server_mod.app).get("/help").text
    endpoints = text.split("ENDPOINTS", 1)[1].split("CLIENT DOWNLOADS", 1)[0]
    listed = [line.split()[:2] for line in endpoints.splitlines() if line.strip()]
    assert ["POST", "/propose"] in listed
    assert ["GET", "/proposals"] in listed and ["GET", "/proposals/ID"] in listed
    index = server_mod.__doc__
    assert "POST /propose " in index
    assert "GET  /proposals" in index and "/proposals/{id}" in index


# --------------------------------------------------------------------------- metrics


def test_metrics_name_the_proposals_route_and_tool(store):
    assert "proposals" in metrics.ROUTES and "proposals" in metrics.TOOLS
    assert metrics.route_for("/proposals", "GET") == "proposals"
    assert metrics.route_for(f"/proposals/{NO_SUCH_ID}", "GET") == "proposals"
    assert metrics.route_for("/propose", "POST") == "propose"
    metrics.reset()
    with TestClient(server_mod.create_token_app()) as client:
        assert client.get("/proposals", headers=AUTH).status_code == 200
        assert client.get(f"/proposals/{NO_SUCH_ID}", headers=AUTH).status_code == 404
        called = client.post("/mcp/", headers=MCP_HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "proposals", "arguments": {}}})
        assert called.status_code == 200 and called.json()["result"]["isError"] is False, called.text
        scraped = client.get("/metrics", headers=AUTH)
        assert scraped.status_code == 200, scraped.text
    metrics.reset()
    families = parse(scraped.text)
    assert value(families, "memd_http_requests_total", route="proposals", status="2xx") == 1
    assert value(families, "memd_http_requests_total", route="proposals", status="4xx") == 1
    assert value(families, "memd_http_request_duration_seconds_count", route="proposals") == 2
    assert value(families, "memd_mcp_tool_calls_total", tool="proposals", outcome="ok") == 1
    assert value(families, "memd_mcp_tool_duration_seconds_count", tool="proposals") == 1
    # Counted under their own names: nothing these requests did is in "other".
    for name in ("memd_http_requests_total", "memd_mcp_tool_calls_total"):
        for _, labels, _ in families[name]["samples"]:
            assert "other" not in labels.values(), (name, labels)


# --------------------------------------------------------------------------- mem-inbox


def test_mem_inbox_prints_pending_duplicates_only_where_there_are_some(store, capsys):
    first = inbox.propose(dict(FIRST), "amber", cfg=store, proposer="agent-a")["id"]
    reworded = inbox.propose(dict(REWORDED), "amber", cfg=store, proposer="agent-b")["id"]
    unrelated = inbox.propose(dict(UNRELATED), "amber", cfg=store, proposer="agent-a")["id"]
    match = {"id": first, "title": FIRST["title"]}
    assert inbox.get(store, "amber", reworded)["lint"]["pending_duplicates"] == [match]

    assert inbox.main(["list"]) == 0
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()[1:]}
    assert set(lines) == {first, reworded, unrelated}
    assert lines[reworded].endswith("]") and "pending-duplicate" in lines[reworded].rsplit("[", 1)[1]
    for cid, fact in ((first, FIRST), (unrelated, UNRELATED)):
        assert lines[cid].endswith(fact["title"]), lines[cid]          # no flags at all

    assert inbox.main(["show", reworded]) == 0
    assert f"  pending_duplicates: {json.dumps(match)}" in capsys.readouterr().out.splitlines()
    for cid in (first, unrelated):
        assert inbox.main(["show", cid]) == 0
        assert "pending_duplicates" not in capsys.readouterr().out

    # A candidate filed before the lint had the field prints what an empty list does.
    item = inbox.get(store, "amber", unrelated)
    inbox._print_candidate(item)
    with_empty_list = capsys.readouterr().out
    del item["lint"]["pending_duplicates"]
    inbox._print_candidate(item)
    assert capsys.readouterr().out == with_empty_list

    # --json is the stored lint, as before.
    assert inbox.main(["--json", "show", reworded]) == 0
    assert json.loads(capsys.readouterr().out)["lint"]["pending_duplicates"] == [match]


# --------------------------------------------------------------------------- propose's output schema


def test_propose_output_schema_describes_pending_duplicates():
    tools = {t.name: t for t in asyncio.run(mcp_mod.list_tools())}
    described = tools["propose"].output_schema["properties"]["lint"]["description"]
    assert "near_duplicates" in described and "pending_duplicates" in described
    assert "your own" in described and "never anyone else's" in described


# --------------------------------------------------------------------------- onboarding page


def test_personal_onboarding_page_says_how_to_recall_across_several_stores():
    from memd.admin_web import TEMPLATES
    html = TEMPLATES.get_template("onboarding.html").render(
        name="amber", csrf="csrf", is_admin=False, origin="https://memory.example.com")
    [note] = [p for p in re.findall(r"<p[^>]*>(.*?)</p>", html, re.S) if "--stores=" in p]
    assert "--stores=all" in note and "--stores=amber,team" in note
    assert "additional stores" in note and "Claude Code hooks" in note
    assert "<" not in note                                             # text only
    # The page still carries no inline script or style.
    assert "<style" not in html.lower()
    for tag in re.findall(r"<[a-zA-Z][^>]*>", html):
        assert not re.search(r"\sstyle\s*=", tag), tag
        assert not re.search(r"\son[a-z]+\s*=", tag), tag
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    assert scripts and all(not body.strip() for body in scripts)
