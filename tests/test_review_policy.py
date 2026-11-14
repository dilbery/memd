"""A review policy per store (memd.inbox.review_policy): open, agents, four-eyes.

Driven through the REST surface, and through MCP where a tool exists. Under
four-eyes nothing reaches the store through save, publish or a console edit
without a decision from another account, and no reviewer changes its text.
Hermetic: temp Git/SQLite stores behind the administered access layer (or the
token registry, or a legacy token alone), no model backends; the socket guard
blocks everything else.
"""
import json
import logging
from pathlib import Path
import re
from types import SimpleNamespace
import uuid

import pytest
import respx
from fastapi.testclient import TestClient

import memd.save as save_mod
import memd.server as server_mod
from memd import control, inbox, registry, share, user_memory
from memd.hooks.handoff import handoff_fact
from memd.identity import bind_identity, clear_identity
from memd.normalize import normalize_fact
from memd.store import list_notes, read_note
from tests.test_admin_web import key  # noqa: F401  (fixture)
from tests.test_clients_ops_improvements import load_script
from tests.test_inbox import AUTH, FACT, _as_request, _mcp, _stored_ids, accounts, head, store  # noqa: F401  (fixtures)
from tests.test_personal_tokens import directory  # noqa: F401  (fixture)
from tests.test_review_capability import LEGACY, MCP, REVIEW_ONLY, World, _refused
from tests.test_user_memory import stores  # noqa: F401  (fixture)
from tests.test_user_web import headers, sign_in, userweb  # noqa: F401  (fixture)

GARAGE = "The garage door opener is on the IoT VLAN."
PRINTER = "The office printer is on the guest VLAN."
NAS = "The archive NAS exports /srv/share over NFS v4."
# What each four-eyes refusal says, in the server's words.
SECOND = "account that proposed it"
TEXT = "importance and tags"
ACCOUNT = "reviewer with an account"


@pytest.fixture
def world(accounts):  # noqa: F811
    """The accounts fixture (owner, agent-user and its token laptop-agent, the served
    store amber) with a second store, team, and the owner's session ready to post."""
    made = World(accounts)
    made.admin.headers.update(made.csrf)
    made.store("team")
    return made


def _policy(world, name, policy, **config):
    r = world.admin.put(f"/admin/stores/{name}", json={"config": {"review_policy": policy, **config}})
    assert r.status_code == 200, r.text
    assert r.json()["store"]["review_policy"] == policy
    return r.json()["store"]


def _set_by_hand(name, value):
    """Put a policy in the store's configuration that no request would be allowed to store."""
    with control.db() as c:
        config = json.loads(c.execute("SELECT config FROM stores WHERE id=?", (name,)).fetchone()[0])
        c.execute("UPDATE stores SET config=? WHERE id=?", (json.dumps({**config, "review_policy": value}), name))


def _listed(world, name):
    return next(s for s in world.overview()["stores"] if s["id"] == name)


def _clone_of(name):
    return Path(control.store(name)["config"]["clone_path"])


def _queued(response):
    """A save or a publish that waits: 202 and the queued receipt. Returns the candidate id."""
    assert response.status_code == 202, response.text
    receipt = response.json()
    assert receipt["queued"] is True and re.fullmatch(r"[0-9a-f]{16}", receipt["inbox_id"]), receipt
    return receipt["inbox_id"]


def _saved(response):
    assert response.status_code == 200, response.text
    assert response.json()["saved"] is True and "queued" not in response.json(), response.text
    return response.json()


def _decide(client, cid, decision, name=None, bearer=None, **body):
    return client.post(f"/inbox/{cid}/{decision}", headers=bearer,
                       json={**body, **({"profile": name} if name else {})})


def _routes(cid, name):
    return (("get", f"/inbox?profile={name}", None), ("get", f"/inbox/{cid}?profile={name}", None),
            ("post", f"/inbox/{cid}/approve", {"profile": name}), ("post", f"/inbox/{cid}/reject", {"profile": name}))


def _token(world, uid, **fields):
    r = world.issue(uid, **fields)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}, r.json()["id"]


def _signed_in(world, uid, username):
    """A session of an account that so far only had tokens."""
    reset = world.admin.post(f"/admin/users/{uid}/reset-password")
    assert reset.status_code == 200, reset.text
    return world.session({"username": username, "password": reset.json()["temporary_password"]})


# --------------------------------------------------------------------------- which policy


def test_the_policy_is_the_stores_setting_else_the_environment_else_open(world, monkeypatch):
    assert inbox.REVIEW_POLICIES == ("open", "agents", "four-eyes")
    assert [inbox.review_policy(name) for name in ("amber", "team")] == ["open", "open"]
    # The environment speaks for a store without a setting, in either case and with either separator.
    for written, policy in (("agents", "agents"), ("four-eyes", "four-eyes"), ("open", "open"),
                            ("Four-Eyes", "four-eyes"), ("FOUR_EYES", "four-eyes"), ("four_eyes", "four-eyes"),
                            (" Agents ", "agents"), ("OPEN", "open"), ("", "open"), ("   ", "open")):
        monkeypatch.setenv("MEMD_TEAM_REVIEW_POLICY", written)
        assert inbox.review_policy("team") == policy, written
        assert _listed(world, "team")["review_policy"] == policy, written
        assert inbox.review_policy("amber") == "open"            # the variable names one store
    # Its name is built as MEMD_<STORE>_PUBLISH_REVIEW is.
    world.store("team-notes")
    monkeypatch.setenv("MEMD_TEAM_NOTES_REVIEW_POLICY", "agents")
    monkeypatch.setenv("MEMD_TEAM_NOTES_PUBLISH_REVIEW", "false")
    assert inbox.review_policy("team-notes") == "agents" and share.publish_review("team-notes") is False
    # The store's own setting wins over the environment, whichever is the stricter.
    monkeypatch.setenv("MEMD_TEAM_REVIEW_POLICY", "four-eyes")
    _policy(world, "team", "open")
    assert inbox.review_policy("team") == "open"
    monkeypatch.setenv("MEMD_TEAM_REVIEW_POLICY", "open")
    _policy(world, "team", "four-eyes")
    assert inbox.review_policy("team") == "four-eyes"
    # A setting written by hand is read as loosely as the variable is.
    monkeypatch.setenv("MEMD_TEAM_REVIEW_POLICY", "open")
    _set_by_hand("team", "Four_Eyes")
    assert inbox.review_policy("team") == "four-eyes"
    # Without administration only the environment is asked.
    monkeypatch.setenv("MEMD_TEAM_REVIEW_POLICY", "agents")
    monkeypatch.delenv("MEMD_ADMIN_DB")
    assert inbox.review_policy("team") == "agents" and inbox.review_policy("amber") == "open"


