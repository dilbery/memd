"""Review as its own per-store capability beside read and write.

Driven through the REST surface, and through MCP where a tool exists
(memd.access, memd.inbox, memd.control, memd.admin_api). Hermetic: temp
Git/SQLite stores behind the administered access layer, no model backends; the
socket guard blocks everything else.
"""
import json
from pathlib import Path
import re
import sqlite3
import types
import uuid

import pytest
from fastapi.testclient import TestClient

import memd.server as server_mod
from memd import access, control, inbox, registry
from memd.store import read_note
from tests.test_inbox import _as_request, _mcp, accounts, head, store  # noqa: F401  (fixtures)

MCP = {"Accept": "application/json, text/event-stream"}
LEGACY = {"Authorization": "Bearer test-token"}
REVIEW_ONLY = {"permission": "read", "review": True}
WRITE_ONLY = {"permission": "write", "review": False}


class World:
    """The accounts fixture: an administrator (owner), the serving store amber, and
    laptop-agent, a token of agent-user that may write amber and was issued without review."""

    def __init__(self, fixture):
        self.admin, self.app, csrf, agent, self.cfg = fixture
        self.csrf = {"X-CSRF-Token": csrf}
        self.agent = {"Authorization": "Bearer " + agent}
        self.api = TestClient(self.app)
        with control.db() as c:
            self.owner = c.execute("SELECT id FROM users WHERE username='owner'").fetchone()[0]
            self.agent_user = c.execute("SELECT id FROM users WHERE username='agent-user'").fetchone()[0]

    def store(self, name):
        r = self.admin.post("/admin/stores", headers=self.csrf, json={"id": name, "name": name, "kind": "local"})
        assert r.status_code == 200, r.text

    def grant(self, uid, grants):
        return self.admin.put(f"/admin/users/{uid}/grants", headers=self.csrf, json={"grants": grants})

    def member(self, username, grants):
        made = self.admin.post("/admin/users", headers=self.csrf, json={"username": username}).json()
        assert self.grant(made["id"], grants).status_code == 200
        return {"id": made["id"], "username": username, "password": made["temporary_password"]}

    def session(self, user):
        client = TestClient(self.app)
        login = client.post("/ui/login", json={"username": user["username"], "password": user["password"]})
        assert login.status_code == 200, login.text
        client.headers["X-CSRF-Token"] = login.json()["csrf"]
        return client

    def issue(self, uid, **fields):
        return self.admin.post("/admin/tokens", headers=self.csrf, json={
            "label": "agent", "user_id": uid, "store_id": "amber", "scope": "read", **fields})

    def token(self, uid, **fields):
        r = self.issue(uid, **fields)
        assert r.status_code == 200, r.text
        return {"Authorization": "Bearer " + r.json()["token"]}

    def propose(self, body, headers=None, **fields):
        r = self.api.post("/propose", headers=headers or self.agent, json={"body": body, **fields})
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def overview(self):
        r = self.admin.get("/admin/overview")
        assert r.status_code == 200, r.text
        return r.json()

    def note(self, response):
        assert response.status_code == 200, response.text
        return read_note(self.cfg.clone, response.json()["receipt"]["slug"])


@pytest.fixture
def world(accounts):  # noqa: F811
    return World(accounts)


def _inbox_routes(cid):
    return (("get", "/inbox"), ("get", f"/inbox/{cid}"),
            ("post", f"/inbox/{cid}/approve"), ("post", f"/inbox/{cid}/reject"))


def _refused(response, *words):
    """A refusal is a 403 whose detail says why."""
    detail = response.json().get("detail") if response.status_code == 403 else None
    return isinstance(detail, str) and bool(detail) and all(word in detail for word in words)


# --------------------------------------------------------------------------- review is not write


