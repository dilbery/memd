"""Feedback for the proposer: its own proposals, and the pending-duplicate lint.

GET /proposals, GET /proposals/{id} and the ``proposals`` tool show a caller the
candidates it filed and what became of them, and nothing anyone else filed. A
proposal also names the pending candidates it restates: all of them for
reviewers, only the proposer's own in its reply. Driven through REST and the
MCP tool surface.

Hermetic: temp Git/SQLite stores behind the administered access layer or one
legacy token, no model backends; the socket guard blocks everything else.
"""
import asyncio
import contextvars
import json
import tracemalloc
import uuid

import jsonschema
import pytest
from fastapi.testclient import TestClient

import memd.hooks.handoff as handoff_hook
import memd.mcp as mcp_mod
import memd.save as save_mod
import memd.server as server_mod
from memd import actor, control, inbox, registry
from memd.store import read_note
from tests.test_inbox import AUTH, FACT, TOKEN, accounts, store  # noqa: F401  (fixtures)
from tests.test_personal_tokens import directory  # noqa: F401  (fixture)
from tests.test_share import OTHER_SECRET, _agent, team  # noqa: F401  (fixture)
from tests.test_user_memory import stores  # noqa: F401  (fixture)
from tests.test_user_web import userweb  # noqa: F401  (fixture)

ACCEPT = {"Accept": "application/json, text/event-stream"}
ENTRY = {"id", "status", "created", "source", "title", "excerpt", "decided_at", "reviewer", "reason",
         "slug", "revision", "edited", "last_error"}
NO_SUCH_ID = "0" * 16
# One body under several titles: each pair is a reworded duplicate, none an identical one.
BODY = "Nightly backups of the archive volume run at 02:30 on lapbox and finish before 04:00 every morning."
FIRST = {"title": "Backup window", "body": BODY}
REWORDED = {"title": "Archive backup timing", "body": BODY}
AGAIN = {"title": "Nightly backup schedule", "body": BODY}
UNRELATED = {"title": "Garage door", "body": "The garage door opener is on the IoT VLAN."}


def _tool(client, arguments, headers=None, name="proposals"):
    r = client.post("/mcp/", headers={**(headers or {}), **ACCEPT}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _user(username):
    with control.db() as c:
        return c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]


def _issue(client, csrf, user_id, label, scope="write"):
    r = client.post("/admin/tokens", headers={"X-CSRF-Token": csrf},
                    json={"label": label, "user_id": user_id, "store_id": "amber", "scope": scope})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mine(client, headers=None, **params):
    """Ids of every candidate GET /proposals lists for this caller."""
    r = client.get("/proposals", headers=headers, params={"status": "all", **params})
    assert r.status_code == 200, r.text
    return {i["id"] for i in r.json()["items"]}


def _propose(client, fact, headers=None):
    r = client.post("/propose", headers=headers, json=fact)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- own proposals


def test_a_token_follows_its_proposals_and_reads_why_one_was_rejected(accounts):
    client, app, csrf, token, cfg = accounts
    api, agent, review = TestClient(app), {"Authorization": "Bearer " + token}, {"X-CSRF-Token": csrf}
    waiting = _propose(api, FACT, agent)["id"]
    kept = _propose(api, UNRELATED, agent)["id"]
    refused = _tool(api, {"title": "Moon", "body": "The moon is made of cheese."}, agent,
                    name="propose")["structuredContent"]["id"]
    r = client.post(f"/inbox/{kept}/approve", headers=review,
                    json={"edits": {"body": "The garage door opener is on the IoT VLAN (VLAN 30)."}})
    assert r.status_code == 200, r.text
    r = client.post(f"/inbox/{refused}/reject", headers=review, json={"reason": "Not a fact about this network."})
    assert r.status_code == 200, r.text

    assert api.get("/proposals").status_code == 401
    listed = api.get("/proposals", headers=agent).json()       # pending is the default
    assert listed["ok"] and listed["profile"] == "amber" and listed["total"] == 1 and listed["offset"] == 0
    assert listed["counts"] == {"pending": 1, "approved": 1, "rejected": 1}
    [item] = listed["items"]
    assert set(item) == ENTRY
    assert (item["id"], item["status"], item["source"], item["title"]) == (waiting, "pending", "agent", FACT["title"])
    assert item["excerpt"] == FACT["body"] and item["created"] > 0 and item["edited"] is False
    assert all(item[k] is None for k in ("decided_at", "reviewer", "reason", "slug", "revision", "last_error"))

    everything = api.get("/proposals", headers=agent, params={"status": "all"}).json()
    assert everything["total"] == 3
    by_id = {i["id"]: i for i in everything["items"]}
    approved, rejected = by_id[kept], by_id[refused]
    assert (approved["status"], approved["reviewer"], approved["edited"]) == ("approved", "owner", True)
    assert approved["decided_at"] >= approved["created"] and approved["reason"] is None
    note = read_note(cfg.clone, approved["slug"])
    assert note.body.endswith("(VLAN 30).") and approved["revision"] == note.git_blob
    assert (rejected["status"], rejected["reviewer"], rejected["reason"]) == \
        ("rejected", "owner", "Not a fact about this network.")
    assert rejected["slug"] is None and rejected["revision"] is None and rejected["edited"] is False
    assert [i["id"] for i in api.get("/proposals", headers=agent, params={"status": "approved"}).json()["items"]] \
        == [kept]
    page = api.get("/proposals", headers=agent, params={"status": "all", "limit": 1, "offset": 1}).json()
    assert len(page["items"]) == 1 and page["total"] == 3 and page["offset"] == 1

    # One candidate by id adds the full text.
    one = api.get(f"/proposals/{refused}", headers=agent)
    assert one.status_code == 200, one.text
    assert set(one.json()) == ENTRY | {"body"}
    assert one.json()["body"] == "The moon is made of cheese." and one.json()["reason"].startswith("Not a fact")
    assert api.get(f"/proposals/{kept}", headers=agent).json()["body"].endswith("(VLAN 30).")

    # The tool gives the same answers.
    out = _tool(api, {"status": "rejected"}, agent)
    assert out["isError"] is False, out
    [told] = out["structuredContent"]["items"]
    assert told == rejected and out["structuredContent"]["counts"] == listed["counts"]
    assert out["structuredContent"]["profile"] == "amber"
    assert [i["id"] for i in _tool(api, {}, agent)["structuredContent"]["items"]] == [waiting]
    out = _tool(api, {"id": refused}, agent)
    assert out["isError"] is False and out["structuredContent"]["proposal"] == one.json()
    paged = _tool(api, {"status": "all", "limit": "2"}, agent)
    assert len(paged["structuredContent"]["items"]) == 2
    # A client that honours the advertised output schema must accept every reply.
    schema = next(t for t in asyncio.run(mcp_mod.list_tools()) if t.name == "proposals").output_schema
    for reply in (out, paged, _tool(api, {"status": "all"}, agent), _tool(api, {"id": NO_SUCH_ID}, agent)):
        jsonschema.validate(instance=reply["structuredContent"], schema=schema)

    assert api.get("/proposals", headers=agent, params={"status": "bogus"}).status_code == 400
    assert api.get("/proposals/not-an-id", headers=agent).status_code == 400
    assert api.get(f"/proposals/{NO_SUCH_ID}", headers=agent).status_code == 404
    assert _tool(api, {"status": "bogus"}, agent)["isError"] is True