def test_a_policy_that_is_not_one_of_the_three_means_four_eyes_and_is_logged(world, monkeypatch, caplog):
    monkeypatch.setattr(inbox, "_warned", set())
    caplog.set_level(logging.WARNING, logger="memd.inbox")
    unknown = ("strict", "agent", "four eyes", "4-eyes", "true", "off", "none")
    for written in unknown:
        monkeypatch.setenv("MEMD_TEAM_REVIEW_POLICY", written)
        assert inbox.review_policy("team") == "four-eyes", written
        assert inbox.review_policy("team") == "four-eyes"
    logged = [r.getMessage() for r in caplog.records if r.name == "memd.inbox"]
    assert len(logged) == len(unknown)                  # once for each value, not once for each request
    assert all("store team" in line and "treating it as four-eyes" in line for line in logged)
    assert "'strict'" in logged[0] and "'none'" in logged[-1]
    # It is not another word for open: the store really does hold a session's save back.
    cid = _queued(world.admin.post("/save", json={"profile": "team", "body": GARAGE}))
    assert _refused(_decide(world.admin, cid, "approve", "team"), SECOND)
    _saved(world.admin.post("/save", json={"body": GARAGE}))            # the store beside it is untouched
    # Nor is a setting that no request could have stored.
    monkeypatch.delenv("MEMD_TEAM_REVIEW_POLICY")
    for value in ("reviewed", True, 2, ["open"], {"policy": "open"}):
        _set_by_hand("team", value)
        assert inbox.review_policy("team") == "four-eyes", value
        assert _listed(world, "team")["review_policy"] == "four-eyes", value
    assert len([r for r in caplog.records if r.name == "memd.inbox"]) == len(unknown) + 5
    # An empty one is no setting at all.
    for value in ("", "  ", None):
        _set_by_hand("team", value)
        assert inbox.review_policy("team") == "open", value


# --------------------------------------------------------------------------- agents


def test_agents_queues_a_tokens_save_into_that_store_and_no_other(world):
    assert world.grant(world.agent_user, {"amber": "write", "team": "write"}).status_code == 200
    token = world.token(world.agent_user, scope="write", extra_stores={"team": "write"})
    mcp = {**token, **MCP}
    _policy(world, "team", "agents", publish_review=False)
    clone, before = _clone_of("team"), head(_clone_of("team"))
    # MEMD_SAVE_MODE is not set: the store's policy is what holds the token's save.
    r = world.api.post("/save", headers=token, json={"profile": "team", "title": "Printer", "body": PRINTER})
    first = _queued(r)
    assert r.json()["saved"] is False and r.json()["action"] == "proposed"
    assert "review policy is agents" in r.json()["warnings"][0]
    out = _mcp(world.api, "save", {"profile": "team", "title": "NAS", "body": NAS}, headers=mcp)
    assert out["isError"] is False and out["structuredContent"]["queued"] is True
    published = _queued(world.api.post("/publish", headers=token,
                                       json={"slug": "repo-hosting-policy", "target_store": "team"}))
    assert head(clone) == before and list_notes(clone) == []
    # The same token saves directly into the store beside it, over both surfaces ...
    assert _saved(world.api.post("/save", headers=token, json={"title": "Printer", "body": PRINTER}))["slug"] == "printer"
    out = _mcp(world.api, "save", {"title": "NAS", "body": NAS}, headers=mcp)
    assert out["isError"] is False and out["structuredContent"]["saved"] is True
    _saved(world.api.post("/save", headers=LEGACY, json={"body": GARAGE}))
    # ... and a session saves and publishes directly into the agents store.
    _saved(world.admin.post("/save", json={"profile": "team", "title": "Garage door", "body": GARAGE}))
    r = world.admin.post("/publish", json={"slug": "vmhost-proxmox-vm", "target_store": "team"})
    assert r.status_code == 200 and r.json()["action"] == "published", r.text
    assert world.admin.get("/inbox?profile=team").json()["total"] == 3
    assert world.admin.get("/inbox").json()["total"] == 0
    # Who decides is as under open: the proposing account's own session may, and may correct the text.
    session = _signed_in(world, world.agent_user, "agent-user")
    r = _decide(session, first, "approve", "team", edits={"body": PRINTER + " It is the only one."})
    assert r.status_code == 200, r.text
    assert read_note(clone, r.json()["receipt"]["slug"]).body.endswith("It is the only one.")
    assert _decide(session, published, "reject", "team").status_code == 200