def test_review_without_write_decides_candidates_and_writes_nothing_of_its_own(world):
    world.store("team")
    cobalt = world.member("cobalt", {"amber": REVIEW_ONLY, "team": REVIEW_ONLY})
    session = world.session(cobalt)
    first = world.propose("The garage door opener is on the IoT VLAN.", title="Garage door")
    second = world.propose("The office printer is on the guest VLAN.")
    listed = session.get("/inbox").json()
    assert listed["total"] == 2 and listed["profile"] == "amber"
    assert session.get(f"/inbox/{first}").json()["status"] == "pending"
    assert session.get("/entities/host/vmhost").json()["inbox"]["can_review"] is True
    before = head(world.cfg.clone)
    # Every way of writing into the store is refused to a reviewer who may not write ...
    for path, body, why in (("/save", {"body": "Saved by the reviewer."}, "required store access"),
                            ("/save", {"profile": "team", "body": "Saved by the reviewer."}, "required store access"),
                            ("/propose", {"body": "Proposed by the reviewer."}, "required store access"),
                            ("/publish", {"slug": "repo-hosting-policy", "target_store": "team"}, "may write")):
        assert _refused(session.post(path, json=body), why), path
    # ... and so is changing the text someone else proposed.
    for edits in ({"title": "Garage door opener"}, {"body": "The reviewer's own words."},
                  {"slug": "repo-hosting-policy"}, {"supersedes": "repo-hosting-policy"},
                  {"tags": ["network"], "title": "Garage door opener"}):
        r = session.post(f"/inbox/{first}/approve", json={"edits": edits})
        assert _refused(r, "importance and tags"), (edits, r.text)
    assert head(world.cfg.clone) == before
    assert session.get("/inbox").json()["total"] == 2 and session.get("/inbox?profile=team").json()["total"] == 0
    note = world.note(session.post(f"/inbox/{first}/approve",
                                   json={"edits": {"tags": ["network"], "importance": 2, "title": "Garage door"}}))
    assert (note.tags, note.importance, note.title) == (["network"], 2, "Garage door")
    assert note.body == "The garage door opener is on the IoT VLAN." and note.saved_by == "laptop-agent"
    assert session.post(f"/inbox/{second}/reject", json={"reason": "not ours"}).status_code == 200
    assert session.get("/inbox?status=all").json()["counts"] == {"pending": 0, "approved": 1, "rejected": 1}
    # The same through MCP, with a token that carries the member's review and nothing more.
    token = {**world.token(cobalt["id"], review=True, extra_stores={"team": "read"}), **MCP}
    for name, arguments in (("save", {"body": "Saved through MCP."}),
                            ("propose", {"body": "Proposed through MCP."}),
                            ("publish", {"slug": "repo-hosting-policy", "target_store": "team"})):
        assert _mcp(world.api, name, arguments, headers=token)["isError"] is True, name
    assert session.get("/inbox").json()["total"] == 0 and session.get("/inbox?profile=team").json()["total"] == 0


def test_write_without_review_saves_and_proposes_and_reviews_nothing(world):
    writer = world.member("writer", {"amber": WRITE_ONLY})
    session = world.session(writer)
    saved = session.post("/save", json={"title": "Printer", "body": "The office printer is on the guest VLAN."})
    assert saved.status_code == 200 and saved.json()["saved"] is True, saved.text
    proposed = session.post("/propose", json={"body": "The garage door opener is on the IoT VLAN."})
    assert proposed.status_code == 200, proposed.text
    cid = proposed.json()["id"]
    for method, path in _inbox_routes(cid):
        assert _refused(getattr(session, method)(path), "review access"), path
    assert session.get("/entities/host/vmhost").json()["inbox"]["can_review"] is False
    # Its tokens write as it does, and review no more than it does.
    token = world.token(writer["id"], scope="write")
    assert _mcp(world.api, "save", {"body": "Saved through MCP."}, headers={**token, **MCP})["isError"] is False
    out = _mcp(world.api, "propose", {"body": "Proposed through MCP."}, headers={**token, **MCP})
    assert out["isError"] is False
    for method, path in _inbox_routes(out["structuredContent"]["id"]):
        assert _refused(getattr(world.api, method)(path, headers=token)), path
    assert inbox.list_candidates(world.cfg, "amber")["counts"] == {"pending": 2, "approved": 0, "rejected": 0}


# --------------------------------------------------------------------------- the approval write


def test_a_review_only_approval_is_one_write_for_the_proposer(world, monkeypatch):
    world.store("team")
    session = world.session(world.member("cobalt", {"amber": REVIEW_ONLY, "team": REVIEW_ONLY}))
    seen = []
    approve, save = inbox.approve, server_mod._core_save

    def may_write(store):
        try:
            return access.authorize(store, write=True) == store
        except PermissionError:
            return False

    def watched_approve(*args, **kwargs):
        seen.append(("before", access.approved_write.get(), may_write("amber")))
        try:
            return approve(*args, **kwargs)
        finally:
            seen.append(("after", access.approved_write.get(), may_write("amber")))

    def watched_save(fact, profile="amber"):
        # Inside the approval: this store may be written, and no other the reviewer reads.
        seen.append(("save", access.approved_write.get(), may_write("amber"), may_write("team")))
        return save(fact, profile=profile)
    monkeypatch.setattr(inbox, "approve", watched_approve)
    monkeypatch.setattr(server_mod, "_core_save", watched_save)
    good = world.propose("The garage door opener is on the IoT VLAN.")
    failing = world.propose("Replace a missing note.", title="Retire", supersedes="no-such-note")
    expected = [("before", None, False), ("save", "amber", True, False), ("after", None, False)]

    note = world.note(session.post(f"/inbox/{good}/approve", json={}))
    assert note.saved_by == "laptop-agent" and note.body == "The garage door opener is on the IoT VLAN."
    assert seen == expected
    assert _refused(session.post("/save", json={"body": "Saved by the reviewer right after."}))

    del seen[:]
    r = session.post(f"/inbox/{failing}/approve", json={})
    assert r.status_code == 400 and "supersedes" in r.json()["detail"], r.text
    assert seen == expected
    item = session.get(f"/inbox/{failing}").json()
    assert item["status"] == "pending" and "supersedes" in item["last_error"]
    assert _refused(session.post("/save", json={"body": "Saved by the reviewer after a failed approval."}))