def test_another_credential_never_sees_a_proposal_even_of_the_same_account(accounts):
    client, app, csrf, token, cfg = accounts
    api, first, review = TestClient(app), {"Authorization": "Bearer " + token}, {"X-CSRF-Token": csrf}
    member, owner = _user("agent-user"), _user("owner")
    sibling = _issue(client, csrf, member, "desktop-agent")     # the same account, another credential
    twin = _issue(client, csrf, member, "laptop-agent")         # the same account and the same label
    owner_token = _issue(client, csrf, owner, "owner-agent")    # the signed-in reviewer's own account
    by_token = _propose(api, FACT, first)["id"]
    by_twin = _propose(api, UNRELATED, twin)["id"]
    by_session = _propose(client, {"body": "The owner proposes this from the browser."}, review)["id"]
    by_owner_token = _propose(api, {"body": "The owner's agent proposes this."}, owner_token)["id"]
    missing = api.get(f"/proposals/{NO_SUCH_ID}", headers=first)
    assert missing.status_code == 404
    told_missing = _tool(api, {"id": NO_SUCH_ID}, first)
    assert told_missing["isError"] is True
    assert told_missing["structuredContent"]["error"] == missing.json()["detail"] == \
        "Candidate not found among your proposals"

    def hidden(candidate_id, headers, http=api):
        """Not listed, and by id exactly what a candidate that does not exist gives."""
        r = http.get(f"/proposals/{candidate_id}", headers=headers)
        assert (r.status_code, r.json()) == (404, missing.json()), r.text
        assert FACT["body"] not in r.text
        if headers is not None:
            assert _tool(http, {"id": candidate_id}, headers) == told_missing
        return candidate_id not in _mine(http, headers)

    assert _mine(api, first) == {by_token} and _mine(api, twin) == {by_twin}
    assert _mine(api, sibling) == set()
    # Two tokens of one account, and two that also share a label.
    assert hidden(by_token, sibling) and hidden(by_token, twin) and hidden(by_twin, first)
    # A session and a token of one account are different credentials too.
    assert _mine(client) == {by_session} and _mine(api, owner_token) == {by_owner_token}
    assert hidden(by_owner_token, None, http=client) and hidden(by_session, owner_token)
    assert hidden(by_token, None, http=client) and hidden(by_session, first)
    # Every session carries the credential "session": the account tells two of them apart.
    password = client.post(f"/admin/users/{member}/reset-password", headers=review).json()["temporary_password"]
    browser = TestClient(app)
    login = browser.post("/ui/login", json={"username": "agent-user", "password": password})
    assert login.status_code == 200, login.text
    by_member_session = _propose(browser, {"body": "The member proposes this from the browser."},
                                 {"X-CSRF-Token": login.json()["csrf"]})["id"]
    assert _mine(browser) == {by_member_session} and _mine(client) == {by_session}
    assert hidden(by_session, None, http=browser) and hidden(by_member_session, None, http=client)
    assert hidden(by_token, None, http=browser) and hidden(by_member_session, first)
    # Counts and totals cover the caller's own candidates, never the store's.
    nothing = api.get("/proposals", headers=sibling, params={"status": "all"}).json()
    assert (nothing["items"], nothing["total"]) == ([], 0)
    assert nothing["counts"] == {"pending": 0, "approved": 0, "rejected": 0}
    out = _tool(api, {"status": "all"}, sibling)
    assert out["isError"] is False and out["structuredContent"]["items"] == []
    assert out["structuredContent"]["total"] == 0 and FACT["title"] not in json.dumps(out)
    # A decision does not change whose it is: only the proposer learns the reason.
    r = client.post(f"/inbox/{by_token}/reject", headers=review, json={"reason": "Already saved."})
    assert r.status_code == 200, r.text
    assert api.get(f"/proposals/{by_token}", headers=first).json()["reason"] == "Already saved."
    assert hidden(by_token, sibling) and hidden(by_token, twin)
    # The store's reviewer is the other reader of pending text, through the inbox.
    assert client.get(f"/inbox/{by_twin}").json()["body"] == UNRELATED["body"]