def test_agents_needs_neither_the_save_mode_nor_administration(store, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MEMD_AMBER_REVIEW_POLICY", "Agents")
    before = head(store.clone)
    with TestClient(server_mod.create_token_app()) as client:
        cid = _queued(client.post("/save", headers=AUTH, json=FACT))
        out = _mcp(client, "save", {"title": "Printer", "body": PRINTER})
        assert out["isError"] is False and out["structuredContent"]["queued"] is True
        assert "review policy is agents" in out["structuredContent"]["warnings"][0]
    assert head(store.clone) == before and inbox.list_candidates(store, "amber")["total"] == 2
    assert inbox.get(store, "amber", cid)["source"] == "save"
    # The variable is this store's; a save mode of direct does not undo it.
    monkeypatch.setenv("MEMD_SAVE_MODE", "direct")
    with TestClient(server_mod.create_token_app()) as client:
        _queued(client.post("/save", headers=AUTH, json={"body": NAS}))
        monkeypatch.delenv("MEMD_AMBER_REVIEW_POLICY")
        _saved(client.post("/save", headers=AUTH, json={"body": GARAGE}))


# --------------------------------------------------------------------------- four-eyes: what waits


def test_four_eyes_holds_every_save_and_publish_until_another_account_decides(world):
    assert world.grant(world.agent_user, {"amber": "write", "team": "write"}).status_code == 200
    token = world.token(world.agent_user, scope="write", extra_stores={"team": "write"})
    mcp = {**token, **MCP}
    cobalt = world.session(world.member("cobalt", {"amber": "read", "team": REVIEW_ONLY}))
    # Published notes would be saved directly, were it not for the policy.
    _policy(world, "team", "four-eyes", publish_review=False)
    clone, before = _clone_of("team"), head(_clone_of("team"))
    r = world.api.post("/save", headers=token, json={"profile": "team", "title": "Printer", "body": PRINTER})
    by_token = _queued(r)
    assert r.json()["saved"] is False and "review policy is four-eyes" in r.json()["warnings"][0]
    out = _mcp(world.api, "save", {"profile": "team", "title": "NAS", "body": NAS}, headers=mcp)
    assert out["isError"] is False and out["structuredContent"]["queued"] is True
    # A signed-in session's save waits too, and is recorded as the session's.
    r = world.admin.post("/save", json={"profile": "team", "title": "Garage door", "body": GARAGE})
    by_session = _queued(r)
    assert r.json()["saved"] is False and r.json()["action"] == "proposed"
    assert _stored_ids(share._cfg("team"), by_session, "team") == (world.owner, "session", None)
    assert world.admin.get(f"/inbox/{by_session}?profile=team").json()["proposer"] == "owner"
    # So does every publish, a token's and a session's.
    published = _queued(world.api.post("/publish", headers=token,
                                       json={"slug": "repo-hosting-policy", "target_store": "team"}))
    by_owner = _queued(world.admin.post("/publish", json={"slug": "vmhost-proxmox-vm", "target_store": "team"}))
    out = _mcp(world.api, "publish", {"slug": "gpuhost-inference-tuning", "target_store": "team"}, headers=mcp)
    assert out["isError"] is False and out["structuredContent"]["action"] == "queued"
    # Nothing is in the store, and what it held back is all there to review.
    assert head(clone) == before and list_notes(clone) == []
    assert cobalt.get("/inbox?profile=team").json()["total"] == 6
    # The store beside it still takes the same callers' saves at once.
    _saved(world.api.post("/save", headers=token, json={"body": PRINTER}))
    _saved(world.admin.post("/save", json={"body": GARAGE}))
    # Each proposer's own account is refused, the session's as the token's.
    assert _refused(_decide(world.admin, by_session, "approve", "team"), SECOND)
    assert _refused(_decide(world.admin, by_owner, "reject", "team"), SECOND)
    assert head(clone) == before
    # Another account decides, and only then is anything written.
    r = _decide(cobalt, by_token, "approve", "team")
    assert r.status_code == 200, r.text
    note = read_note(clone, r.json()["receipt"]["slug"])
    assert (note.title, note.body, note.saved_by) == ("Printer", PRINTER, "agent")
    r = _decide(cobalt, by_session, "approve", "team")
    assert r.status_code == 200, r.text
    assert read_note(clone, r.json()["receipt"]["slug"]).saved_by == "owner"
    r = _decide(cobalt, published, "approve", "team")
    assert r.status_code == 200, r.text
    assert read_note(clone, r.json()["receipt"]["slug"]).metadata["published_from"]["slug"] == "repo-hosting-policy"
    assert _decide(cobalt, by_owner, "reject", "team", reason="not for the team").status_code == 200
    assert len(list_notes(clone)) == 3
    assert cobalt.get("/inbox?profile=team&status=all").json()["counts"] == {"pending": 2, "approved": 3, "rejected": 1}


def test_under_four_eyes_a_save_over_an_existing_note_and_a_republish_wait_too(world):
    assert world.grant(world.agent_user, {"amber": "write", "team": "write"}).status_code == 200
    token = world.token(world.agent_user, scope="write", extra_stores={"team": "write"})
    cobalt = world.session(world.member("cobalt", {"amber": "read", "team": REVIEW_ONLY}))
    # A published copy, made while the store took publishes directly.
    _policy(world, "team", "open", publish_review=False)
    r = world.admin.post("/publish", json={"slug": "vmhost-proxmox-vm", "target_store": "team"})
    assert r.status_code == 200 and r.json()["action"] == "published", r.text
    _policy(world, "team", "four-eyes")
    clone = _clone_of("team")
    before, copy = head(clone), read_note(clone, "vmhost-proxmox-vm")
    # Its source changes and is published again; the copy is saved over by slug; another note would retire it.
    _saved(world.admin.post("/save", json={"slug": "vmhost-proxmox-vm", "body": "The VM moved to the second node."}))
    again = _queued(world.admin.post("/publish", json={"slug": "vmhost-proxmox-vm", "target_store": "team"}))
    over = _queued(world.admin.post("/save", json={"profile": "team", "slug": "vmhost-proxmox-vm",
                                                   "body": "Overwritten by its owner alone."}))
    retire = _queued(world.api.post("/save", headers=token, json={
        "profile": "team", "title": "VM hosts", "body": "The VMs run on the second node.",
        "supersedes": "vmhost-proxmox-vm"}))
    out = _mcp(world.api, "save", {"profile": "team", "slug": "vmhost-proxmox-vm", "body": "Changed over MCP."},
               headers={**token, **MCP})
    assert out["isError"] is False and out["structuredContent"]["queued"] is True
    # A session has no MCP surface to save through instead: MCP takes bearer tokens only.
    r = world.admin.post("/mcp/", headers=MCP, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "save", "arguments": {"profile": "team", "slug": "vmhost-proxmox-vm", "body": "By session."}}})
    assert r.status_code == 401, r.text
    now = read_note(clone, "vmhost-proxmox-vm")
    assert head(clone) == before and (now.body, now.superseded_by, now.git_blob) == (copy.body, None, copy.git_blob)
    # The account that asked decides none of them, and no reviewer drops the revision a republish was made against.
    for cid in (again, over):
        assert _refused(_decide(world.admin, cid, "approve", "team"), SECOND)
    assert _refused(_decide(cobalt, again, "approve", "team", edits={"expected_revision": None}), TEXT)
    assert head(clone) == before
    r = _decide(cobalt, again, "approve", "team")
    assert r.status_code == 200, r.text
    assert read_note(clone, "vmhost-proxmox-vm").body == "The VM moved to the second node."
    r = _decide(cobalt, retire, "approve", "team")
    assert r.status_code == 200, r.text
    assert read_note(clone, "vmhost-proxmox-vm").superseded_by == r.json()["receipt"]["slug"]