def test_the_approved_write_needs_a_grant_and_names_one_store():
    reader = access.Principal("u-1", "cobalt", False, {"amber": "read", "team": "read"}, "amber", session=True,
                              reviews=frozenset({"amber"}))

    def write(store, approved):
        def run():
            access.approved_write.set(approved)
            return access.authorize(store, write=True)
        return _as_request(run, label="cobalt", principal=reader)
    assert access.approved_write.get() is None
    assert write("amber", "amber") == "amber" and write(None, "amber") == "amber"
    for store, approved in (("amber", None), ("amber", "team"), ("team", "amber"), (None, "team"),
                            ("other", "other"), ("amber", "")):
        with pytest.raises(PermissionError):
            write(store, approved)
    assert access.approved_write.get() is None


def test_only_the_approval_names_a_store_it_may_write():
    package = Path(inbox.__file__).parent
    setters = {path.relative_to(package).as_posix(): len(re.findall(r"approved_write\s*\.\s*set\(", path.read_text()))
               for path in package.rglob("*.py")}
    assert {name: count for name, count in setters.items() if count} == {"inbox.py": 1}


def test_a_reviewer_decides_only_in_the_store_it_was_built_for(world):
    world.store("team")
    principal = access.Principal("u-1", "cobalt", False, {"amber": "read", "team": "write"}, "amber", session=True,
                                 reviews=frozenset({"amber", "team"}))
    cid = world.propose("The garage door opener is on the IoT VLAN.")

    def as_cobalt(fn):
        return _as_request(fn, label="cobalt", principal=principal)
    for_team, for_amber = as_cobalt(lambda: inbox.reviewer_for("team")), as_cobalt(lambda: inbox.reviewer_for("amber"))
    assert (for_team.store, for_team.can_write) == ("team", True)
    assert (for_amber.store, for_amber.can_write) == ("amber", False)
    for decide in (inbox.approve, inbox.reject):
        with pytest.raises(PermissionError, match="another store"):
            as_cobalt(lambda: decide(world.cfg, "amber", cid, reviewer=for_team))
    assert inbox.get(world.cfg, "amber", cid)["status"] == "pending"
    # Write on team does not make the amber reviewer a writer of amber.
    with pytest.raises(PermissionError, match="importance and tags"):
        as_cobalt(lambda: inbox.approve(world.cfg, "amber", cid, reviewer=for_amber, edits={"body": "Rewritten."}))
    out = as_cobalt(lambda: inbox.approve(world.cfg, "amber", cid, reviewer=for_amber))
    assert out["receipt"]["saved"] is True
    # The operator was built for no store in particular.
    other = world.propose("The office printer is on the guest VLAN.")
    assert inbox.operator_reviewer("cli:amber").store is None
    assert inbox.reject(world.cfg, "amber", other, reviewer=inbox.operator_reviewer("cli:amber"))["status"] == "rejected"


# --------------------------------------------------------------------------- tokens


def test_a_tokens_review_never_exceeds_its_owners(world):
    world.store("team")
    cobalt = world.member("cobalt", {"amber": REVIEW_ONLY, "team": "read"})
    token = world.token(cobalt["id"], review=True, extra_stores={"team": "read"})
    cid = world.propose("The garage door opener is on the IoT VLAN.")
    assert world.api.get("/inbox", headers=token).json()["total"] == 1
    # It reaches team, where its owner may not review.
    assert _refused(world.api.get("/inbox?profile=team", headers=token))
    reach = {s["id"]: (s["permission"], s["review"]) for s in world.api.get("/ui/me", headers=token).json()["stores"]}
    assert reach == {"amber": ("read", True), "team": ("read", False)}
    # Removing the owner's review removes the token's at the next request ...
    assert world.grant(cobalt["id"], {"amber": "read", "team": "read"}).status_code == 200
    for method, path in _inbox_routes(cid):
        assert _refused(getattr(world.api, method)(path, headers=token)), path
    assert inbox.get(world.cfg, "amber", cid)["status"] == "pending"
    # ... and giving it back restores it, with the same token.
    assert world.grant(cobalt["id"], {"amber": REVIEW_ONLY, "team": "read"}).status_code == 200
    note = world.note(world.api.post(f"/inbox/{cid}/approve", headers=token, json={"edits": {"importance": 2}}))
    assert note.saved_by == "laptop-agent" and note.importance == 2
    assert _refused(world.api.post("/save", headers=token, json={"body": "Saved by the reviewing token."}))