def test_a_local_caller_is_known_by_its_label_and_a_remote_one_never_is(store):
    older = inbox.propose(dict(FACT), "amber", cfg=store, proposer="legacy", now=1000.0)["id"]
    newer = inbox.propose(dict(UNRELATED), "amber", cfg=store, proposer="legacy", now=2000.0)["id"]
    distilled = inbox.propose({"body": "The archive NAS exports /srv/share over NFS v4."}, "amber", cfg=store,
                              proposer="mem-inbox distill")["id"]
    with TestClient(server_mod.create_token_app()) as client:
        remote = _propose(client, {"body": "The office printer is on the guest VLAN."}, AUTH)["id"]
        # The token's label is "legacy" as well, but it has a credential: only what it filed is its own.
        assert _mine(client, AUTH) == {remote}
        assert client.get(f"/proposals/{remote}", headers=AUTH).json()["body"].startswith("The office printer")
        for cid in (older, distilled):
            assert client.get(f"/proposals/{cid}", headers=AUTH).status_code == 404
        assert _tool(client, {"id": older}, {**AUTH, "Content-Type": "application/json"})["isError"] is True
    local = inbox.own_proposals(store, "amber", actor.Caller("legacy"), status="all")
    assert [i["id"] for i in local["items"]] == [newer, older]         # newest first
    assert local["counts"] == {"pending": 2, "approved": 0, "rejected": 0}
    assert inbox.own_proposal(store, "amber", actor.Caller("legacy"), older)["body"] == FACT["body"]
    for cid in (remote, distilled, NO_SUCH_ID):
        with pytest.raises(FileNotFoundError, match="^Candidate not found among your proposals$"):
            inbox.own_proposal(store, "amber", actor.Caller("legacy"), cid)
    # A caller with ids is never matched by label, whatever it is called.
    token = actor.Caller("legacy", None, "legacy:" + control.digest("some other token")[:12])
    assert inbox.own_proposals(store, "amber", token, status="all")["items"] == []

    def as_local(label, arguments):
        """The stdio tool surface: no request, so the label is the identity."""
        def run():
            actor.set_actor(label)
            return asyncio.run(mcp_mod.call_tool("proposals", arguments))
        return contextvars.copy_context().run(run)
    out = as_local("mem-inbox distill", {"status": "all"})
    assert not out.is_error, out
    assert [i["id"] for i in out.structured_content["items"]] == [distilled]
    assert as_local("mem-inbox distill", {"id": distilled}).structured_content["proposal"]["id"] == distilled
    assert as_local("someone-else", {"id": distilled}).is_error
    assert as_local("someone-else", {"status": "all"}).structured_content["items"] == []


def test_own_proposals_need_read_access_to_the_store_and_nothing_more(team):
    admin = team["admin"]
    secret = _propose(admin, {"profile": "other", "title": "Rotation plan",
                              "body": f"{OTHER_SECRET} rotation plan"})["id"]
    assert _mine(admin, profile="other") == {secret}

    # A read-only token cannot propose, so it has nothing to list; asking is not refused.
    reader = _agent(team, scope="read")
    assert reader.post("/propose", json=FACT).status_code == 403
    listed = reader.get("/proposals", params={"status": "all"})
    assert listed.status_code == 200, listed.text
    assert (listed.json()["items"], listed.json()["total"], listed.json()["profile"]) == ([], 0, "amber")
    out = _tool(reader, {"status": "all"})
    assert out["isError"] is False and out["structuredContent"]["items"] == []
    assert reader.get(f"/proposals/{NO_SUCH_ID}").status_code == 404

    # A store the token does not reach is refused before anything in it is read.
    writer = _agent(team)
    for name in ("other", "team"):
        for r in (writer.get("/proposals", params={"profile": name, "status": "all"}),
                  writer.get(f"/proposals/{secret}", params={"profile": name})):
            assert r.status_code == 403, (name, r.text)
            assert OTHER_SECRET not in r.text and "Rotation plan" not in r.text
        for arguments in ({"store": name, "status": "all"}, {"store": name, "id": secret},
                          {"profile": name, "status": "all"}):
            out = _tool(writer, arguments)
            assert out["isError"] is True, (name, out)
            assert OTHER_SECRET not in json.dumps(out) and "Rotation plan" not in json.dumps(out)
    assert writer.get(f"/proposals/{secret}").status_code == 404        # its own store has no such candidate

    # Proposals are listed per store, for a store the token does reach.
    both = _agent(team, {"team": "write"})
    shared = _propose(both, {"profile": "team", "title": "Team printer",
                             "body": "The team printer is on the guest VLAN."})["id"]
    assert _mine(both, profile="team") == {shared} and _mine(both) == set()
    out = _tool(both, {"store": "team"})
    assert out["isError"] is False, out
    assert [i["id"] for i in out["structuredContent"]["items"]] == [shared]
    assert out["structuredContent"]["profile"] == "team"
    assert _tool(both, {"store": "team", "id": shared})["structuredContent"]["proposal"]["title"] == "Team printer"
    assert both.get(f"/proposals/{shared}").status_code == 404
    assert _tool(both, {"store": "team", "profile": "amber"})["isError"] is True


# Every caller memd.actor.caller describes (tests/test_inbox.py pins that table). All
# but the last share one label, so only the ids tell them apart: both of them, NULLs
# included. A local caller has none and is its label.
CALLERS = {
    "a session": actor.Caller("amber", "u-1", "session"),
    "another account's session": actor.Caller("amber", "u-2", "session"),
    "a token": actor.Caller("amber", "u-1", "t-1"),
    "a second token of that account": actor.Caller("amber", "u-1", "t-2"),
    "a legacy token": actor.Caller("amber", None, "legacy:0123456789ab"),
    "another legacy token": actor.Caller("amber", None, "legacy:ba9876543210"),
    "a registry token": actor.Caller("amber", None, "r-1"),
    "a personal registry token": actor.Caller("amber", "identity:amber", "r-2"),
    "the identity it is bound to": actor.Caller("amber", "identity:amber", "identity:amber"),
    "a local caller": actor.Caller("amber"),
    "another local caller": actor.Caller("cobalt"),
}