def test_under_four_eyes_a_handoff_waits_for_another_account_like_any_candidate(world):
    _policy(world, "amber", "four-eyes")
    cobalt = world.session(world.member("cobalt", {"amber": REVIEW_ONLY}))
    agent_session = _signed_in(world, world.agent_user, "agent-user")
    repo = {"repo": "git.example.com/team/widget"}
    before = head(world.cfg.clone)
    r = world.api.post("/propose", headers=world.agent,
                       json=handoff_fact({"key": repo["repo"]}, "Retry done; backoff next.", 1_790_000_000.0))
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert world.api.get("/handoff", headers=world.agent, params=repo).json()["handoff"]["status"] == "pending"
    for decision in ("approve", "reject"):
        assert _refused(_decide(agent_session, cid, decision), SECOND), decision
    assert head(world.cfg.clone) == before
    assert world.note(_decide(cobalt, cid, "approve")).body == "Retry done; backoff next."
    assert world.api.get("/handoff", headers=world.agent, params=repo).json()["handoff"]["status"] == "approved"


# --------------------------------------------------------------------------- four-eyes: who decides


def test_under_four_eyes_the_proposers_account_decides_nothing_of_its_own(world):
    _policy(world, "amber", "four-eyes")
    cobalt = world.member("cobalt", {"amber": REVIEW_ONLY})
    session, theirs = world.session(cobalt), world.token(cobalt["id"], review=True)
    agent_session = _signed_in(world, world.agent_user, "agent-user")
    reviewing = world.token(world.agent_user, scope="write", review=True)       # agent-user's second token
    first = _queued(world.api.post("/save", headers=world.agent, json={"body": GARAGE}))
    second = _queued(world.api.post("/save", headers=world.agent, json={"body": PRINTER}))
    before = head(world.cfg.clone)
    # By session or by its own reviewing token, the account that proposed them gets neither decision.
    assert agent_session.get("/inbox").json()["total"] == 2
    for cid in (first, second):
        for decision in ("approve", "reject"):
            assert _refused(_decide(agent_session, cid, decision), SECOND), decision
            assert _refused(_decide(world.api, cid, decision, bearer=reviewing), SECOND), decision
    assert head(world.cfg.clone) == before
    assert world.admin.get("/inbox").json()["counts"] == {"pending": 2, "approved": 0, "rejected": 0}
    # A different account does, by session and by token.
    note = world.note(_decide(session, first, "approve"))
    assert (note.body, note.saved_by) == (GARAGE, "laptop-agent")
    assert _decide(world.api, second, "reject", bearer=theirs, reason="not true").status_code == 200
    # An administrator is an account like any other: what it proposed, it does not decide.
    own = _queued(world.admin.post("/save", json={"body": NAS}))
    admins = world.token(world.owner, review=True)
    for decision in ("approve", "reject"):
        assert _refused(_decide(world.admin, own, decision), SECOND), decision
        assert _refused(_decide(world.api, own, decision, bearer=admins), SECOND), decision
    assert inbox.get(world.cfg, "amber", own)["status"] == "pending"
    assert world.note(_decide(session, own, "approve")).saved_by == "owner"
    assert _stored_ids(world.cfg, own) == (world.owner, "session", cobalt["id"])


def test_under_four_eyes_no_reviewer_changes_a_candidates_text(world, monkeypatch):
    _policy(world, "amber", "four-eyes")
    writer = world.member("writer", {"amber": "write"})          # write and review
    session, token = world.session(writer), world.token(writer["id"], scope="write", review=True)
    cid = _queued(world.api.post("/save", headers=world.agent, json={
        "title": "Garage door", "body": GARAGE, "host": "vmhost", "tags": ["network"]}))
    before = head(world.cfg.clone)
    # Elsewhere this lifts a token's limits. Here it lifts nothing.
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "all")
    reviewers = {"a member who holds write": (session, None), "an administrator": (world.admin, None),
                 "a token under all": (world.api, token)}
    for edits in ({"body": "The reviewer's own words."}, {"title": "Garage door opener"},
                  {"slug": "repo-hosting-policy"}, {"supersedes": "repo-hosting-policy"}, {"host": None},
                  {"tags": ["door"], "description": "Rewritten."}):
        for who, (client, bearer) in reviewers.items():
            r = _decide(client, cid, "approve", bearer=bearer, edits=edits)
            assert _refused(r, TEXT), (who, edits, r.text)
    item = world.admin.get(f"/inbox/{cid}").json()
    assert (item["status"], item["title"], item["body"], item["last_error"]) == ("pending", "Garage door", GARAGE, None)
    assert head(world.cfg.clone) == before
    # Tags and importance stay the reviewer's to change.
    note = world.note(_decide(world.api, cid, "approve", bearer=token, edits={"tags": ["door"], "importance": 2}))
    assert (note.title, note.body, note.host, note.tags, note.importance, note.saved_by) == \
        ("Garage door", GARAGE, "vmhost", ["door"], 2, "laptop-agent")


def test_under_four_eyes_every_other_field_stays_as_proposed_also_when_an_edit_would_remove_it(world):
    _policy(world, "amber", "four-eyes")
    session = world.session(world.member("writer", {"amber": "write"}))          # write and review
    cid = _queued(world.api.post("/save", headers=world.agent, json={
        "title": "Garage door", "body": GARAGE, "host": "vmhost", "tags": ["network"], "importance": 4,
        "source": "runbook", "description": "Where the opener is.", "pinned": True, "observed_at": "2026-01-05",
        "volatility": "state"}))
    before = head(world.cfg.clone)
    for edits in ({"source": "elsewhere"}, {"source": None}, {"pinned": False}, {"pinned": None},
                  {"description": None}, {"host": "lapbox"}, {"observed_at": None}, {"verified_at": "2026-02-02"},
                  {"volatility": "durable"}, {"volatility": None}, {"verify": []}, {"expected_revision": "0" * 40},
                  {"title": None}, {"importance": 1, "source": None}):
        for client in (session, world.admin):
            r = _decide(client, cid, "approve", edits=edits)
            assert _refused(r, TEXT), (edits, r.text)
    assert head(world.cfg.clone) == before and world.admin.get(f"/inbox/{cid}").json()["status"] == "pending"
    # Removing its tags and importance changes those two and nothing else.
    note = world.note(_decide(session, cid, "approve", edits={"tags": None, "importance": None}))
    assert (note.title, note.body, note.host, note.source, note.description, note.pinned, str(note.observed_at),
            note.volatility) == ("Garage door", GARAGE, "vmhost", "runbook", "Where the opener is.", True,
                                 "2026-01-05", "state")
    assert (note.tags, note.importance, note.saved_by) == ([], 3, "laptop-agent")