def test_a_reviewing_token_needs_an_owner_who_reviews_one_of_its_stores(world):
    world.store("team")
    writer = world.member("writer", {"amber": WRITE_ONLY, "team": "read"})
    issued = len(world.overview()["tokens"])
    r = world.issue(writer["id"], scope="write", review=True, extra_stores={"team": "read"})
    assert r.status_code == 400 and "review" in r.json()["detail"], r.text
    with pytest.raises(ValueError, match="review"):
        control.issue_token("agent", writer["id"], "amber", "write", None, actor="test", review=True)
    for not_a_boolean in ("yes", 1, None):
        assert world.issue(writer["id"], review=not_a_boolean).status_code == 400
    assert len(world.overview()["tokens"]) == issued
    # Review on an additional store is enough, and that is where the token reviews.
    assert world.grant(writer["id"], {"amber": WRITE_ONLY, "team": REVIEW_ONLY}).status_code == 200
    token = world.token(writer["id"], scope="write", review=True, extra_stores={"team": "read"})
    assert world.api.get("/inbox?profile=team", headers=token).status_code == 200
    assert _refused(world.api.get("/inbox", headers=token))
    # An administrator reviews every store, and so may a token of theirs.
    admins = world.token(world.owner, store_id="team", review=True)
    assert world.api.get("/inbox", headers=admins).json()["profile"] == "team"
    assert _refused(world.api.get("/inbox?profile=amber", headers=admins))      # the token reaches team only


def test_a_token_without_review_follows_the_switch_as_before(world, monkeypatch):
    second = world.token(world.agent_user, scope="write")       # agent-user's other token, also without review
    reader = world.token(world.agent_user)
    mine = world.propose("Proposed by laptop-agent.")
    theirs = world.propose("Proposed by the second token.", second)
    # off, the default: its owner may review, the token may not.
    for headers in (world.agent, second, reader, LEGACY):
        for method, path in _inbox_routes(mine):
            assert _refused(getattr(world.api, method)(path, headers=headers), "Agent tokens cannot review"), path
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "others")
    assert world.api.get("/inbox", headers=world.agent).json()["total"] == 2
    assert world.api.get("/inbox", headers=LEGACY).json()["total"] == 2
    assert _refused(world.api.get("/inbox", headers=reader))              # the switch still asks for write
    for decision in ("approve", "reject"):
        assert _refused(world.api.post(f"/inbox/{mine}/{decision}", headers=world.agent, json={}), "own proposal")
    r = world.api.post(f"/inbox/{theirs}/approve", headers=world.agent, json={"edits": {"body": "Rewritten."}})
    assert _refused(r, "importance and tags")
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "all")
    own = world.note(world.api.post(f"/inbox/{mine}/approve", headers=world.agent, json={}))
    assert own.saved_by == "laptop-agent"
    rewritten = world.note(world.api.post(f"/inbox/{theirs}/approve", headers=world.agent,
                                          json={"edits": {"body": "Rewritten by laptop-agent."}}))
    assert rewritten.body == "Rewritten by laptop-agent." and rewritten.saved_by == "laptop-agent"


def test_a_reviewing_token_keeps_the_limits_of_a_token(world, monkeypatch):
    # agent-user was granted "write", which is write and review.
    token = world.token(world.agent_user, scope="write", review=True)
    reader = world.token(world.agent_user, review=True)
    own = world.propose("Proposed by the reviewing token.", token)
    theirs = world.propose("Proposed by laptop-agent.")
    third = world.propose("Also proposed by laptop-agent.")
    for decision in ("approve", "reject"):
        assert _refused(world.api.post(f"/inbox/{own}/{decision}", headers=token, json={}), "own proposal")
    r = world.api.post(f"/inbox/{theirs}/approve", headers=token, json={"edits": {"title": "Rewritten"}})
    assert _refused(r, "importance and tags")
    note = world.note(world.api.post(f"/inbox/{theirs}/approve", headers=token, json={"edits": {"tags": ["network"]}}))
    assert note.tags == ["network"] and note.saved_by == "laptop-agent"
    # MEMD_INBOX_TOKEN_REVIEW=all lifts both limits, as for any token; it does not stand in for write.
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "all")
    r = world.api.post(f"/inbox/{third}/approve", headers=reader, json={"edits": {"body": "Rewritten."}})
    assert _refused(r, "importance and tags")
    assert world.note(world.api.post(f"/inbox/{third}/approve", headers=reader, json={})).saved_by == "laptop-agent"
    mine = world.note(world.api.post(f"/inbox/{own}/approve", headers=token, json={"edits": {"body": "Its own words."}}))
    assert mine.body == "Its own words." and mine.saved_by == "agent"