def test_every_kind_of_caller_reads_back_only_what_it_filed(store):
    def file(who, title):
        return inbox.propose({"title": title, "body": BODY}, "amber", cfg=store, proposer=who.label,
                             caller=who if who.credential else None, precomputed_lint={"action": "create"})

    def named(replies):
        return [{"id": r["id"], "title": inbox.get(store, "amber", r["id"])["title"]} for r in replies]
    # Everyone restates everyone: the reply names nobody else, the stored lint all of them.
    first = {name: file(who, f"Backup note {n}") for n, (name, who) in enumerate(CALLERS.items())}
    assert not any(r["duplicate"] or r["lint"]["pending_duplicates"] for r in first.values())
    second = {name: file(who, f"Backup memo {n}") for n, (name, who) in enumerate(CALLERS.items())}
    for name, reply in second.items():
        assert reply["lint"]["pending_duplicates"] == named([first[name]]), name
    last = list(second.values())[-1]["id"]
    assert inbox.get(store, "amber", last)["lint"]["pending_duplicates"] == \
        named([*first.values(), *list(second.values())[:-1]])
    # A decision is the proposer's to read as well, and nobody else's.
    inbox.reject(store, "amber", first["a token"]["id"], reviewer=inbox.operator_reviewer("owner"),
                 reason="Said twice.")

    with pytest.raises(FileNotFoundError) as missing:
        inbox.own_proposal(store, "amber", CALLERS["a token"], NO_SUCH_ID)
    for name, who in CALLERS.items():
        own = {first[name]["id"], second[name]["id"]}
        listed = inbox.own_proposals(store, "amber", who, status="all")
        assert {i["id"] for i in listed["items"]} == own and listed["total"] == 2, name
        assert sum(listed["counts"].values()) == 2, name
        for cid in own:
            assert inbox.own_proposal(store, "amber", who, cid)["body"] == BODY
        for other in CALLERS:
            for cid in () if other == name else (first[other]["id"], second[other]["id"]):
                with pytest.raises(FileNotFoundError) as refused:
                    inbox.own_proposal(store, "amber", who, cid)
                assert str(refused.value) == str(missing.value), (name, other)
    assert inbox.own_proposal(store, "amber", CALLERS["a token"], first["a token"]["id"])["reason"] == "Said twice."


def test_registry_tokens_sharing_a_label_read_only_their_own_proposals(store, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMD_CONTROL_DB", str(tmp_path / "control" / "control.db"))
    tokens = registry.Registry()
    tokens.initialize()

    def issue(*operations):
        made = tokens.issue(actor="operator", operation_id=str(uuid.uuid4()), label="shared", owner="team",
                            purpose="agents", stores=["amber"], operations=list(operations))
        return {"Authorization": "Bearer " + made["secret"]}
    first, second, writer = issue("read", "save"), issue("read", "save"), issue("save")
    local = inbox.propose(dict(AGAIN), "amber", cfg=store, proposer="shared")["id"]      # the label alone
    with TestClient(server_mod.create_token_app()) as client:
        mine = _propose(client, FIRST, first)
        theirs = _tool(client, REWORDED, second, name="propose")["structuredContent"]
        for reply in (mine, theirs):
            assert not reply["duplicate"] and reply["lint"]["pending_duplicates"] == []
        assert _mine(client, first) == {mine["id"]} and _mine(client, second) == {theirs["id"]}
        assert [i["id"] for i in _tool(client, {"status": "all"}, second)["structuredContent"]["items"]] \
            == [theirs["id"]]
        assert client.get(f"/proposals/{mine['id']}", headers=first).json()["body"] == BODY
        missing = client.get(f"/proposals/{NO_SUCH_ID}", headers=first)
        told_missing = _tool(client, {"id": NO_SUCH_ID}, first)
        assert missing.status_code == 404 and told_missing["isError"] is True
        for cid, headers in ((theirs["id"], first), (local, first), (mine["id"], second), (local, second)):
            r = client.get(f"/proposals/{cid}", headers=headers)
            assert (r.status_code, r.json()) == (404, missing.json()), r.text
            assert _tool(client, {"id": cid}, headers) == told_missing
        # A token that may save but not read is refused, as /read refuses it.
        kept = _propose(client, UNRELATED, writer)["id"]
        assert client.get("/proposals", headers=writer).status_code == 403
        assert client.get(f"/proposals/{kept}", headers=writer).status_code == 403
        out = _tool(client, {"id": kept}, writer)
        assert out["isError"] is True and UNRELATED["body"] not in json.dumps(out)


def test_personal_tokens_of_one_identity_are_two_readers(userweb, directory, stores, monkeypatch):  # noqa: F811
    monkeypatch.setattr(save_mod, "_vector_near_matches", lambda cfg, body: [])
    client, tokens, _ = userweb
    user = "alice@example.invalid"

    def issue():
        made = tokens.issue_personal(subject="alice-sub", store=user, label="laptop",
                                     operation_id=str(uuid.uuid4()))
        return {"Authorization": "Bearer " + made["secret"]}
    # One account (the identity both are bound to) and one label: the token id is the credential.
    first, second = issue(), issue()
    mine, theirs = _propose(client, FIRST, first), _propose(client, REWORDED, second)
    assert theirs["lint"]["pending_duplicates"] == [] and mine["id"] not in json.dumps(theirs)
    again = _tool(client, AGAIN, second, name="propose")["structuredContent"]
    assert again["lint"]["pending_duplicates"] == [{"id": theirs["id"], "title": REWORDED["title"]}]
    assert _mine(client, first) == {mine["id"]} and _mine(client, second) == {theirs["id"], again["id"]}
    listed = _tool(client, {"status": "all"}, first)["structuredContent"]
    assert [i["id"] for i in listed["items"]] == [mine["id"]] and listed["profile"] == user
    missing = client.get(f"/proposals/{NO_SUCH_ID}", headers=first)
    assert missing.status_code == 404
    for cid, headers in ((theirs["id"], first), (mine["id"], second)):
        r = client.get(f"/proposals/{cid}", headers=headers)
        assert (r.status_code, r.json()) == (404, missing.json()), r.text
        assert _tool(client, {"id": cid}, headers) == _tool(client, {"id": NO_SUCH_ID}, headers)
    assert client.get(f"/proposals/{mine['id']}", headers=first).json()["body"] == BODY
    # No request field moves either of them into another user's store.
    assert client.get("/proposals", headers=first, params={"profile": "bob@example.invalid"}).status_code == 403
    assert _tool(client, {"store": "bob@example.invalid"}, first)["isError"] is True


def test_paging_is_bounded_on_both_surfaces(accounts):
    client, app, csrf, token, cfg = accounts
    api, agent = TestClient(app), {"Authorization": "Bearer " + token}
    for n in range(3):
        _propose(api, {"body": f"Proposal number {n} is about something of its own."}, agent)
    for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"limit": "many"}):
        assert api.get("/proposals", headers=agent, params=params).status_code == 422, params
    assert len(api.get("/proposals", headers=agent, params={"limit": 200}).json()["items"]) == 3
    # An offset past anything SQLite can count is an empty page, not a failure.
    far = api.get("/proposals", headers=agent, params={"offset": 2 ** 64})
    assert far.status_code == 200, far.text
    assert (far.json()["items"], far.json()["total"]) == ([], 3)
    out = _tool(api, {"offset": 2 ** 64, "limit": 10 ** 30}, agent)
    assert out["isError"] is False, out
    assert (out["structuredContent"]["items"], out["structuredContent"]["total"]) == ([], 3)
    assert len(_tool(api, {"offset": -5, "limit": 10 ** 30}, agent)["structuredContent"]["items"]) == 3
    assert len(_tool(api, {"limit": 0}, agent)["structuredContent"]["items"]) == 1
    assert len(_tool(api, {"limit": "2", "offset": "1"}, agent)["structuredContent"]["items"]) == 2
    assert _tool(api, {"limit": "many"}, agent)["isError"] is True