def test_a_reviewer_without_an_account_decides_nothing_once_its_store_is_four_eyes(store, monkeypatch):  # noqa: F811
    # Built while the store was open, when the switch let a bearer without an account review it.
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "all")
    cid = inbox.propose(dict(FACT), "amber", cfg=store, proposer="mem-import")["id"]
    reviewer = _as_request(lambda: inbox.reviewer_for("amber"), label="ci")
    assert (reviewer.account, reviewer.is_token, reviewer.operator) == (None, True, False)
    monkeypatch.setenv("MEMD_AMBER_REVIEW_POLICY", "four-eyes")
    before = head(store.clone)
    for decide in (inbox.approve, inbox.reject):
        with pytest.raises(inbox.PolicyRefusal, match=ACCOUNT):
            decide(store, "amber", cid, reviewer=reviewer)
    assert head(store.clone) == before and inbox.get(store, "amber", cid)["status"] == "pending"
    # Open again, the same reviewer decides it as before.
    monkeypatch.delenv("MEMD_AMBER_REVIEW_POLICY")
    assert inbox.reject(store, "amber", cid, reviewer=reviewer)["status"] == "rejected"


def test_the_token_review_switch_gives_no_token_a_say_in_a_four_eyes_store(world, monkeypatch):
    assert world.grant(world.agent_user, {"amber": "write", "team": "write"}).status_code == 200
    plain = world.token(world.agent_user, scope="write", extra_stores={"team": "write"})   # issued without review
    _policy(world, "team", "four-eyes")
    waiting = _queued(world.admin.post("/save", json={"profile": "team", "body": GARAGE}))
    in_amber = world.propose(PRINTER, LEGACY)
    for mode in ("others", "all"):
        monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", mode)
        # In the open store the switch works as it always has ...
        assert world.api.get("/inbox", headers=plain).json()["total"] == 1
        assert world.api.get("/inbox", headers=LEGACY).json()["total"] == 1
        # ... and in the four-eyes store it lets the same write token do nothing.
        for method, path, body in _routes(waiting, "team"):
            r = world.api.request(method, path, headers=plain, json=body)
            assert _refused(r, ACCOUNT), (mode, path, r.text)
    # The served store too: a legacy token has no account at all.
    _policy(world, "amber", "four-eyes")
    for mode in ("others", "all"):
        monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", mode)
        for bearer in (LEGACY, plain, world.agent):
            for method, path, body in _routes(in_amber, "amber"):
                r = world.api.request(method, path, headers=bearer, json=body)
                assert _refused(r, ACCOUNT), (mode, path, r.text)
            page = world.api.get("/entities/host/vmhost?fresh=true", headers=bearer).json()["inbox"]
            assert page["can_review"] is False and page["items"] is None
    assert inbox.get(world.cfg, "amber", in_amber)["status"] == "pending"
    assert inbox.get(share._cfg("team"), "team", waiting)["status"] == "pending"
    # all lifts neither limit for a token that does review: not its own account's candidate, not the text.
    reviewing = world.token(world.agent_user, scope="write", review=True, extra_stores={"team": "write"})
    own = _queued(world.api.post("/save", headers=plain, json={"profile": "team", "body": NAS}))
    for decision in ("approve", "reject"):
        assert _refused(_decide(world.api, own, decision, "team", bearer=reviewing), SECOND), decision
    r = _decide(world.api, waiting, "approve", "team", bearer=reviewing, edits={"body": "Rewritten."})
    assert _refused(r, TEXT)
    assert _decide(world.api, waiting, "approve", "team", bearer=reviewing).status_code == 200


def test_a_remote_candidate_without_an_account_is_the_operators_and_the_operator_decides_anything(world, capsys):
    _policy(world, "amber", "four-eyes")
    cobalt = world.member("cobalt", {"amber": REVIEW_ONLY})
    reviewing = world.token(cobalt["id"], review=True)            # its label is "agent"
    # Filed by a legacy token, and by a local caller whose label is the reviewing token's.
    by_legacy = _queued(world.api.post("/save", headers=LEGACY, json={"body": GARAGE}))
    local = inbox.propose({"body": PRINTER}, "amber", cfg=world.cfg, proposer="agent")["id"]
    assert _stored_ids(world.cfg, by_legacy)[0] is None and _stored_ids(world.cfg, by_legacy)[1] is not None
    assert _stored_ids(world.cfg, local) == (None, None, None)
    # A remote credential that belongs to no account: whoever holds it may hold a reviewing
    # account too, so no account can be shown to be a second one. It waits for the operator.
    before = head(world.cfg.clone)
    for client, bearer in ((world.admin, None), (world.api, reviewing)):
        for decision in ("approve", "reject"):
            r = _decide(client, by_legacy, decision, bearer=bearer)
            assert r.status_code == 403 and "The operator decides it" in r.json()["detail"], r.text
    assert head(world.cfg.clone) == before and inbox.get(world.cfg, "amber", by_legacy)["status"] == "pending"
    by_operator = inbox.approve(world.cfg, "amber", by_legacy, reviewer=inbox.operator_reviewer("cli:amber"))
    assert read_note(world.cfg.clone, by_operator["receipt"]["slug"]).saved_by == "legacy"
    # Filed on the server, with no ids at all: any reviewer with an account decides.
    assert _decide(world.api, local, "reject", bearer=reviewing).status_code == 200
    # The operator has the store on disk. It decides what the policy would refuse anyone else,
    # corrects the text, and its own saves never wait.
    own = _queued(world.admin.post("/save", json={"title": "NAS", "body": NAS}))
    other = _queued(world.api.post("/save", headers=world.agent, json={"body": "The router is in the hall cupboard."}))
    operator = inbox.operator_reviewer("cli:amber")
    out = inbox.approve(world.cfg, "amber", own, reviewer=operator,
                        edits={"title": "Archive NAS", "body": NAS + " Read-only."})
    assert out["edited"] is True and read_note(world.cfg.clone, out["receipt"]["slug"]).title == "Archive NAS"
    assert inbox.reject(world.cfg, "amber", other, reviewer=operator)["status"] == "rejected"
    assert inbox.save_routes_to_inbox("amber") is False
    # So with the local CLI's publish: into a four-eyes store, with publish_review off, it is written.
    _policy(world, "team", "four-eyes", publish_review=False)
    capsys.readouterr()
    assert share.main(["publish", "vmhost-proxmox-vm", "team", "--from", "amber", "--json"]) == 0
    by_cli = json.loads(capsys.readouterr().out)
    assert (by_cli["action"], by_cli["queued"]) == ("published", False)
    assert [n.slug for n in list_notes(_clone_of("team"))] == ["vmhost-proxmox-vm"]