# --------------------------------------------------------------------------- upgrade


def test_an_upgraded_instance_behaves_as_before(world, monkeypatch):
    reader = world.member("reader", {"amber": "read"})
    second = world.token(world.agent_user, scope="write")
    with control.db() as c:      # the two tables as an earlier build left them
        c.execute("ALTER TABLE grants DROP COLUMN review")
        c.execute("ALTER TABLE tokens DROP COLUMN review")
    control.initialize()
    overview = world.overview()
    assert {g["user_id"]: (g["permission"], g["review"]) for g in overview["grants"]} == \
        {world.agent_user: ("write", True), reader["id"]: ("read", False)}
    assert not any(t["review"] for t in overview["tokens"])
    # A member who had write still reviews and corrects the text.
    reset = world.admin.post(f"/admin/users/{world.agent_user}/reset-password", headers=world.csrf).json()
    writer = world.session({"username": "agent-user", "password": reset["temporary_password"]})
    cid = world.propose("The garage door opener is on the IoT VLAN.")
    assert writer.get("/inbox").json()["total"] == 1
    note = world.note(writer.post(f"/inbox/{cid}/approve",
                                  json={"edits": {"body": "The garage door opener is on the IoT VLAN (VLAN 30)."}}))
    assert note.body.endswith("(VLAN 30).") and note.saved_by == "laptop-agent"
    assert writer.post("/save", json={"body": "Saved by the member."}).json()["saved"] is True
    # A member who had read still does not.
    assert _refused(world.session(reader).get("/inbox"))
    # Tokens review through the switch alone, as they did.
    mine, theirs = world.propose("Proposed by laptop-agent."), world.propose("Proposed by the second token.", second)
    assert _refused(world.api.get("/inbox", headers=world.agent), "Agent tokens cannot review")
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "others")
    assert _refused(world.api.post(f"/inbox/{mine}/approve", headers=world.agent, json={}), "own proposal")
    assert world.api.post(f"/inbox/{theirs}/reject", headers=world.agent, json={}).status_code == 200
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "all")
    assert world.note(world.api.post(f"/inbox/{mine}/approve", headers=world.agent,
                                     json={"edits": {"body": "Its own words."}})).body == "Its own words."


# --------------------------------------------------------------------------- the admin API


def test_the_grants_api_takes_the_earlier_strings_and_the_object_form(world):
    world.store("team")
    uid = world.member("member", {})["id"]

    def stored():
        return {g["store_id"]: (g["permission"], g["review"]) for g in world.overview()["grants"]
                if g["user_id"] == uid}
    assert stored() == {}
    # The strings keep their meaning: "write" has always included review.
    assert world.grant(uid, {"amber": "read", "team": "write"}).status_code == 200
    assert stored() == {"amber": ("read", False), "team": ("write", True)}
    for permission in ("read", "write"):
        for review in (False, True):
            r = world.grant(uid, {"amber": {"permission": permission, "review": review}, "team": "read"})
            assert r.status_code == 200, r.text
            assert stored() == {"amber": (permission, review), "team": ("read", False)}
    kept = stored()
    for bad in ("admin", "review", "", None, True, 1, ["read"], {}, {"permission": "read"}, {"review": True},
                {"permission": "write", "review": "yes"}, {"permission": "write", "review": 1},
                {"permission": "write", "review": None}, {"permission": "review", "review": True},
                {"permission": ["read"], "review": True}, {"permission": "read", "review": True, "admin": True}):
        r = world.grant(uid, {"team": "read", "amber": bad})
        assert r.status_code == 400 and r.json()["detail"], (bad, r.text)
    for body in ({"missing": "read"}, ["amber"], "write"):
        assert world.grant(uid, body).status_code == 400, body
    assert stored() == kept