# --------------------------------------------------------------------------- pending duplicates


def test_pending_duplicates_are_named_for_reviewers_and_filtered_for_the_proposer(accounts, monkeypatch):
    client, app, csrf, token, cfg = accounts
    api, first = TestClient(app), {"Authorization": "Bearer " + token}
    second = _issue(client, csrf, _user("agent-user"), "desktop-agent")     # the same account, another credential

    def others_named(response_text, *theirs):
        return [t for t in theirs if t in response_text]

    filed = _propose(api, FIRST, first)
    f1 = filed["id"]
    assert filed["lint"]["pending_duplicates"] == [] and "others" not in json.dumps(filed)

    # Someone else's match is not mentioned at all: no id, no title, no count.
    r = api.post("/propose", headers=second, json=REWORDED)
    f2, lint = r.json()["id"], r.json()["lint"]
    assert lint["pending_duplicates"] == [] and "others" not in r.text
    assert not r.json()["duplicate"] and others_named(r.text, f1, FIRST["title"]) == []

    # The proposer's own match is named; the other one still is not mentioned.
    r = api.post("/propose", headers=first, json=AGAIN)
    f3, lint = r.json()["id"], r.json()["lint"]
    assert lint["pending_duplicates"] == [{"id": f1, "title": FIRST["title"]}]
    assert "others" not in r.text and others_named(r.text, f2, REWORDED["title"]) == []

    # The stored lint names every match, and a reviewer reads it through the inbox.
    everyone = [{"id": f1, "title": FIRST["title"]}, {"id": f2, "title": REWORDED["title"]}]
    assert inbox.get(cfg, "amber", f3)["lint"]["pending_duplicates"] == everyone
    assert "pending_duplicates_others" not in inbox.get(cfg, "amber", f3)["lint"]
    assert client.get(f"/inbox/{f3}").json()["lint"]["pending_duplicates"] == everyone
    assert client.get(f"/inbox/{f2}").json()["lint"]["pending_duplicates"] == [everyone[0]]
    assert client.get(f"/inbox/{f1}").json()["lint"]["pending_duplicates"] == []
    listed = {i["id"]: i["lint"]["pending_duplicates"] for i in client.get("/inbox").json()["items"]}
    assert listed[f3] == everyone

    # A fact about something else restates nothing.
    lint = _propose(api, UNRELATED, first)["lint"]
    assert lint["pending_duplicates"] == []

    # The same text again, from someone else: the first proposer's stored lint names
    # f1 and f2, but this reply names only the caller's own f2. Nothing of the first's
    # comes back: not its title or text, as it wrote them, nor its other candidate's.
    shouted = {"title": AGAIN["title"].upper(), "body": BODY.upper()}
    r = api.post("/propose", headers=second, json=shouted)
    assert r.status_code == 200 and r.json()["duplicate"] is True and r.json()["id"] == f3
    lint = r.json()["lint"]
    assert lint["pending_duplicates"] == [{"id": f2, "title": REWORDED["title"]}]
    assert "others" not in r.text
    assert others_named(r.text, f1, FIRST["title"], AGAIN["title"], BODY) == []
    out = _tool(api, shouted, second, name="propose")
    assert out["structuredContent"]["duplicate"] is True and out["structuredContent"]["lint"] == lint
    assert others_named(json.dumps(out), f1, FIRST["title"], AGAIN["title"], BODY) == []
    # The id it was answered with is still not its own to read.
    assert api.get(f"/proposals/{f3}", headers=second).status_code == 404
    assert f3 not in _mine(api, second) and f3 in _mine(api, first)
    # ... and from the proposer itself: its own other match, and nothing else.
    again = _propose(api, AGAIN, first)
    assert again["duplicate"] is True and again["id"] == f3
    assert again["lint"]["pending_duplicates"] == [{"id": f1, "title": FIRST["title"]}]
    assert "others" not in json.dumps(again)
    assert inbox.get(cfg, "amber", f3)["lint"]["pending_duplicates"] == everyone      # the stored lint is untouched

    # A queued save says so in its receipt, under the same filter.
    monkeypatch.setenv("MEMD_SAVE_MODE", "inbox")
    r = api.post("/save", headers=first, json={"title": "Backup timing", "body": BODY})
    assert r.status_code == 202 and r.json()["queued"] is True, r.text
    [warning] = [w for w in r.json()["warnings"] if "already waiting for review" in w]
    assert warning == (f"Similar candidates are already waiting for review: {f1} ({FIRST['title']}), "
                       f"{f3} ({AGAIN['title']}).")
    assert others_named(r.text, f2, REWORDED["title"]) == [] and "someone else" not in r.text
    out = _tool(api, {"title": "Archive backup hours", "body": BODY}, second, name="save")
    assert out["isError"] is False and out["structuredContent"]["queued"] is True
    [warning] = [w for w in out["structuredContent"]["warnings"] if "already waiting for review" in w]
    assert warning == f"Similar candidates are already waiting for review: {f2} ({REWORDED['title']})."
    assert others_named(json.dumps(out), f1, f3, FIRST["title"], AGAIN["title"], "Backup timing") == []
    # A queued save that restates nothing carries no such line.
    r = api.post("/save", headers=first, json={"body": "The office printer is on the guest VLAN."})
    assert r.status_code == 202 and not [w for w in r.json()["warnings"] if "already waiting" in w]