def test_without_accounts_a_four_eyes_store_is_the_operators_to_review(store, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MEMD_AMBER_REVIEW_POLICY", "four_eyes")
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "all")
    before = head(store.clone)
    with TestClient(server_mod.create_token_app()) as client:
        cid = _queued(client.post("/save", headers=AUTH, json=FACT))            # MEMD_SAVE_MODE is not set
        out = _mcp(client, "save", {"title": "Printer", "body": PRINTER})
        assert out["isError"] is False and out["structuredContent"]["queued"] is True
        assert client.get("/ui/me", headers=AUTH).json()["stores"] == [
            {"id": "amber", "name": "amber", "permission": "write", "review": False, "review_policy": "four-eyes"}]
        for method, path, body in _routes(cid, "amber"):
            r = client.request(method, path, headers=AUTH, json=body)
            assert _refused(r, ACCOUNT), (path, r.text)
    assert head(store.clone) == before and read_note(store.clone, "backup-window") is None
    assert inbox.main(["approve", cid, "--title", "Archive backup window"]) == 0
    assert read_note(store.clone, "archive-backup-window").body == FACT["body"]


def test_a_registry_token_saves_into_a_four_eyes_store_and_never_reviews_it(store, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MEMD_CONTROL_DB", str(tmp_path / "control" / "control.db"))
    monkeypatch.setenv("MEMD_AMBER_REVIEW_POLICY", "four-eyes")
    tokens = registry.Registry()
    tokens.initialize()
    made = tokens.issue(actor="operator", operation_id=str(uuid.uuid4()), label="ci", owner="team",
                        purpose="review", stores=["amber"], operations=["read", "save"])
    bearer = {"Authorization": "Bearer " + made["secret"]}
    before = head(store.clone)
    for mode in ("others", "all"):
        monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", mode)
        with TestClient(server_mod.create_token_app()) as client:
            cid = _queued(client.post("/save", headers=bearer, json={"body": f"{GARAGE} Seen with {mode}."}))
            for method, path, body in _routes(cid, "amber"):
                r = client.request(method, path, headers=bearer, json=body)
                assert _refused(r, ACCOUNT), (mode, path, r.text)
            shown = client.get("/entities/host/vmhost?fresh=true", headers=bearer).json()["inbox"]
            assert shown["can_review"] is False and shown["items"] is None
        assert _stored_ids(store, cid) == (None, made["token"]["id"], None)
    assert head(store.clone) == before and inbox.list_candidates(store, "amber")["counts"]["pending"] == 2


# --------------------------------------------------------------------------- four-eyes: the console


def test_the_console_edits_retracts_and_decides_nothing_of_its_own_in_a_four_eyes_store(
        userweb, key, directory, stores, monkeypatch):  # noqa: F811
    client, tokens, _ = userweb
    monkeypatch.setattr(save_mod, "_vector_near_matches", lambda cfg, body: [])
    monkeypatch.setenv("MEMD_ALICE_EXAMPLE_INVALID_REVIEW_POLICY", "four-eyes")
    cfg = stores["alice@example.invalid"]
    issued = tokens.issue_personal(subject="alice-sub", store="alice@example.invalid", label="laptop",
                                   operation_id=str(uuid.uuid4()))
    # Her own token's save waits, and is her account's.
    own = _queued(client.post("/save", headers={"Authorization": "Bearer " + issued["secret"]},
                              json={"title": "Desk fan", "body": "The desk fan is on plug 4."}))
    # A candidate with no account: filed on the server by a local caller.
    bind_identity("alice@example.invalid")
    try:
        local = inbox.propose({"title": "Desk lamp", "body": "The desk lamp is on plug 3."}, cfg.profile,
                              cfg=user_memory.owner_config(), proposer="mem-import")["id"]
    finally:
        clear_identity()
    before = head(cfg.clone)
    with respx.mock(assert_all_called=False) as router:
        assert sign_in(client, router, key)[0].status_code == 303
        h = headers(client, tokens)
        note = client.get("/memories/api/note?slug=widget").json()
        edit = {"slug": "widget", "revision": note["revision"], "title": "Corrected", "body": "Changed.", "tags": []}
        for action, body in (("edit", edit), ("retract", {"slug": "widget", "revision": note["revision"]})):
            r = client.post(f"/memories/api/{action}", headers=h, json=body)
            assert r.status_code == 403 and "requires review" in r.json()["detail"], (action, r.text)
        assert client.get("/memories/api/note?slug=widget").json() == note
        # Its reviewer is her account: what her token proposed is not hers to decide.
        for decision, body in (("approve", {"edits": {}}), ("reject", {})):
            r = client.post(f"/memories/api/inbox/{own}/{decision}", headers=h, json=body)
            assert r.status_code == 403 and SECOND in r.json()["detail"], (decision, r.text)
        # What no account proposed she may decide, changing tags and importance only.
        r = client.post(f"/memories/api/inbox/{local}/approve", headers=h,
                        json={"edits": {"body": "The desk lamp is on smart plug 3."}})
        assert r.status_code == 403 and TEXT in r.json()["detail"], r.text
        assert head(cfg.clone) == before
        r = client.post(f"/memories/api/inbox/{local}/approve", headers=h, json={"edits": {"tags": ["home"]}})
        assert r.status_code == 200, r.text
    lamp = read_note(cfg.clone, "desk-lamp")
    assert lamp.body == "The desk lamp is on plug 3." and lamp.tags == ["home"]
    assert read_note(cfg.clone, "desk-fan") is None
    # The policy is that store's alone: the store beside it is edited as before.
    bind_identity("bob@example.invalid")
    try:
        theirs = user_memory.detail("widget")
        out = user_memory.change("retract", {"slug": "widget", "revision": theirs["revision"]}, subject="bob-sub")
        assert out["saved"] is True
        monkeypatch.setenv("MEMD_BOB_EXAMPLE_INVALID_REVIEW_POLICY", "four-eyes")
        with pytest.raises(inbox.PolicyRefusal, match="requires review"):
            user_memory.change("retract", {"slug": "bob-only", "revision": user_memory.detail("bob-only")["revision"]},
                               subject="bob-sub")
    finally:
        clear_identity()


# --------------------------------------------------------------------------- one proposer, or two


def test_one_accounts_tokens_are_one_proposer_under_four_eyes_and_two_under_open(world):
    cobalt = world.member("cobalt", {"amber": "write"})
    first, first_id = _token(world, world.agent_user, label="shared", scope="write", review=True)
    second, second_id = _token(world, world.agent_user, label="shared", scope="write", review=True)
    other, other_id = _token(world, cobalt["id"], label="shared", scope="write", review=True)
    # Open: two tokens of one account are two proposers. Neither decides its own, each the other's.
    mine = world.propose("Proposed by the first token while the store is open.", first)
    assert _stored_ids(world.cfg, mine)[:2] == (world.agent_user, first_id)
    for decision in ("approve", "reject"):
        assert _refused(_decide(world.api, mine, decision, bearer=first), "own proposal"), decision
    assert _decide(world.api, mine, "approve", bearer=second).status_code == 200
    # Four-eyes: the same two are one proposer, so the second token gets neither decision ...
    _policy(world, "amber", "four-eyes")
    mine = _queued(world.api.post("/save", headers=first, json={"body": "Proposed by the first token under four-eyes."}))
    theirs = world.propose("Proposed by the other account's token.", other)
    assert _stored_ids(world.cfg, mine)[:2] == (world.agent_user, first_id)
    assert _stored_ids(world.cfg, theirs)[:2] == (cobalt["id"], other_id) and second_id != first_id
    for bearer in (first, second):
        for decision in ("approve", "reject"):
            assert _refused(_decide(world.api, mine, decision, bearer=bearer), SECOND), decision
    # ... while a token that shares their label, but not their account, is a different one.
    assert [i["proposer"] for i in world.admin.get("/inbox").json()["items"]] == ["shared", "shared"]
    assert world.note(_decide(world.api, mine, "approve", bearer=other)).saved_by == "shared"
    assert _refused(_decide(world.api, theirs, "reject", bearer=other), SECOND)
    assert _decide(world.api, theirs, "reject", bearer=second).status_code == 200


# --------------------------------------------------------------------------- two stores, two policies


def test_a_four_eyes_store_and_an_open_store_on_one_instance_keep_their_own_rules(world, monkeypatch):
    _policy(world, "team", "four-eyes", publish_review=False)
    writer = world.member("writer", {"amber": "write", "team": "write"})
    session = world.session(writer)
    token = world.token(writer["id"], scope="write", extra_stores={"team": "write"})
    for response in (session.get("/ui/me"), session.get("/ui/stores"),
                     world.api.get("/ui/me", headers=token), world.api.get("/ui/stores", headers=token)):
        assert {s["id"]: s["review_policy"] for s in response.json()["stores"]} == {"amber": "open", "team": "four-eyes"}
    # Saving: at once into amber, held in team, for the same session and the same token.
    _saved(session.post("/save", json={"title": "Printer", "body": PRINTER}))
    _saved(world.api.post("/save", headers=token, json={"title": "NAS", "body": NAS}))
    in_team = _queued(session.post("/save", json={"profile": "team", "title": "Printer", "body": PRINTER}))
    by_token = _queued(world.api.post("/save", headers=token, json={"profile": "team", "title": "NAS", "body": NAS}))
    assert list_notes(_clone_of("team")) == []
    # Deciding: in amber the session approves what it proposed itself and rewrites it; in team neither.
    in_amber = session.post("/propose", json={"title": "Garage door", "body": GARAGE}).json()["id"]
    theirs = _queued(world.admin.post("/save", json={"profile": "team", "title": "Garage door", "body": GARAGE}))
    for cid in (in_team, by_token):
        for decision in ("approve", "reject"):
            assert _refused(_decide(session, cid, decision, "team"), SECOND), decision
    assert _refused(_decide(session, theirs, "approve", "team", edits={"body": "Rewritten."}), TEXT)
    note = world.note(_decide(session, in_amber, "approve", edits={"body": GARAGE + " (VLAN 30)."}))
    assert note.body.endswith("(VLAN 30).") and note.saved_by == "writer"
    # A candidate of one store is not found in the other's inbox.
    assert _decide(session, theirs, "approve").status_code == 404
    # The switch still lets the write token review amber, and gives it nothing in team.
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "all")
    assert world.api.get("/inbox", headers=token).status_code == 200
    assert _refused(world.api.get("/inbox?profile=team", headers=token), ACCOUNT)
    # Opened again, team behaves as amber does: a session rejects its own candidate and saves directly.
    _policy(world, "team", "open")
    assert _decide(session, in_team, "reject", "team").status_code == 200
    _saved(session.post("/save", json={"profile": "team", "body": "Saved directly once the store is open."}))
    assert world.api.get("/inbox?profile=team", headers=token).json()["total"] == 2