def test_overview_me_and_stores_report_review(world):
    world.store("team")
    cobalt = world.member("cobalt", {"amber": REVIEW_ONLY, "team": WRITE_ONLY})
    plain, reviewing = world.issue(cobalt["id"]).json(), world.issue(cobalt["id"], review=True).json()
    overview = world.overview()
    assert overview["grants"] and all(type(g["review"]) is bool for g in overview["grants"])
    assert any(t["legacy"] for t in overview["tokens"]) and all(type(t["review"]) is bool for t in overview["tokens"])
    tokens = {t["id"]: t["review"] for t in overview["tokens"]}
    assert (tokens[plain["id"]], tokens[reviewing["id"]]) == (False, True)
    assert sum(tokens.values()) == 1
    assert {u["username"]: u["reviews"] for u in overview["users"]} == \
        {"owner": ["amber", "team"], "agent-user": ["amber"], "cobalt": ["amber"]}

    def stores(response):
        assert response.status_code == 200, response.text
        return sorted(response.json()["stores"], key=lambda s: s["id"])
    session = world.session(cobalt)
    mine = [{"id": "amber", "name": "amber", "permission": "read", "review": True, "review_policy": "open"},
            {"id": "team", "name": "team", "permission": "write", "review": False, "review_policy": "open"}]
    assert stores(session.get("/ui/me")) == mine and stores(session.get("/ui/stores")) == mine
    everything = [{"id": "amber", "name": "amber", "permission": "write", "review": True, "review_policy": "open"},
                  {"id": "team", "name": "team", "permission": "write", "review": True, "review_policy": "open"}]
    assert stores(world.admin.get("/ui/me")) == everything and stores(world.admin.get("/ui/stores")) == everything
    # A token reports its own reach: review only where it was issued with it.
    for issued, review in ((plain, False), (reviewing, True)):
        bearer = {"Authorization": "Bearer " + issued["token"]}
        only = [{"id": "amber", "name": "amber", "permission": "read", "review": review, "review_policy": "open"}]
        assert stores(world.api.get("/ui/me", headers=bearer)) == only
        assert stores(world.api.get("/ui/stores", headers=bearer)) == only
    assert stores(world.api.get("/ui/me", headers=LEGACY)) == \
        [{"id": "amber", "name": "amber", "permission": "write", "review": False, "review_policy": "open"}]
    # A disabled account reviews nothing.
    assert world.admin.patch(f"/admin/users/{cobalt['id']}", headers=world.csrf, json={"disabled": True}).status_code == 200
    assert next(u for u in world.overview()["users"] if u["username"] == "cobalt")["reviews"] == []


# --------------------------------------------------------------------------- every caller, every field


def test_who_reviews_under_each_setting_of_the_switch(world, monkeypatch):
    members = {name: world.member(name, {"amber": grant}) for name, grant in (
        ("reader", "read"), ("reviewer", REVIEW_ONLY), ("writer", WRITE_ONLY), ("both", "write"))}
    sessions = {name: world.session(user) for name, user in members.items()}
    tokens = {"both, write": world.token(members["both"]["id"], scope="write"),
              "both, read": world.token(members["both"]["id"]),
              "both, read, issued with review": world.token(members["both"]["id"], review=True),
              "writer, write": world.token(members["writer"]["id"], scope="write"),
              "reviewer, read, issued with review": world.token(members["reviewer"]["id"], review=True),
              "legacy": LEGACY}
    issued_with_review = {name for name in tokens if "issued with review" in name}
    write_tokens = {"both, write", "writer, write", "legacy"}       # only the switch lets these review
    for mode in ("off", "others", "all"):
        monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", mode)
        assert world.admin.get("/inbox").status_code == 200
        # The switch is about tokens: it gives no session a review its grant lacks.
        assert {name: session.get("/inbox").status_code for name, session in sessions.items()} == \
            {"reader": 403, "reviewer": 200, "writer": 403, "both": 200}, mode
        allowed = issued_with_review | (write_tokens if mode != "off" else set())
        assert {name: world.api.get("/inbox", headers=headers).status_code for name, headers in tokens.items()} == \
            {name: 200 if name in allowed else 403 for name in tokens}, mode
        assert world.api.get("/inbox").status_code == 401