def test_pending_candidates_carry_proposer_ids_and_pending_texts_keeps_its_shape(store):
    local = inbox.propose(dict(FIRST), "amber", cfg=store, proposer="mem-import", now=1000.0)
    decided = inbox.propose(dict(UNRELATED), "amber", cfg=store, proposer="mem-import", now=1500.0)["id"]
    inbox.reject(store, "amber", decided, reviewer=inbox.operator_reviewer("owner"))
    with TestClient(server_mod.create_token_app()) as client:
        remote = _propose(client, REWORDED, AUTH)
    # The token has a credential and the local candidate has no ids: someone else's.
    assert remote["lint"]["pending_duplicates"] == [] and "others" not in json.dumps(remote)
    # A local caller's own match is named too: no ids on either side, and the same label.
    again = inbox.propose(dict(AGAIN), "amber", cfg=store, proposer="mem-import")
    assert again["lint"]["pending_duplicates"] == [{"id": local["id"], "title": FIRST["title"]}]
    other = inbox.propose({"title": "Backup timing", "body": BODY}, "amber", cfg=store, proposer="mem-inbox distill")
    assert other["lint"]["pending_duplicates"] == [] and "others" not in json.dumps(other)

    waiting = list(inbox.pending_candidates(store, "amber"))
    assert [c["id"] for c in waiting] == [local["id"], remote["id"], again["id"], other["id"]]      # oldest first
    assert set(waiting[0]) == {"id", "title", "body", "source", "proposer", "proposer_account", "proposer_credential"}
    assert (waiting[0]["title"], waiting[0]["body"], waiting[0]["proposer"]) == (FIRST["title"], BODY, "mem-import")
    assert (waiting[0]["proposer_account"], waiting[0]["proposer_credential"]) == (None, None)
    assert (waiting[1]["proposer_account"], waiting[1]["proposer_credential"]) == \
        (None, "legacy:" + control.digest(TOKEN)[:12])
    texts = inbox.pending_texts(store, "amber")
    assert (local["id"], FIRST["title"], BODY) in texts and len(texts) == 4
    assert all(len(row) == 3 for row in texts)
    # A lint computed by the caller still gains the field, and is not changed in place.
    given = {"action": "create"}
    filed = inbox.propose({"title": "Archive backup hours", "body": BODY}, "amber", cfg=store,
                          proposer="mem-import", precomputed_lint=given)
    assert given == {"action": "create"} and filed["lint"]["action"] == "create"
    assert len(inbox.get(store, "amber", filed["id"])["lint"]["pending_duplicates"]) == 4


def test_a_proposers_reply_does_not_depend_on_what_others_have_pending(store):
    """No oracle: the reply is the same whether or not someone else's similar candidate waits."""
    operator = inbox.operator_reviewer("owner")
    theirs = inbox.propose(dict(FIRST), "amber", cfg=store, proposer="mem-import", now=1000.0)["id"]
    with TestClient(server_mod.create_token_app()) as client:
        beside = _propose(client, REWORDED, AUTH)                 # someone else's match is pending
        inbox.reject(store, "amber", theirs, reviewer=operator)
        inbox.reject(store, "amber", beside["id"], reviewer=operator)
        alone = _propose(client, REWORDED, AUTH)                  # nothing is pending
    assert alone["id"] != beside["id"] and not alone["duplicate"]
    assert {k: v for k, v in beside.items() if k != "id"} == {k: v for k, v in alone.items() if k != "id"}
    # The reviewers' copy does differ: that is where the match is recorded.
    assert inbox.get(store, "amber", beside["id"])["lint"]["pending_duplicates"] == [{"id": theirs, "title": FIRST["title"]}]
    assert inbox.get(store, "amber", alone["id"])["lint"]["pending_duplicates"] == []