# --------------------------------------------------------------------------- the setting itself


def test_the_policy_is_an_administrators_to_change_and_every_change_is_audited(world, monkeypatch):

    def changes(name="team"):
        with control.db() as c:
            return [tuple(r) for r in c.execute(
                "SELECT actor, action, target FROM audit WHERE action IN ('store.update', 'store.review_policy') "
                "AND (target=? OR target LIKE ?) ORDER BY id", (name, name + ":%"))]
    updated = ("owner", "store.update", "team")
    assert changes() == []
    _policy(world, "team", "four-eyes")
    assert changes() == [updated, ("owner", "store.review_policy", "team:open->four-eyes")]
    _policy(world, "team", "agents")
    assert changes()[2:] == [updated, ("owner", "store.review_policy", "team:four-eyes->agents")]
    # A save that leaves the policy as it was adds no such row: the same choice again, another setting.
    _policy(world, "team", "agents")
    r = world.admin.put("/admin/stores/team", json={"name": "Team notes", "config": {"publish_review": False}})
    assert r.status_code == 200 and r.json()["store"]["review_policy"] == "agents", r.text
    assert changes()[4:] == [updated, updated]
    # Refused values change and record nothing.
    for bad in ("strict", "Four-Eyes", "four_eyes", "", None, True, ["open"]):
        r = world.admin.put("/admin/stores/team", json={"config": {"review_policy": bad}})
        assert r.status_code == 400 and "review_policy" in r.json()["detail"], (bad, r.text)
    assert len(changes()) == 6 and inbox.review_policy("team") == "agents"
    # The row names what the store was really doing before, where that came from the environment.
    monkeypatch.setenv("MEMD_AMBER_REVIEW_POLICY", "agents")
    _policy(world, "amber", "four-eyes")
    assert changes("amber") == [("owner", "store.update", "amber"),
                               ("owner", "store.review_policy", "amber:agents->four-eyes")]
    logged = [(a["actor"], a["action"], a["target"]) for a in world.overview()["audit"]]
    assert ("owner", "store.review_policy", "team:four-eyes->agents") in logged
    # Nobody but an administrator's session sets it, on an existing store or a new one.
    member = world.session(world.member("cobalt", {"amber": "write", "team": "write"}))
    admins_token = world.token(world.owner, store_id="team", scope="write", review=True)
    for who, client, bearer in (("a member", member, None), ("an administrator's token", world.api, admins_token),
                                ("a legacy token", world.api, LEGACY), ("nobody", world.api, None)):
        r = client.put("/admin/stores/team", headers=bearer, json={"config": {"review_policy": "open"}})
        assert r.status_code == (401 if who == "nobody" else 403) and r.json()["detail"], (who, r.text)
        r = client.post("/admin/stores", headers=bearer,
                        json={"id": "theirs", "kind": "local", "config": {"review_policy": "open"}})
        assert r.status_code == (401 if who == "nobody" else 403), (who, r.text)
    assert inbox.review_policy("team") == "agents" and control.store("theirs") is None
    assert len(changes()) == 6