def test_the_switch_lets_a_registry_token_review_only_when_it_may_save(store, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MEMD_CONTROL_DB", str(tmp_path / "control" / "control.db"))
    monkeypatch.setenv("MEMD_INBOX_TOKEN_REVIEW", "all")
    tokens = registry.Registry()
    tokens.initialize()

    def issue(*operations):
        made = tokens.issue(actor="operator", operation_id=str(uuid.uuid4()), label="ci", owner="team",
                            purpose="review", stores=["amber"], operations=list(operations))
        return {"Authorization": "Bearer " + made["secret"]}, made["token"]["id"]
    (saver, saver_id), (reader, reader_id) = issue("read", "save"), issue("read", "recall")
    with TestClient(server_mod.create_token_app()) as client:
        proposed = client.post("/propose", headers=saver, json={"body": "vmhost moved to the IoT VLAN."})
        assert proposed.status_code == 200, proposed.text
        cid = proposed.json()["id"]

        def page(headers):
            r = client.get("/entities/host/vmhost?fresh=true", headers=headers)
            assert r.status_code == 200, r.text
            return r.json()["inbox"]
        shown = page(saver)
        assert shown["can_review"] is True and [item["id"] for item in shown["items"]] == [cid]
        # A token the registry lets only read is no reviewer: it learns how many wait, not which.
        hidden = page(reader)
        assert (hidden["can_review"], hidden["pending"], hidden["items"]) == (False, 1, None)
        for method, path in _inbox_routes(cid):
            assert getattr(client, method)(path, headers=reader).status_code == 403, path
    made = _as_request(lambda: inbox.reviewer_for("amber"), label="ci", registry_token=saver_id)
    assert (made.is_token, made.can_write, made.store, made.credential) == (True, True, "amber", saver_id)
    with pytest.raises(PermissionError):
        _as_request(lambda: inbox.reviewer_for("amber"), label="ci", registry_token=reader_id)
    assert inbox.get(store, "amber", cid)["status"] == "pending"


def test_a_reviewer_without_write_changes_and_removes_nothing_but_tags_and_importance(world):
    session = world.session(world.member("cobalt", {"amber": REVIEW_ONLY}))
    proposed = {"title": "Garage door", "slug": "garage-door", "host": "vmhost",
                "description": "Where the opener sits.", "source": "runbook", "observed_at": "2026-11-01",
                "verified_at": "2026-11-02", "pinned": True, "volatility": "state",
                "verify": ["tcp: vmhost:8077"], "tags": ["network"], "importance": 4}
    body = "The garage door opener is on the IoT VLAN."
    cid = world.propose(body, **proposed)
    changed = {"title": "Garage door opener", "body": "The reviewer's own words.", "slug": "side-door",
               "host": "otherhost", "description": "Rewritten.", "source": "the reviewer",
               "observed_at": "2026-01-01", "verified_at": "2026-01-02", "pinned": False,
               "volatility": "durable", "verify": [], "supersedes": "repo-hosting-policy",
               "expected_revision": "0" * 40}
    assert set(changed) == set(inbox.EDITABLE) - inbox.METADATA_EDITS      # every field a reviewer can name
    # null removes a proposed field, which rewrites the note as surely as a new value does.
    removed = {key: None for key in proposed if key not in inbox.METADATA_EDITS}
    before = head(world.cfg.clone)
    for edits in [{key: value} for key, value in changed.items()] + [{key: None} for key in ("body", *removed)] + \
            [removed, {"tags": ["door"], "importance": 1, "host": None}]:
        r = session.post(f"/inbox/{cid}/approve", json={"edits": edits})
        assert _refused(r, "importance and tags"), (edits, r.text)
    item = session.get(f"/inbox/{cid}").json()
    assert (item["status"], item["last_error"], item["title"], item["body"]) == ("pending", None, "Garage door", body)
    assert head(world.cfg.clone) == before
    # Nothing a caller sends names the store an approval may write.
    named = {"approved_write": "amber", "memd_approved_write": "amber"}
    assert _refused(session.post("/save", headers={"X-Approved-Write": "amber"},
                                 json={"body": "Saved by the reviewer.", **named}), "required store access")
    # Tags and importance are the reviewer's to change, and to remove.
    note = world.note(session.post(f"/inbox/{cid}/approve", json={"edits": {"tags": None, "importance": None}}))
    assert (note.tags, note.importance) == ([], 3)
    assert (note.title, note.body, note.slug, note.host, note.description, note.source, note.observed_at,
            note.verified_at, note.pinned, note.volatility, note.saved_by) == \
        ("Garage door", body, "garage-door", "vmhost", "Where the opener sits.", "runbook", "2026-11-01",
         "2026-11-02", True, "state", "laptop-agent")
    assert note.metadata["verify"] and session.get(f"/inbox/{cid}").json()["original"]["meta"]["tags"] == ["network"]


def test_an_approval_writes_its_own_store_whatever_the_candidate_names(world):
    world.store("team")
    session = world.session(world.member("cobalt", {"amber": REVIEW_ONLY, "team": REVIEW_ONLY}))
    cid = world.propose("The garage door opener is on the IoT VLAN.", title="Garage door")
    team = Path(control.store("team")["config"]["clone_path"])
    assert team.is_dir() and not list(team.rglob("*.md"))
    # A row no proposal can file: its stored fields name another store the reviewer reviews.
    connection = sqlite3.connect(inbox.inbox_path(world.cfg, "amber"))
    try:
        with connection:
            connection.execute("UPDATE candidates SET meta=? WHERE id=?",
                               (json.dumps({"profile": "team", "store": "team", "target_store": "team"}), cid))
    finally:
        connection.close()
    for edits in ({"profile": "team"}, {"store": "team"}):
        r = session.post(f"/inbox/{cid}/approve", json={"edits": edits})
        assert r.status_code == 400 and "Editable fields" in r.json()["detail"], r.text
    note = world.note(session.post(f"/inbox/{cid}/approve", json={"profile": "amber"}))
    assert note.title == "Garage door" and note.profile == "amber" and not list(team.rglob("*.md"))
    assert session.get("/inbox?profile=team").json()["counts"] == {"pending": 0, "approved": 0, "rejected": 0}
    # The same id decides nothing in the other store's inbox.
    other = world.propose("The office printer is on the guest VLAN.")
    for decision in ("approve", "reject"):
        assert session.post(f"/inbox/{other}/{decision}", json={"profile": "team"}).status_code == 404
    assert inbox.get(world.cfg, "amber", other)["status"] == "pending" and not list(team.rglob("*.md"))


def test_a_tokens_review_follows_its_owner_on_each_store_and_ends_with_the_account(world):
    world.store("team")
    cobalt = world.member("cobalt", {"amber": REVIEW_ONLY, "team": REVIEW_ONLY})
    issued = world.issue(cobalt["id"], review=True, extra_stores={"team": "read"}).json()
    token = {"Authorization": "Bearer " + issued["token"]}

    def reviews(headers=token):
        return {name: world.api.get(f"/inbox?profile={name}", headers=headers).status_code
                for name in ("amber", "team")}

    def disable(uid, disabled):
        r = world.admin.patch(f"/admin/users/{uid}", headers=world.csrf, json={"disabled": disabled})
        assert r.status_code == 200, r.text
    assert reviews() == {"amber": 200, "team": 200}
    # Each store follows its owner's grant there: the additional store alone, then the token's own alone.
    assert world.grant(cobalt["id"], {"amber": REVIEW_ONLY, "team": "read"}).status_code == 200
    assert reviews() == {"amber": 200, "team": 403}
    assert world.grant(cobalt["id"], {"amber": "read", "team": REVIEW_ONLY}).status_code == 200
    assert reviews() == {"amber": 403, "team": 200}
    assert world.grant(cobalt["id"], {"amber": REVIEW_ONLY}).status_code == 200      # the team grant is gone
    assert reviews() == {"amber": 200, "team": 403}
    # A disabled owner's token is nobody, and so is a revoked token.
    disable(cobalt["id"], True)
    assert reviews() == {"amber": 401, "team": 401}
    disable(cobalt["id"], False)
    assert reviews() == {"amber": 200, "team": 403}
    assert world.admin.delete(f"/admin/tokens/{issued['id']}", headers=world.csrf).status_code == 200
    assert reviews() == {"amber": 401, "team": 401}
    # An administrator's token reviews every store it reaches only while its owner is one.
    deputy = world.admin.post("/admin/users", headers=world.csrf, json={"username": "deputy", "role": "admin"}).json()
    admins = world.token(deputy["id"], review=True, extra_stores={"team": "read"})
    assert reviews(admins) == {"amber": 200, "team": 200}
    r = world.admin.patch(f"/admin/users/{deputy['id']}", headers=world.csrf, json={"role": "member"})
    assert r.status_code == 200, r.text
    assert reviews(admins) == {"amber": 401, "team": 401}          # a member with no grant reaches nothing
    assert world.grant(deputy["id"], {"amber": "write", "team": WRITE_ONLY}).status_code == 200
    assert reviews(admins) == {"amber": 200, "team": 403}


# --------------------------------------------------------------------------- the shared connection


def test_waiting_for_the_journal_mode_is_bounded_and_hides_no_other_error(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(control, "time", types.SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))

    class Connection:
        def __init__(self, *errors, then=None):
            self.errors, self.then, self.calls = list(errors), then, 0

        def execute(self, sql):
            assert sql == "PRAGMA journal_mode=WAL"
            self.calls += 1
            error = self.errors.pop(0) if self.errors else self.then
            if error:
                raise sqlite3.OperationalError(error)

    def run(connection):
        started = clock[0]
        try:
            control._wal(connection)
            return None, connection.calls, clock[0] - started
        except sqlite3.OperationalError as exc:
            return str(exc), connection.calls, clock[0] - started
    # A database nobody is switching: one statement and no wait, as before the retry existed.
    assert run(Connection()) == (None, 1, 0)
    # Any other error is raised at once, also after a lock gave way to it.
    assert run(Connection(then="disk I/O error")) == ("disk I/O error", 1, 0)
    error, calls, waited = run(Connection("database is locked", then="unable to open database file"))
    assert (error, calls) == ("unable to open database file", 2) and waited < 1
    # Another connection making the switch: wait for it.
    error, calls, waited = run(Connection("database is locked", "database is locked"))
    assert (error, calls) == (None, 3) and waited < 1
    # A lock that never clears is reported as itself, after a bounded wait.
    error, calls, waited = run(Connection(then="database is locked"))
    assert error == "database is locked" and 15 <= waited <= 16 and calls <= 800