def test_a_queued_save_and_an_identical_proposal_do_not_depend_on_what_others_have_pending(store, monkeypatch):
    """No oracle on the other replies either: a queued save (REST and MCP) and the exact-duplicate reply."""
    operator = inbox.operator_reviewer("owner")

    def told(client):
        """Everything one caller is told, its own candidate ids masked; nothing of its own is left pending."""
        monkeypatch.setenv("MEMD_SAVE_MODE", "inbox")
        queued = client.post("/save", headers=AUTH, json=REWORDED)
        assert queued.status_code == 202, queued.text
        own = [queued.json()["inbox_id"]]
        replies = [queued.json(), client.post("/save", headers=AUTH, json=REWORDED).json()]
        monkeypatch.setenv("MEMD_SAVE_MODE", "direct")
        replies.append(_propose(client, REWORDED, AUTH))
        assert replies[1]["duplicate"] is True and replies[2]["duplicate"] is True
        inbox.reject(store, "amber", own[0], reviewer=operator)
        monkeypatch.setenv("MEMD_SAVE_MODE", "inbox")
        replies.append(_tool(client, REWORDED, AUTH, name="save"))
        own.append(replies[3]["structuredContent"]["inbox_id"])
        inbox.reject(store, "amber", own[1], reviewer=operator)
        text = json.dumps(replies, sort_keys=True)
        for n, cid in enumerate(own):
            text = text.replace(cid, f"<own {n}>")
        return text, own

    theirs = inbox.propose(dict(FIRST), "amber", cfg=store, proposer="mem-import")["id"]
    with TestClient(server_mod.create_token_app()) as client:
        beside, filed_beside = told(client)                 # someone else's match is pending
        inbox.reject(store, "amber", theirs, reviewer=operator)
        alone, filed_alone = told(client)                   # nothing is
    assert beside == alone
    assert theirs not in beside and FIRST["title"] not in beside
    # The reviewers' copies do differ.
    for cid in filed_beside:
        assert inbox.get(store, "amber", cid)["lint"]["pending_duplicates"] == [{"id": theirs, "title": FIRST["title"]}]
    for cid in filed_alone:
        assert inbox.get(store, "amber", cid)["lint"]["pending_duplicates"] == []


def test_an_identical_proposal_from_someone_else_gets_the_lint_of_its_own_fact(accounts, monkeypatch):
    """The exact-duplicate reply says a candidate with this text waits, and its id: nothing else of it."""
    client, app, csrf, token, cfg = accounts
    api, first = TestClient(app), {"Authorization": "Bearer " + token}
    second = _issue(client, csrf, _user("agent-user"), "desktop-agent")
    # What the first proposer asked for shapes its lint: the slug it chose, the note it would replace.
    theirs = _propose(api, {**FIRST, "slug": "private-runbook", "supersedes": "repo-hosting-policy"}, first)
    assert (theirs["lint"]["slug"], theirs["lint"]["action"], theirs["lint"]["supersedes"]) == \
        ("private-runbook", "supersede", "repo-hosting-policy")

    def nothing_of_theirs(text):
        return not [word for word in ("private-runbook", "repo-hosting-policy", "supersede") if word in text]

    r = api.post("/propose", headers=second, json=FIRST)
    assert r.status_code == 200 and r.json()["duplicate"] is True and r.json()["id"] == theirs["id"]
    assert (r.json()["lint"]["slug"], r.json()["lint"]["action"]) == ("backup-window", "create")
    assert r.json()["lint"]["pending_duplicates"] == [] and nothing_of_theirs(r.text), r.text
    out = _tool(api, FIRST, second, name="propose")
    assert out["structuredContent"]["lint"] == r.json()["lint"] and nothing_of_theirs(json.dumps(out))
    # Its proposer is still answered from the stored lint, and the reviewers' copy is as it was.
    again = _propose(api, FIRST, first)
    assert again["duplicate"] is True and again["lint"] == theirs["lint"]
    assert inbox.get(cfg, "amber", theirs["id"])["lint"]["supersedes"] == "repo-hosting-policy"
    # A queued save of the same text is a duplicate too, and its receipt is about what was sent.
    monkeypatch.setenv("MEMD_SAVE_MODE", "inbox")
    r = api.post("/save", headers=second, json=FIRST)
    assert r.status_code == 202 and r.json()["duplicate"] is True and r.json()["inbox_id"] == theirs["id"]
    assert r.json()["slug"] == "backup-window" and nothing_of_theirs(r.text), r.text
    out = _tool(api, FIRST, second, name="save")
    assert out["structuredContent"]["slug"] == "backup-window" and nothing_of_theirs(json.dumps(out))
    assert api.post("/save", headers=first, json=FIRST).json()["slug"] == "private-runbook"