# --------------------------------------------------------------------------- memd-doctor


def test_the_doctors_save_check_passes_a_saved_probe_and_a_queued_one_and_says_which(monkeypatch):
    def probe(receipt):
        doctor = load_script("doctor_review_policy", "maint/memd-doctor")
        monkeypatch.setattr(doctor, "read_token", lambda _: "test-token")
        monkeypatch.setattr(doctor, "mcp_call", lambda *a: (200, json.dumps(receipt)))
        return doctor, doctor.c5()
    doctor, (status, detail) = probe({"saved": True, "revision": "r", "lexical_indexed": True, "indexed": True,
                                      "synced": True, "action": "updated", "slug": "memd-doctor-probe"})
    assert status == "PASS" and detail.startswith("saved: action=updated ") and doctor.STATE["nonce_a"] in detail
    # The receipt a store answers with when its policy queues the doctor's token.
    queued = inbox.queued_receipt({"id": "0123456789abcdef", "duplicate": False,
                                   "lint": {"slug": "memd-doctor-probe"}}, "four-eyes")
    doctor, (status, detail) = probe(queued)
    assert status == "PASS" and detail.startswith("queued for review: candidate 0123456789abcdef")
    # Nothing was written, so the round trip has nothing to find: skipped, with the reason, not failed.
    assert "nonce_a" not in doctor.STATE
    status, detail = doctor.c6()
    assert status == "SKIP" and "waits for review" in detail
    # Neither saved nor queued is still a failure.
    for receipt in ({**queued, "inbox_id": ""}, {k: v for k, v in queued.items() if k != "inbox_id"},
                    {**queued, "queued": False}, {**queued, "queued": "yes"}, {**queued, "error": "refused"},
                    {**queued, "inbox_id": 7}, {"saved": False}, {}):
        assert probe(receipt)[1][0] == "FAIL", receipt


def test_the_doctor_fails_a_queued_probe_that_approval_would_save_beside_its_note(store, monkeypatch):  # noqa: F811
    doctor = load_script("doctor_queued_fork", "maint/memd-doctor")
    monkeypatch.setattr(doctor, "read_token", lambda _: "test-token")
    seconds = iter(range(1_790_000_000, 1_790_000_010))
    monkeypatch.setattr(doctor, "time", SimpleNamespace(time=lambda: next(seconds)))    # a new nonce for each probe

    def queued(name, arguments, token):
        # What a store answers when its policy queues the doctor's token: the lint of the real save path.
        proposed = inbox.propose(normalize_fact(arguments), "amber", cfg=store, source="save", proposer="doctor")
        return 200, json.dumps(inbox.queued_receipt(proposed, "agents"))
    monkeypatch.setattr(doctor, "mcp_call", queued)
    status, detail = doctor.c5()
    assert status == "PASS" and detail.startswith("queued for review: "), detail
    # The probe note was retired, so a save under its slug adds a note beside it: the fork a direct save fails on.
    save_mod.save({"slug": doctor.PROBE_SLUG, "body": "memd daily probe."}, "amber", cfg=store)
    save_mod.save({"title": "Probe retired", "body": "The daily probe moved elsewhere.",
                   "supersedes": doctor.PROBE_SLUG}, "amber", cfg=store)
    doctor.STATE.clear()
    status, detail = doctor.c5()
    assert status == "FAIL" and doctor.PROBE_SLUG in detail and "queued" not in doctor.STATE, detail
    # So does one whose lint could not run at all.
    monkeypatch.setattr(inbox, "lint", lambda fact, profile, *, cfg: {"error": "lint unavailable: unreadable store"})
    status, detail = doctor.c5()
    assert status == "FAIL" and "queued" not in doctor.STATE, detail


def test_the_doctors_omp_read_back_is_skipped_only_where_the_probe_was_queued(monkeypatch):
    doctor = load_script("doctor_queued_omp", "maint/memd-doctor")
    doctor.STATE.update(token="test-token", c11_ok=True, nonce_b="omp-1790000000")
    monkeypatch.setattr(doctor, "mcp_call", lambda *a: (200, "No matching memory notes."))
    # A save that was to be written and cannot be found is a failure, as before.
    assert doctor.c12()[0] == "FAIL"
    # The store queued the doctor's own probe, so it queued omp's save into it as well.
    doctor.STATE["queued"] = "0123456789abcdef"
    status, detail = doctor.c12()
    assert status == "SKIP" and "waits for review" in detail
    # A server that does not answer is not excused by that, and a nonce that is there still passes.
    monkeypatch.setattr(doctor, "mcp_call", lambda *a: (503, "unavailable"))
    assert doctor.c12()[0] == "FAIL"
    monkeypatch.setattr(doctor, "mcp_call", lambda *a: (200, "Recalled memory: nonce omp-1790000000"))
    assert doctor.c12()[0] == "PASS"