def test_a_proposal_reads_the_pending_candidates_one_at_a_time(store):
    """MAX_PENDING candidates of MAX_BODY characters may wait: a proposal must not hold them all at once."""
    inbox.propose(dict(UNRELATED), "amber", cfg=store, proposer="mem-import", precomputed_lint={})
    rows, size = 60, 100_000
    conn = inbox._connect(inbox.inbox_path(store, "amber"))
    try:
        with conn:
            for n in range(rows):
                title, body = f"Long candidate {n}", (f"filler-{n:03d}-" + "x" * 40 + " ") * (size // 52)
                conn.execute("INSERT INTO candidates(id, created, source, proposer, title, body, content_hash) "
                             "VALUES(?,?,?,?,?,?,?)", (f"{n:016x}", 1000.0 + n, "agent", "someone-else", title,
                                                      body, inbox.content_hash(title, body)))
    finally:
        conn.close()
    tracemalloc.start()
    try:
        filed = inbox.propose(dict(FIRST), "amber", cfg=store, proposer="mem-import", precomputed_lint={})
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert not filed["duplicate"] and filed["lint"]["pending_duplicates"] == []
    assert peak < rows * size // 4, f"{peak} bytes at peak for {rows * size} characters of pending text"
    # The texts are still compared: one of the long ones, said again, is found.
    conn = inbox._connect(inbox.inbox_path(store, "amber"))
    try:
        title, body = conn.execute("SELECT title, body FROM candidates WHERE id=?", (f"{7:016x}",)).fetchone()
    finally:
        conn.close()
    said_again = inbox.propose({"title": title + " again", "body": body}, "amber", cfg=store,
                               proposer="someone-else", precomputed_lint={})
    assert said_again["lint"]["pending_duplicates"] == [{"id": f"{7:016x}", "title": title}]


def test_a_handoff_is_not_reported_as_a_duplicate_of_the_one_it_replaces(store):
    def handoff(state):
        return handoff_hook.handoff_fact({"key": "git.example.com/team/widget"}, state, 1_790_000_000.0)
    state = "Retry logic is done and the backoff tests pass; the release notes are the next step."
    first = inbox.propose(handoff(state), "amber", cfg=store, proposer="laptop")
    second = inbox.propose(handoff(state + " Then tag it."), "amber", cfg=store, proposer="laptop")
    assert [c["id"] for c in inbox.pending_candidates(store, "amber")] == [second["id"]]
    assert second["lint"]["pending_duplicates"] == []
    assert inbox.get(store, "amber", second["id"])["lint"]["pending_duplicates"] == []
    assert first["id"] != second["id"]
    # An ordinary proposal that restates the handoff is still told.
    plain = inbox.propose({"title": handoff(state)["title"], "body": state + " Then tag it today."}, "amber",
                          cfg=store, proposer="laptop")
    assert plain["lint"]["pending_duplicates"] == [{"id": second["id"], "title": handoff(state)["title"]}]


# --------------------------------------------------------------------------- the tool


def test_the_proposals_tool_is_advertised_beside_propose_and_is_read_only():
    tools = asyncio.run(mcp_mod.list_tools())
    assert [t.name for t in tools] == ["recall", "save", "timeline", "ask", "read", "propose", "proposals", "publish"]
    tool = next(t for t in tools if t.name == "proposals")
    assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True
    schema = tool.input_schema
    assert set(schema["properties"]) == {"status", "id", "limit", "offset", "store", "profile"}
    assert schema["required"] == []
    assert schema["properties"]["status"]["enum"] == ["pending", "approved", "rejected", "all"]
    assert {"items", "proposal", "counts", "total"} <= set(tool.output_schema["properties"])


def test_a_rotated_registry_token_keeps_its_proposals_and_still_cannot_approve_them(store, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMD_CONTROL_DB", str(tmp_path / "control" / "control.db"))
    tokens = registry.Registry()
    tokens.initialize()

    def issue():
        return tokens.issue(actor="operator", operation_id=str(uuid.uuid4()), label="shared",
                            owner="team", purpose="agents", stores=["amber"], operations=["read", "save"])
    made, other = issue(), issue()
    bearer = lambda secret: {"Authorization": "Bearer " + secret}
    with TestClient(server_mod.create_token_app()) as client:
        mine = _propose(client, FIRST, bearer(made["secret"]))
        rotated = tokens.rotate(made["token"]["id"], actor="operator", operation_id=str(uuid.uuid4()),
                               revision=made["token"]["revision"], overlap_hours=0)
        successor = bearer(rotated["secret"])
        assert _mine(client, successor) == {mine["id"]}
        assert client.get(f"/proposals/{mine['id']}", headers=successor).json()["body"] == BODY
        again = _propose(client, FIRST, successor)
        assert again["duplicate"] is True and again["id"] == mine["id"]
        assert _mine(client, bearer(other["secret"])) == set()
        assert client.get(f"/proposals/{mine['id']}", headers=bearer(other["secret"])).status_code == 404
    assert registry.lineage(rotated["token"]["id"]) == (rotated["token"]["id"], made["token"]["id"])
    row = {"proposer_credential": made["token"]["id"], "proposer": "shared"}
    for credential, expected in ((rotated["token"]["id"], True), (made["token"]["id"], True),
                                 (other["token"]["id"], False)):
        reviewer = inbox._reviewer("shared", credential=credential, is_token=True, store="amber")
        assert inbox._own_proposal(row, reviewer) is expected


def test_every_token_of_a_rotation_is_one_proposer_and_an_unreadable_registry_refuses(store, tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("MEMD_CONTROL_DB", str(tmp_path / "control" / "control.db"))
    tokens = registry.Registry()
    tokens.initialize()

    def issue():
        return tokens.issue(actor="operator", operation_id=str(uuid.uuid4()), label="shared",
                            owner="team", purpose="agents", stores=["amber"], operations=["read", "save"])

    def rotate(made):
        return tokens.rotate(made["token"]["id"], actor="operator", operation_id=str(uuid.uuid4()),
                             revision=made["token"]["revision"], overlap_hours=24)
    made, other = issue(), issue()
    rotated = rotate(made)
    again = rotate(rotated)
    family = frozenset(t["token"]["id"] for t in (made, rotated, again))
    for t in (made, rotated, again):
        assert registry.rotation_family(t["token"]["id"]) == family
    assert registry.rotation_family(other["token"]["id"]) == {other["token"]["id"]}
    # The newest token filed it. The older ones are still active during the overlap, and decide nothing of it.
    row = {"proposer_credential": again["token"]["id"], "proposer": "shared"}
    for t, expected in ((made, True), (rotated, True), (again, True), (other, False)):
        reviewer = inbox._reviewer("shared", credential=t["token"]["id"], is_token=True, store="amber")
        assert inbox._own_proposal(row, reviewer) is expected
    # A registry that cannot be read refuses the decision; reading back one's own only narrows.

    def unreadable(self, *, write=False):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(registry.Registry, "connection", unreadable)
    reviewer = inbox._reviewer("shared", credential=other["token"]["id"], is_token=True, store="amber")
    with pytest.raises(inbox.PolicyRefusal, match="rotation history"):
        inbox._own_proposal(row, reviewer)
    assert registry.lineage(again["token"]["id"]) == (again["token"]["id"],)


def test_a_full_inbox_refuses_before_reading_the_pending_texts(store, monkeypatch):
    monkeypatch.setattr(inbox, "MAX_PENDING", 2)
    inbox.propose(dict(FIRST), "amber", cfg=store, proposer="mem-import", precomputed_lint={})
    inbox.propose(dict(UNRELATED), "amber", cfg=store, proposer="mem-import", precomputed_lint={})

    def refuse_read(*args, **kwargs):
        raise AssertionError("read the pending texts")
    monkeypatch.setattr(inbox, "pending_candidates", refuse_read)
    with pytest.raises(ValueError, match="already holds 2 pending"):
        inbox.propose({"title": "Gate code", "body": "The side gate code changes every quarter."},
                      "amber", cfg=store, proposer="mem-import", precomputed_lint={})
