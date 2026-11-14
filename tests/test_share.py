"""Team sharing with provenance (memd.share): publish, federated recall, upstream drift.

Security first. Hermetic: temp Git/SQLite stores behind the administered access
layer (accounts, grants, scoped tokens), model backends replaced by stubs, the
socket guard blocks everything else. Every refusal also asserts that nothing
was written and nothing from the refused store came back.
"""
import asyncio
import contextvars
import json
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

import memd.mcp as mcp_mod
import memd.recall as recall_mod
import memd.save as save_mod
from memd import access, actor, control, facts, server, share, sources
from memd.config import Config
from memd.refresh import ensure_lexical
from memd.store import list_notes, read_note

PASSWORD = "test-password-long-enough"
TEAM_SECRET = "TEAMONLY-QUARTZ"
OTHER_SECRET = "OTHERSTORE-ZEPHYR"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit_note(clone, name, text):
    (clone / name).write_text(text)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", f"edit {name}")


RICH_NOTE = """\
---
title: Archive backup window
slug: archive-backup-window
profile: amber
host: lapbox
importance: 4
tags: [backup, archive]
grounding: ok
description: When the archive backs up
source: operator
observed_at: '2026-08-01'
volatility: state
pinned: true
saved_by: personal-laptop
last_used: '2026-09-01T10:00:00Z'
cluster: personal-backups
verify:
- tcp: lapbox:22
verification:
  status: failed
  checked_at: '2026-09-01'
---
Nightly archive backups start at 02:30 on lapbox and finish by 04:00.
"""


def _store_cfg(profile):
    return share._cfg(profile)


def _clone(profile):
    return share._paths(profile)[1]


@pytest.fixture
def team(config, tmp_path, monkeypatch):
    """Admin control with the serving store `amber`, a `team` and an `other` store.

    `member` may read+write amber and write team; `other` is granted to nobody
    but the administrator and holds a secret no member may ever see.
    """
    monkeypatch.setenv("MEMD_ADMIN_DB", str(tmp_path / "control" / "admin.db"))
    monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "1")
    monkeypatch.setenv("MEMD_LOCAL_HOST", "any")
    monkeypatch.setattr(sources._jobs, "submit", lambda fn, *args: fn(*args))
    monkeypatch.setattr(recall_mod, "embed_with_deadline", lambda *a, **k: None)
    monkeypatch.setattr(recall_mod, "rerank", lambda *a, **k: None)
    monkeypatch.setattr(save_mod, "_vector_near_matches", lambda cfg, body: [])
    monkeypatch.setattr(save_mod, "_pull_rebase_push", lambda clone: None)
    _commit_note(config.clone, "archive-backup-window.md", RICH_NOTE)
    control.initialize()
    control.register_existing()
    control.create_user("owner", PASSWORD, "admin")
    ensure_lexical(config)
    app = server.create_token_app()
    with TestClient(app) as admin:
        login = admin.post("/ui/login", json={"username": "owner", "password": PASSWORD})
        assert login.status_code == 200
        admin.headers["X-CSRF-Token"] = login.json()["csrf"]
        for store in ("team", "other"):
            r = admin.post("/admin/stores", json={"id": store, "name": store, "kind": "local"})
            assert r.status_code == 200, r.text
        # Direct saves by default in these tests; the inbox route has its own test.
        assert admin.put("/admin/stores/team", json={"config": {"publish_review": False}}).status_code == 200
        user = admin.post("/admin/users", json={"username": "member"}).json()
        r = admin.put(f"/admin/users/{user['id']}/grants", json={"grants": {"amber": "write", "team": "write"}})
        assert r.status_code == 200
        owner = TestClient(app)
        owner.headers["Authorization"] = "Bearer " + _issue(admin, user["id"], "amber", "write")
        other_token = _issue(admin, _admin_id(), "other", "write")
        seeded = TestClient(app).post("/save", headers={"Authorization": "Bearer " + other_token},
                                      json={"title": "Other secret", "body": f"{OTHER_SECRET} backup codes"})
        assert seeded.status_code == 200, seeded.text
        yield {"admin": admin, "app": app, "user": user, "config": config}


def _admin_id():
    with control.db() as c:
        return c.execute("SELECT id FROM users WHERE username='owner'").fetchone()[0]


def _issue(admin, uid, store, scope, extra=None):
    body = {"label": "agent", "user_id": uid, "store_id": store, "scope": scope}
    if extra is not None:
        body["extra_stores"] = extra
    r = admin.post("/admin/tokens", json=body)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _client(team, token):
    client = TestClient(team["app"])
    client.headers["Authorization"] = "Bearer " + token
    return client


def _agent(team, extra=None, store="amber", scope="write"):
    return _client(team, _issue(team["admin"], team["user"]["id"], store, scope, extra))


def _mcp(client, name, args):
    r = client.post("/mcp/", headers={"Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _published(profile):
    return [n for n in list_notes(_clone(profile)) if share.provenance_of(n.metadata)]


# --------------------------------------------------------------------------- publish: permissions


def test_publish_permission_matrix(team):
    admin, uid = team["admin"], team["user"]["id"]
    team_head = _git(_clone("team"), "rev-parse", "HEAD")
    body = {"slug": "archive-backup-window", "target_store": "team"}

    # No write on the target: the token reaches team read-only.
    r = _agent(team, {"team": "read"}).post("/publish", json=body)
    assert r.status_code == 403, r.text
    # The token has no scope on the target at all.
    assert _agent(team).post("/publish", json=body).status_code == 403
    # No read on the source: a token scoped to team only cannot reach amber.
    r = _agent(team, store="team").post("/publish", json={**body, "store": "amber"})
    assert r.status_code == 403
    # "store": null must not hide source_store and fall back to the caller's own store.
    r = _agent(team, store="team").post("/publish", json={**body, "store": None, "source_store": "amber"})
    assert r.status_code == 403, r.text
    # A store granted to nobody here, as source or target.
    both = _agent(team, {"team": "write"})
    assert both.post("/publish", json={**body, "target_store": "other"}).status_code == 403
    assert both.post("/publish", json={**body, "store": "other"}).status_code == 403
    # The legacy serving token carries the serving store only.
    legacy = _client(team, "test-token")
    assert legacy.post("/publish", json=body).status_code == 403
    assert _mcp(both, "publish", {**body, "target_store": "other"})["isError"] is True
    assert _git(_clone("team"), "rev-parse", "HEAD") == team_head
    assert _published("team") == [] and _published("other") == []

    # A token may not be issued beyond its owner's grants.
    r = admin.post("/admin/tokens", json={"label": "x", "user_id": uid, "store_id": "amber", "scope": "read",
                                          "extra_stores": {"other": "read"}})
    assert r.status_code == 400
    assert admin.post("/admin/tokens", json={"label": "x", "user_id": uid, "store_id": "amber", "scope": "read",
                                             "extra_stores": {"../team": "read"}}).status_code == 400

    # Read on the source + write on the target: allowed.
    r = both.post("/publish", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "published"

    # Revoking the owner's team grant revokes the token's reach there at once.
    admin.put(f"/admin/users/{uid}/grants", json={"grants": {"amber": "write", "team": "read"}})
    assert both.post("/publish", json={**body, "slug": "repo-hosting-policy"}).status_code == 403
    admin.put(f"/admin/users/{uid}/grants", json={"grants": {"amber": "write"}})
    assert both.post("/recall", json={"query": "archive", "stores": ["amber", "team"]}).status_code == 403


def test_published_copy_carries_provenance_and_only_travelling_fields(team):
    agent = _agent(team, {"team": "write"})
    r = agent.post("/publish", json={"slug": "archive-backup-window", "target_store": "team"})
    assert r.status_code == 200, r.text
    out = r.json()
    source = read_note(team["config"].clone, "archive-backup-window")
    [copy] = _published("team")
    prov = copy.metadata["published_from"]
    assert {k: prov[k] for k in ("store", "slug", "revision")} == \
        {"store": "amber", "slug": "archive-backup-window", "revision": source.git_blob}
    assert prov["by"] == "agent" and prov["at"].endswith("Z")
    assert out["published_from"] == prov and out["target"]["slug"] == copy.slug == "archive-backup-window"
    # Travels.
    assert copy.body == source.body and copy.title == source.title and copy.host == "lapbox"
    assert copy.tags == ["backup", "archive"] and copy.importance == 4
    assert copy.description == source.description and copy.source == "operator"
    assert copy.observed_at == "2026-08-01" and copy.volatility == "state"
    assert copy.metadata["verify"] == [{"tcp": "lapbox:22"}]
    # Never travels.
    assert copy.pinned is False and copy.last_used is None and copy.saved_by == "agent"
    assert "cluster" not in copy.metadata and "verification" not in copy.metadata
    assert copy.profile == "team"


def test_save_and_propose_cannot_forge_provenance(team):
    agent = _agent(team, {"team": "write"})
    forged = {"store": "other", "slug": "other-secret", "revision": "0" * 40}
    r = agent.post("/save", json={"title": "Forged", "body": "hello", "published_from": forged, "profile": "team"})
    assert r.status_code == 200, r.text
    assert "published_from" not in read_note(_clone("team"), "forged").metadata
    r = agent.post("/propose", json={"title": "Forged two", "body": "hi", "published_from": forged,
                                     "profile": "team"})
    assert r.status_code == 200
    item = team["admin"].get(f"/inbox/{r.json()['id']}?profile=team").json()
    assert item["published_from"] is None


def test_publish_review_routes_through_the_target_inbox(team):
    admin = team["admin"]
    assert admin.put("/admin/stores/team", json={"config": {"publish_review": True}}).status_code == 200
    assert control.public_store(control.store("team"))["publish_review"] is True
    head = _git(_clone("team"), "rev-parse", "HEAD")
    agent = _agent(team, {"team": "write"})
    r = agent.post("/publish", json={"slug": "archive-backup-window", "target_store": "team"})
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["queued"] and out["action"] == "queued" and out["inbox_id"]
    assert _git(_clone("team"), "rev-parse", "HEAD") == head and _published("team") == []
    item = admin.get(f"/inbox/{out['inbox_id']}?profile=team").json()
    assert item["source"] == "publish" and item["published_from"]["slug"] == "archive-backup-window"
    # An agent token cannot approve (MEMD_INBOX_TOKEN_REVIEW defaults to off).
    assert agent.post(f"/inbox/{out['inbox_id']}/approve", json={"profile": "team"}).status_code == 403
    r = admin.post(f"/inbox/{out['inbox_id']}/approve", json={"profile": "team"})
    assert r.status_code == 200, r.text
    [copy] = _published("team")
    assert copy.metadata["published_from"]["revision"] == out["published_from"]["revision"]
    # MCP reports the same queue.
    result = _mcp(agent, "publish", {"slug": "repo-hosting-policy", "target_store": "team"})
    assert result["isError"] is False and result["structuredContent"]["action"] == "queued"


def test_republish_is_idempotent_updates_in_place_and_never_clobbers_other_notes(team):
    agent = _agent(team, {"team": "write"})
    # An unrelated team note already owns the source's slug.
    r = agent.post("/save", json={"profile": "team", "slug": "repo-hosting-policy", "title": "Team repo rule",
                                  "body": f"{TEAM_SECRET} team repos live on the team forge."})
    assert r.status_code == 200, r.text
    first = agent.post("/publish", json={"slug": "repo-hosting-policy", "target_store": "team"}).json()
    assert first["action"] == "published" and first["target"]["slug"] == "repo-hosting-policy-2"
    assert TEAM_SECRET in read_note(_clone("team"), "repo-hosting-policy").body
    head = _git(_clone("team"), "rev-parse", "HEAD")
    again = agent.post("/publish", json={"slug": "repo-hosting-policy", "target_store": "team"}).json()
    assert again["action"] == "unchanged" and again["target"]["slug"] == "repo-hosting-policy-2"
    assert _git(_clone("team"), "rev-parse", "HEAD") == head

    # The source changes: the copy is updated in place, never duplicated.
    agent.post("/save", json={"slug": "repo-hosting-policy", "body": "New repos go to the self-hosted forge."})
    updated = agent.post("/publish", json={"slug": "repo-hosting-policy", "target_store": "team"}).json()
    assert updated["action"] == "updated" and updated["target"]["slug"] == "repo-hosting-policy-2"
    copies = [n for n in _published("team") if n.metadata["published_from"]["slug"] == "repo-hosting-policy"]
    assert len(copies) == 1 and copies[0].body == "New repos go to the self-hosted forge."
    assert TEAM_SECRET in read_note(_clone("team"), "repo-hosting-policy").body
    assert agent.post("/publish", json={"slug": "no-such-note", "target_store": "team"}).status_code == 404
    assert agent.post("/publish", json={"slug": "repo-hosting-policy", "target_store": "amber"}).status_code == 400


def test_encrypted_source_needs_the_explicit_flag_for_a_plaintext_target(team, tmp_path, monkeypatch):
    from memd.crypt import generate_key_file
    from memd.cli_crypt import encrypt_store
    key = tmp_path / "keys" / "amber.key"
    generate_key_file(key)
    monkeypatch.setenv("MEMD_AMBER_KEY_FILE", str(key))
    encrypt_store(team["config"].clone)
    agent = _agent(team, {"team": "write"})
    head = _git(_clone("team"), "rev-parse", "HEAD")
    body = {"slug": "archive-backup-window", "target_store": "team"}
    r = agent.post("/publish", json=body)
    assert r.status_code == 400 and "allow_decrypted_publish" in r.json()["detail"]
    assert _mcp(agent, "publish", body)["isError"] is True
    assert agent.post("/publish", json={**body, "allow_decrypted_publish": "yes"}).status_code == 400
    assert _git(_clone("team"), "rev-parse", "HEAD") == head and _published("team") == []
    r = agent.post("/publish", json={**body, "allow_decrypted_publish": True})
    assert r.status_code == 200, r.text
    assert any("encrypted" in w for w in r.json()["warnings"])
    assert _published("team")[0].body.startswith("Nightly archive backups")


# --------------------------------------------------------------------------- federated recall


def _seed_team(agent):
    r = agent.post("/save", json={"profile": "team", "title": "Team backup rota",
                                  "body": f"{TEAM_SECRET} the archive backup rota rotates weekly."})
    assert r.status_code == 200, r.text


def test_federated_recall_labels_fuses_and_keeps_default_unchanged(team):
    agent = _agent(team, {"team": "write"})
    _seed_team(agent)
    ensure_lexical(_store_cfg("team"))

    default = agent.post("/recall", json={"query": "archive backup"}).json()
    assert "stores" not in default and default["profile"] == "amber"
    assert all("store" not in n for n in default["notes"])
    assert TEAM_SECRET not in json.dumps(default) and "[amber]" not in default["context"]

    r = agent.post("/recall", json={"query": "archive backup", "stores": ["amber", "team"], "include_core": False})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["stores"] == ["amber", "team"] and out["stores_skipped"] == [] and out["recall_id"] is None
    labelled = [(n["store"], n["slug"]) for n in out["notes"]]
    # Reciprocal-rank fusion: each store's best match before either store's second.
    assert labelled[:2] == [("amber", "archive-backup-window"), ("team", "team-backup-rota")]
    assert "### [amber] archive-backup-window" in out["context"]
    assert "### [team] team-backup-rota" in out["context"]
    assert {e["store"] for e in out["rendering"]["excerpts"]} == {"amber", "team"}
    assert OTHER_SECRET not in json.dumps(out)

    # scope=all expands to the granted stores only; `other` is never searched.
    everything = agent.post("/recall", json={"query": "backup codes archive", "scope": "all"}).json()
    assert everything["stores"] == ["amber", "team"]
    assert OTHER_SECRET not in json.dumps(everything)
    # The administrator is granted every store.
    admin_all = team["admin"].post("/recall", json={"query": "backup codes", "scope": "all"}).json()
    assert "other" in admin_all["stores"] and OTHER_SECRET in json.dumps(admin_all)

    # MCP: same labels.
    result = _mcp(agent, "recall", {"query": "archive backup", "stores": "amber,team"})
    assert result["isError"] is False
    assert result["structuredContent"]["stores"] == ["amber", "team"]
    assert "[team] team-backup-rota" in result["structuredContent"]["text"]
    single = _mcp(agent, "recall", {"query": "archive backup"})["structuredContent"]
    assert "stores" not in single and TEAM_SECRET not in single["text"]


@pytest.mark.parametrize("stores", [
    ["amber", "other"], ["other"], ["../other"], ["other/../amber"], ["team/.."], [".git"], ["OTHER"],
    ["amber\nother"], ["amber", ""], ["amber", 7], [None], ["x" * 300], ["%2e%2e"], ["/etc"],
])
def test_federated_recall_refuses_ungranted_and_crafted_store_names(team, stores):
    agent = _agent(team, {"team": "write"})
    r = agent.post("/recall", json={"query": "backup codes", "stores": stores})
    assert r.status_code == 403, r.text
    assert OTHER_SECRET not in r.text and "etc" not in r.json()["detail"]
    result = _mcp(agent, "recall", {"query": "backup codes", "stores": stores}) \
        if all(isinstance(s, str) for s in stores) else {"isError": True}
    assert result["isError"] is True and OTHER_SECRET not in json.dumps(result)


def test_federated_recall_argument_errors(team):
    agent = _agent(team, {"team": "write"})
    assert agent.post("/recall", json={"query": "q", "stores": []}).status_code == 400
    assert agent.post("/recall", json={"query": "q", "stores": ["amber"] * 17}).status_code == 400
    assert agent.post("/recall", json={"query": "q", "scope": "everything"}).status_code == 400
    assert agent.post("/recall", json={"query": "q", "scope": "all", "stores": ["amber"]}).status_code == 400
    assert agent.post("/recall", json={"query": "q", "stores": ["amber"], "profile": "amber"}).status_code == 400
    # A token without extra stores: scope=all is just its own store.
    assert _agent(team).post("/recall", json={"query": "q", "scope": "all"}).json()["stores"] == ["amber"]


def test_read_with_store_argument_requires_the_matching_grant(team):
    agent = _agent(team, {"team": "write"})
    _seed_team(agent)
    r = agent.post("/read", json={"slug": "team-backup-rota", "store": "team"})
    assert r.status_code == 200 and r.json()["store"] == "team" and TEAM_SECRET in r.json()["body"]
    assert agent.post("/read", json={"slug": "team-backup-rota"}).status_code == 404   # own store: amber
    assert agent.post("/read", json={"slug": "other-secret", "store": "other"}).status_code == 403
    assert agent.post("/read", json={"slug": "x", "store": "team", "profile": "amber"}).status_code == 400
    assert agent.post("/read", json={"slug": "x", "store": 5}).status_code == 400
    assert agent.post("/read", json={"slug": "x", "store": "../other"}).status_code in (400, 403)
    readonly = _agent(team, store="amber", scope="read")
    assert readonly.post("/read", json={"slug": "team-backup-rota", "store": "team"}).status_code == 403
    result = _mcp(agent, "read", {"slug": "team-backup-rota", "store": "team"})
    assert result["structuredContent"]["store"] == "team"
    denied = _mcp(agent, "read", {"slug": "other-secret", "store": "other"})
    assert denied["isError"] is True and OTHER_SECRET not in json.dumps(denied)


# --------------------------------------------------------------------------- drift


def test_upstream_drift_in_read_recall_and_status(team, capsys, monkeypatch):
    agent = _agent(team, {"team": "write"})
    assert agent.post("/publish", json={"slug": "archive-backup-window", "target_store": "team"}).status_code == 200
    ensure_lexical(_store_cfg("team"))
    read = agent.post("/read", json={"slug": "archive-backup-window", "store": "team"}).json()
    assert read["published_from"]["store"] == "amber" and read["upstream"]["status"] == "current"

    agent.post("/save", json={"slug": "archive-backup-window",
                              "body": "Archive backups moved to 03:00 on lapbox."})
    read = agent.post("/read", json={"slug": "archive-backup-window", "store": "team"}).json()
    assert read["upstream"]["status"] == "behind" and "publish(" in read["upstream"]["hint"]
    ensure_lexical(_store_cfg("amber"))
    out = agent.post("/recall", json={"query": "archive backups", "stores": ["team"]}).json()
    [copy] = [n for n in out["notes"] if n["slug"] == "archive-backup-window"]
    assert copy["upstream"]["status"] == "behind"

    # A caller who can read the team store but not the source learns nothing about it.
    uid = team["admin"].post("/admin/users", json={"username": "teammate"}).json()["id"]
    team["admin"].put(f"/admin/users/{uid}/grants", json={"grants": {"team": "read"}})
    mate = _client(team, _issue(team["admin"], uid, "team", "read"))
    blind = mate.post("/read", json={"slug": "archive-backup-window"}).json()
    assert blind["published_from"]["store"] == "amber" and "upstream" not in blind
    recalled = mate.post("/recall", json={"query": "archive backups"}).json()
    assert all("upstream" not in n for n in recalled["notes"])

    # mem-share status, as the local operator.
    monkeypatch.setenv("MEMD_PROFILE", "amber")
    assert share.main(["status", "--store", "team", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["counts"] == {"behind": 1}
    assert status["published"][0]["published_from"]["slug"] == "archive-backup-window"
    assert agent.post("/publish", json={"slug": "archive-backup-window", "target_store": "team"}).json()["action"] == "updated"
    assert share.main(["status", "--store", "team"]) == 0
    assert "1 current" in capsys.readouterr().out


# --------------------------------------------------------------------------- units


def test_federate_skips_a_store_that_misses_the_deadline_or_fails():
    def one(store):
        if store == "slow":
            time.sleep(1.0)
        if store == "broken":
            raise RuntimeError("boom")
        return [{"slug": f"{store}-{i}", "matched": True, "body": "b"} for i in range(3)] + \
               [{"slug": f"{store}-core", "matched": False, "body": "c", "importance": 5}]
    start = time.monotonic()
    notes, skipped = share.federate(["a", "slow", "b", "broken"], one, k=4, budget_ms=300)
    assert time.monotonic() - start < 0.9
    assert {s["store"]: s["reason"] for s in skipped} == {"slow": "deadline", "broken": "RuntimeError"}
    assert [(n["store"], n["slug"]) for n in notes] == [
        ("a", "a-core"), ("b", "b-core"), ("a", "a-0"), ("b", "b-0"), ("a", "a-1"), ("b", "b-1")]


def test_bound_identity_cannot_federate_or_publish_beyond_its_store(monkeypatch):
    from memd.identity import bind_identity, clear_identity
    bind_identity("amber")
    try:
        assert share.readable_stores() == ["amber"]
        assert share.resolve_stores(["amber"]) == ["amber"]
        with pytest.raises(share.StoreRefused):
            share.resolve_stores(["amber", "cobalt"])
        with pytest.raises(share.StoreRefused):
            share.publish("anything", "cobalt")
        with pytest.raises(share.StoreRefused):
            share.publish("anything", "amber", source_store="cobalt")
    finally:
        clear_identity()


def test_publish_review_setting_defaults_on(monkeypatch):
    assert share.publish_review("amber") is True
    monkeypatch.setenv("MEMD_AMBER_PUBLISH_REVIEW", "false")
    assert share.publish_review("amber") is False
    with pytest.raises(ValueError):
        sources.validate_config("local", {"publish_review": "no"})
    assert sources.validate_config("local", {"publish_review": False})["publish_review"] is False


def test_render_marks_store_without_changing_single_store_output():
    from memd.render import render_result
    plain = [{"slug": "a", "matched": True, "body": "alpha"}]
    assert "[" not in render_result(plain)["text"]
    both = [{"slug": "a", "matched": True, "body": "alpha", "store": "x"},
            {"slug": "a", "matched": True, "body": "alpha two", "store": "y"}]
    shaped = render_result(both)
    assert "### [x] a" in shaped["text"] and "### [y] a" in shaped["text"]
    assert [e["store"] for e in shaped["excerpts"]] == ["x", "y"]


def test_mem_share_cli_publishes_as_the_local_operator(team, capsys):
    assert share.main(["publish", "repo-hosting-policy", "team", "--from", "amber", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "published" and out["published_from"]["by"] == "local"
    assert share.main(["publish", "repo-hosting-policy", "team", "--from", "../other"]) == 1
    assert "not a store" in capsys.readouterr().err
    # The CLI marker never outlives the call.
    assert share._operator.get() is False


def test_federated_recall_is_usage_logged_per_store_under_one_id(team, monkeypatch):
    import sqlite3
    from memd import usage
    monkeypatch.setenv("MEMD_USAGE_LOG", "on")
    agent = _agent(team, {"team": "write"})
    _seed_team(agent)
    ensure_lexical(_store_cfg("team"))
    out = agent.post("/recall", json={"query": "archive backup", "stores": ["amber", "team"],
                                      "include_core": False}).json()
    rid = out["recall_id"]
    assert isinstance(rid, str) and len(rid) == 16

    def logged(store):
        path = usage.usage_path(usage.index_path(_store_cfg(store), store))
        with sqlite3.connect(path) as db:
            return {r[0]: json.loads(r[1]) for r in db.execute("SELECT id, slugs FROM recalls")}
    shown = {e["store"]: [] for e in out["rendering"]["excerpts"]}
    for e in out["rendering"]["excerpts"]:
        shown[e["store"]].append(e["slug"])
    assert logged("amber")[rid] == shown["amber"] and logged("team")[rid] == shown["team"]
    assert "team-backup-rota" not in logged("amber")[rid]      # each log holds only its own store

    r = agent.post("/read", json={"slug": "team-backup-rota", "store": "team", "recall_id": rid})
    assert r.status_code == 200, r.text
    path = usage.usage_path(usage.index_path(_store_cfg("team"), "team"))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT recall_id FROM reads WHERE slug='team-backup-rota'").fetchone()[0] == rid


# --------------------------------------------------------------------------- federated recall: deadline, facts, skipped stores

SURFACES = ("rest", "mcp")
PROXY_NOW = "where does the nginx proxy run now?"


def _federated(agent, surface, body):
    """One federated recall over REST or MCP: (text, stores, stores_skipped, rendering)."""
    if surface == "rest":
        r = agent.post("/recall", json=body)
        assert r.status_code == 200, r.text
        out = r.json()
        return out["context"], out["stores"], out["stores_skipped"], out["rendering"]
    result = _mcp(agent, "recall", body)
    assert result["isError"] is False, result
    out = result["structuredContent"]
    return out["text"], out["stores"], out["stores_skipped"], out


def _patch_recall(monkeypatch, behave):
    """Call `behave(store)` before each store's recall, REST and MCP; a list it returns is the result."""
    def wrap(real):
        def recall(query, profile="amber", k=8, **kw):
            early = behave(profile)
            return early if early is not None else real(query, profile=profile, k=k, **kw)
        return recall
    monkeypatch.setattr(server, "_core_recall", wrap(server._core_recall))
    monkeypatch.setattr(mcp_mod, "_core_recall", wrap(mcp_mod._core_recall))


def _hold(monkeypatch, store="team"):
    """Recall of `store` hangs until the returned event is set, then finds nothing."""
    release = threading.Event()

    def behave(profile):
        if profile == store:
            release.wait(10)
            return []
    _patch_recall(monkeypatch, behave)
    return release


def test_deadline_ms_argument_and_budget(monkeypatch):
    from memd.config import DEFAULT_EMBED_DEADLINE_MS, DEFAULT_RERANK_DEADLINE_MS
    from memd.normalize import normalize_recall_args
    for junk in ("soon", "", None, 0, -1, "0", "-5", 0.0, float("nan"), float("inf"), "nan", "inf", "-inf",
                 "1e999", True, False, [], [250], {}):
        assert "deadline_ms" not in normalize_recall_args({"query": "q", "deadline_ms": junk}), repr(junk)
    for value, want in ((250, 250), ("250", 250), (" 250 ", 250), (250.9, 250), ("1e3", 1000), (0.5, 100),
                        (7, 100), (10 ** 12, 10 ** 12)):
        assert normalize_recall_args({"query": "q", "deadline_ms": value})["deadline_ms"] == want
    assert set(normalize_recall_args({"query": "q"})) == {"query", "k"}

    monkeypatch.setenv("MEMD_FEDERATED_DEADLINE_MS", "2000")
    assert share.deadline_ms() == share.deadline_ms(None) == 2000
    assert [share.deadline_ms(v) for v in (1, 100, 750, 2000, 2001, 10 ** 12)] == \
        [100, 100, 750, 2000, 2000, 2000]
    monkeypatch.delenv("MEMD_FEDERATED_DEADLINE_MS")
    default = DEFAULT_EMBED_DEADLINE_MS + DEFAULT_RERANK_DEADLINE_MS + share.FEDERATION_SLACK_MS
    assert share.deadline_ms(10 ** 9) == share.deadline_ms() == default

    props = {t.name: t.input_schema["properties"] for t in asyncio.run(mcp_mod.list_tools())}
    assert set(props["recall"]["deadline_ms"]["type"]) == {"number", "string"}
    assert props["recall"]["deadline_ms"]["description"]
    assert "deadline_ms" not in props["ask"]


def test_federated_deadline_ms_can_only_shorten_the_server_budget(team, monkeypatch):
    agent = _agent(team, {"team": "write"})
    _seed_team(agent)
    ensure_lexical(_store_cfg("team"))
    body = {"query": "archive backup", "stores": ["amber", "team"]}
    for surface in SURFACES:
        plain = _federated(agent, surface, body)
        assert plain[1] == ["amber", "team"] and plain[2] == []
        assert _federated(agent, surface, {**body, "deadline_ms": 600000}) == plain
    # A store that hangs is still cut off at the server's own budget.
    monkeypatch.setenv("MEMD_FEDERATED_DEADLINE_MS", "400")
    release = _hold(monkeypatch)
    try:
        for surface in SURFACES:
            start = time.monotonic()
            _, stores, skipped, _ = _federated(agent, surface, {**body, "deadline_ms": 600000})
            assert time.monotonic() - start < 5
            assert stores == ["amber"] and skipped == [{"store": "team", "reason": "deadline"}]
    finally:
        release.set()


def test_federated_small_deadline_ms_skips_a_slow_store_and_names_it(team, monkeypatch):
    agent = _agent(team, {"team": "write"})
    monkeypatch.setenv("MEMD_FEDERATED_DEADLINE_MS", "20000")
    # The store's hung recalls pile up across these requests, charged to this caller; past the
    # limit it would be reported busy rather than cut off (test_a_deadline_the_caller_shortened_...).
    monkeypatch.setattr(share, "FEDERATION_STUCK_PER_CALLER", 64)
    release = _hold(monkeypatch)
    line = "[Stores not searched: team (deadline).]"
    body = {"query": "archive backup", "stores": ["amber", "team"], "deadline_ms": 300}
    try:
        for surface in SURFACES:
            start = time.monotonic()
            text, stores, skipped, rendering = _federated(agent, surface, body)
            assert time.monotonic() - start < 10        # far inside the 20 s server budget
            assert stores == ["amber"] and skipped == [{"store": "team", "reason": "deadline"}]
            assert "### [amber] archive-backup-window" in text and text.endswith("\n\n" + line)
            assert text.count("Stores not searched") == 1
            assert {e["store"] for e in rendering["excerpts"]} == {"amber"}
            # The line is inside max_chars, whatever the notes would have used.
            for max_chars in (256, 600):
                tight = _federated(agent, surface, {**body, "max_chars": max_chars})[0]
                assert tight.endswith(line) and len(tight) <= max_chars, (surface, max_chars, tight)
    finally:
        release.set()


def test_deadline_ms_is_ignored_by_a_single_store_recall(team, monkeypatch):
    agent = _agent(team, {"team": "write"})
    body = {"query": "archive backup"}
    rest = agent.post("/recall", json=body).json()
    tool = _mcp(agent, "recall", body)["structuredContent"]
    assert rest["notes"] and "stores" not in rest and "stores" not in tool
    # Slower than the deadline it is sent: nothing is cut off and nothing is added.
    _patch_recall(monkeypatch, lambda profile: time.sleep(0.25))
    for value in (100, "soon"):
        assert agent.post("/recall", json={**body, "deadline_ms": value}).json() == rest
        assert _mcp(agent, "recall", {**body, "deadline_ms": value})["structuredContent"] == tool


def test_federated_recall_ignores_a_deadline_ms_that_is_not_a_positive_number(team, monkeypatch):
    agent = _agent(team, {"team": "write"})

    def behave(profile):
        if profile == "team":
            time.sleep(0.2)     # longer than the 100 ms floor a clamped value would give
            return []
    _patch_recall(monkeypatch, behave)
    body = {"query": "archive backup", "stores": ["amber", "team"]}
    for surface in SURFACES:
        plain = _federated(agent, surface, body)
        assert plain[1] == ["amber", "team"] and "Stores not searched" not in plain[0]
        for value in ("soon", 0, -250):
            assert _federated(agent, surface, {**body, "deadline_ms": value}) == plain, (surface, value)
        # The same store is skipped as soon as the value is a usable number.
        assert _federated(agent, surface, {**body, "deadline_ms": 1})[1] == ["amber"]


def _seed_proxy_facts(agent):
    """One current fact about the same subject in each store, with a different answer."""
    for store, host in (("amber", "gpuhost"), ("team", "vmhost")):
        r = agent.post("/save", json={"profile": store, "title": f"Proxy host for {store}",
                                      "body": f"The nginx proxy runs on {host} since 2026-03-01."})
        assert r.status_code == 200, r.text
        facts.run(_store_cfg(store), use_llm=False)


def _proxy_fact(store, host):
    return (f"### [{store}] Current facts (timeline)\n"
            f"- The nginx proxy | runs on | {host} (since 2026-03-01; proxy-host-for-{store})")


def test_federated_recall_appends_the_first_stores_current_facts(team, monkeypatch):
    agent = _agent(team, {"team": "write"})
    _seed_proxy_facts(agent)
    mine, theirs = _proxy_fact("amber", "gpuhost"), _proxy_fact("team", "vmhost")
    for surface in SURFACES:
        text, _, _, rendering = _federated(agent, surface, {"query": "where does the nginx proxy run",
                                                            "stores": ["amber", "team"]})
        assert "Current facts" not in text and "current_facts" not in rendering

        now = {"query": PROXY_NOW, "stores": ["amber", "team"]}
        text, stores, _, rendering = _federated(agent, surface, now)
        assert stores == ["amber", "team"] and rendering["current_facts"] == 1
        assert text.endswith("\n\n" + mine) and text.count("Current facts") == 1
        assert "[team] proxy-host-for-team" in text     # the other store is searched; its facts are not shown

        text = _federated(agent, surface, {**now, "stores": ["team", "amber"]})[0]
        assert text.endswith("\n\n" + theirs) and text.count("Current facts") == 1
        assert _federated(agent, surface, {"query": PROXY_NOW, "scope": "all"})[0].endswith("\n\n" + mine)
        # A block that does not fit is left out.
        tight, _, _, rendering = _federated(agent, surface, {**now, "max_chars": 256})
        assert "Current facts" not in tight and "current_facts" not in rendering and len(tight) <= 256

    # The single-store block is unlabelled, as before.
    single = agent.post("/recall", json={"query": PROXY_NOW}).json()["context"]
    assert single.endswith("\n\n" + mine.replace("[amber] ", ""))

    # A failing facts block never fails the recall.
    def boom(*a, **kw):
        raise RuntimeError("facts index unreadable")
    monkeypatch.setattr(facts, "append_current_facts", boom)
    for surface in SURFACES:
        text, stores, _, _ = _federated(agent, surface, {"query": PROXY_NOW, "stores": ["amber", "team"]})
        assert stores == ["amber", "team"] and "[amber] proxy-host-for-amber" in text
        assert "Current facts" not in text


def test_federated_recall_names_failed_stores_and_skips_the_facts_of_a_skipped_store(team, monkeypatch):
    agent = _agent(team, {"team": "write"})
    _seed_proxy_facts(agent)
    failing = {"team": PermissionError("refused for a private reason")}

    def behave(profile):
        if profile in failing:
            raise failing[profile]
    _patch_recall(monkeypatch, behave)
    now = {"query": PROXY_NOW, "stores": ["amber", "team"]}
    for surface in SURFACES:
        text, stores, skipped, _ = _federated(agent, surface, now)
        assert stores == ["amber"] and skipped == [{"store": "team", "reason": "PermissionError"}]
        # The facts block stays; the line about skipped stores closes the context.
        assert text.endswith(_proxy_fact("amber", "gpuhost") + "\n\n[Stores not searched: team (PermissionError).]")
        assert "private reason" not in text

    # The first store is the one that failed: its facts are not shown either.
    failing.clear()
    failing["amber"] = RuntimeError("boom")
    for surface in SURFACES:
        text, stores, skipped, _ = _federated(agent, surface, now)
        assert stores == ["team"] and skipped == [{"store": "amber", "reason": "RuntimeError"}]
        assert "Current facts" not in text and "[team] proxy-host-for-team" in text
        assert text.endswith("\n\n[Stores not searched: amber (RuntimeError).]")

    # Every store skipped: the line alone.
    failing["team"] = OSError("gone")
    for surface in SURFACES:
        text, stores, skipped, rendering = _federated(agent, surface, {**now, "max_chars": 256})
        assert text == "[Stores not searched: amber (RuntimeError), team (OSError).]"
        assert stores == [] and [s["store"] for s in skipped] == ["amber", "team"]
        assert rendering["excerpts"] == []


def _many(store, query, k, **kw):
    if store.startswith("down"):
        raise ConnectionError("unreachable")
    return [{"slug": f"{store}-note-{i}", "matched": True, "body": f"backup window {i} " + "detail " * 60}
            for i in range(6)] + [{"slug": f"{store}-core", "matched": False, "body": "core", "importance": 5}]


def test_federated_recall_unchanged_without_skips_or_a_present_state_query():
    from memd.render import render_result
    for extra in ({}, {"max_chars": 700}, {"max_chars": 256}, {"core_limit": 0}, {"include_core": False}):
        args = {"query": "backup window", "k": 4, **extra}
        out = share.federated_recall(args, ["a", "b"], _many, budget_ms=5000)
        assert out["stores"] == ["a", "b"] and out["stores_skipped"] == []
        assert out["shaped"] == render_result(
            out["notes"], top_n=4, max_chars=extra.get("max_chars", 14000), query="backup window",
            core_limit=extra.get("core_limit"), include_core=extra.get("include_core", True))


def test_federated_recall_skipped_line_always_fits_max_chars():
    line = "[Stores not searched: down-1 (ConnectionError), down-2 (ConnectionError).]"
    for max_chars in (256, 257, 300, 330, 331, 332, 400, 700, 2000, 14000):
        out = share.federated_recall({"query": "backup window", "k": 6, "max_chars": max_chars},
                                     ["a", "down-1", "b", "down-2"], _many, budget_ms=5000)
        text = out["shaped"]["text"]
        assert len(text) <= max_chars and text.endswith(line), (max_chars, len(text))
        assert out["stores"] == ["a", "b"]
        # What the rendering reports as shown is what the text holds.
        assert all(f"### [{e['store']}] {e['slug']}" in text for e in out["shaped"]["excerpts"])
    assert "### [a] a-note-0" in text and "## Recalled memory (memd)" in text

    # A line longer than the whole budget is cut; the budget is never exceeded.
    down = [f"down-{i:02d}-{'x' * 40}" for i in range(12)]
    out = share.federated_recall({"query": "backup window", "k": 6, "max_chars": 256}, down, _many,
                                 budget_ms=5000)
    text = out["shaped"]["text"]
    assert len(text) == 256 and text.startswith("[Stores not searched: down-00-") and out["stores"] == []


def test_federated_facts_block_and_skipped_line_share_max_chars(team):
    _seed_proxy_facts(_agent(team, {"team": "write"}))
    block = _proxy_fact("amber", "gpuhost")
    line = "[Stores not searched: down-1 (ConnectionError).]"
    seen = set()
    for max_chars in range(256, 560):
        out = share.federated_recall({"query": PROXY_NOW, "k": 6, "max_chars": max_chars},
                                     ["amber", "down-1"], _many, budget_ms=5000)
        text, shown = out["shaped"]["text"], "current_facts" in out["shaped"]
        assert len(text) <= max_chars and text.endswith(line), max_chars
        # The block is whole or absent: the line that follows it never costs it its tail.
        assert text.endswith(f"{block}\n\n{line}") if shown else "Current facts" not in text, max_chars
        seen.add((shown, len(text) == max_chars))
    # Left out when it does not fit, and admitted up to the last character of the budget.
    assert (False, False) in seen and (True, True) in seen


def test_federated_facts_block_is_never_read_from_an_ungranted_store(team):
    agent = _agent(team, {"team": "write"})
    _seed_proxy_facts(agent)
    # `other` states a current fact about the same subject; the member has no grant on it.
    keeper = _client(team, _issue(team["admin"], _admin_id(), "other", "write"))
    r = keeper.post("/save", json={"title": "Proxy host for other",
                                   "body": "The nginx proxy runs on vaulthost since 2026-03-01."})
    assert r.status_code == 200, r.text
    facts.run(_store_cfg("other"), use_llm=False)
    granted = team["admin"].post("/recall", json={"query": PROXY_NOW, "stores": ["other", "amber"]}).json()
    assert granted["context"].endswith("\n\n" + _proxy_fact("other", "vaulthost"))

    for stores in (["other", "amber"], ["other"]):
        r = agent.post("/recall", json={"query": PROXY_NOW, "stores": stores})
        assert r.status_code == 403 and "vaulthost" not in r.text and "Current facts" not in r.text
        result = _mcp(agent, "recall", {"query": PROXY_NOW, "stores": stores})
        assert result["isError"] is True
        assert "vaulthost" not in json.dumps(result) and "Current facts" not in json.dumps(result)
    for surface in SURFACES:
        text, stores, _, _ = _federated(agent, surface, {"query": PROXY_NOW, "scope": "all"})
        assert stores == ["amber", "team"] and "vaulthost" not in text
        assert text.endswith("\n\n" + _proxy_fact("amber", "gpuhost"))


@pytest.fixture(autouse=True)
def _no_stuck_recalls():
    """Each test starts with no store counted as stuck; an earlier test's released recalls may still be ending."""
    share._federation_stuck.clear()
    yield
    share._federation_stuck.clear()


def _until(predicate, seconds=5.0):
    end = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


def test_a_hung_store_is_reported_busy_and_costs_a_bounded_number_of_threads(monkeypatch):
    monkeypatch.setattr(share, "FEDERATION_STUCK_PER_STORE", 2)
    # The server's own deadline runs out, so what is given up counts against the store, for everyone.
    monkeypatch.setenv("MEMD_FEDERATED_DEADLINE_MS", "150")
    release = threading.Event()
    started = []

    def one(store):
        if store == "slow":
            started.append(store)
            release.wait(10)
            return []
        return [{"slug": f"{store}-0", "matched": True, "body": "b"}]

    def threads():
        return sum(t.name == "memd-federated" for t in threading.enumerate())
    try:
        for n in range(5):
            notes, skipped = share.federate(["slow", "fast"], one, k=4)
            # The healthy store is served every time; the hung one is cut off, then busy.
            assert [note["slug"] for note in notes] == ["fast-0"]
            assert skipped == [{"store": "slow", "reason": "deadline" if n < 2 else "busy"}], n
        assert started == ["slow", "slow"] and share._federation_stuck == {("store", "slow"): 2}
        _until(lambda: threads() == 2)      # the fast recalls' threads have ended; the hung two remain
        # The same caller, asking for a healthy store alone, is not held up.
        start = time.monotonic()
        notes, skipped = share.federate(["fast"], one, k=4, budget_ms=3000)
        assert skipped == [] and time.monotonic() - start < 1
    finally:
        release.set()
    # Once the hung recalls end, the store is recalled again.
    _until(lambda: share._federation_stuck == {})
    notes, skipped = share.federate(["slow", "fast"], one, k=4, budget_ms=3000)
    assert skipped == [] and started == ["slow"] * 3


def test_a_deadline_the_caller_shortened_makes_a_store_busy_only_for_that_caller(monkeypatch):
    monkeypatch.setenv("MEMD_FEDERATED_DEADLINE_MS", "20000")
    release = threading.Event()
    started = []

    def one(store):
        if store == "slow":
            started.append(actor.get_actor())
            release.wait(10)
            return []
        return [{"slug": f"{store}-0", "matched": True, "body": "b"}]

    def as_caller(label, budget_ms=150):
        def go():
            actor.set_actor(label)
            return share.federate(["slow", "fast"], one, k=4, budget_ms=budget_ms)
        return contextvars.copy_context().run(go)
    try:
        for n in range(3):
            notes, skipped = as_caller("looper")
            assert [note["slug"] for note in notes] == ["fast-0"]
            assert skipped == [{"store": "slow", "reason": "deadline" if n < 2 else "busy"}], n
        # Given up at a deadline it chose: charged to that caller, not to the store.
        assert share._federation_stuck == {("caller", ("label", "looper"), "slow"): 2, ("shortened", "slow"): 2}
        # Another caller still recalls the store.
        notes, skipped = as_caller("someone-else")
        assert skipped == [{"store": "slow", "reason": "deadline"}]
        assert started == ["looper", "looper", "someone-else"]
    finally:
        release.set()
    _until(lambda: share._federation_stuck == {})


def test_however_many_callers_shorten_the_deadline_the_threads_they_leave_stay_bounded(monkeypatch):
    monkeypatch.setenv("MEMD_FEDERATED_DEADLINE_MS", "20000")
    monkeypatch.setattr(share, "FEDERATION_STUCK_SHORTENED", 3)
    release = threading.Event()
    started = []

    def one(store):
        started.append(actor.get_actor())
        release.wait(10)
        return []

    def as_caller(label):
        def go():
            actor.set_actor(label)
            return share.federate(["slow"], one, k=4, budget_ms=150)
        return contextvars.copy_context().run(go)
    try:
        # Each caller has its own share, but together they reach the ceiling for shortened deadlines.
        reasons = [as_caller(f"caller-{n}")[1][0]["reason"] for n in range(5)]
        assert reasons == ["deadline", "deadline", "deadline", "busy", "busy"]
        assert started == ["caller-0", "caller-1", "caller-2"]
        assert share._federation_stuck[("shortened", "slow")] == 3
        # A request that keeps the server's deadline is not held up by them.
        monkeypatch.setenv("MEMD_FEDERATED_DEADLINE_MS", "150")

        def full():
            actor.set_actor("patient")
            return share.federate(["slow"], one, k=4)
        assert contextvars.copy_context().run(full)[1] == [{"store": "slow", "reason": "deadline"}]
        assert started[-1] == "patient" and share._federation_stuck[("store", "slow")] == 1
    finally:
        release.set()
    _until(lambda: share._federation_stuck == {})


def test_a_remote_caller_without_an_account_or_token_cannot_shorten_the_deadline(monkeypatch):
    monkeypatch.setenv("MEMD_FEDERATED_DEADLINE_MS", "2000")

    def remote():
        access.remote_request.set(True)
        actor.set_actor("")
        return share.request_deadline_ms(250), share.request_deadline_ms(None)
    assert contextvars.copy_context().run(remote) == (2000, 2000)
    # A local caller (the CLI, an in-process hook) still may.
    assert share.request_deadline_ms(250) == 250 and share.request_deadline_ms(None) == 2000


def test_one_request_runs_a_few_of_its_stores_at_a_time_and_none_after_its_deadline(monkeypatch):
    monkeypatch.setattr(share, "FEDERATION_PER_CALL", 2)
    lock = threading.Lock()
    now = {"running": 0, "most": 0}
    ran = []
    release = threading.Event()

    def one(store):
        ran.append(store)
        with lock:
            now["running"] += 1
            now["most"] = max(now["most"], now["running"])
        try:
            if store.startswith("slow"):
                release.wait(10)
                return []
            time.sleep(0.05)
            return [{"slug": f"{store}-0", "matched": True, "body": "b"}]
        finally:
            with lock:
                now["running"] -= 1
    # More stores than may run at once: all are served, two at a time.
    notes, skipped = share.federate([f"fast-{n}" for n in range(5)], one, k=8, budget_ms=3000)
    assert skipped == [] and sorted(n["slug"] for n in notes) == [f"fast-{n}-0" for n in range(5)]
    assert now["most"] == 2
    try:
        # Its two places are held by hung recalls when the deadline comes: the third store never starts.
        notes, skipped = share.federate(["slow-a", "slow-b", "fast-x"], one, k=8, budget_ms=150)
        assert skipped == [{"store": s, "reason": "deadline"} for s in ("slow-a", "slow-b", "fast-x")]
        assert "fast-x" not in ran
    finally:
        release.set()
    _until(lambda: share._federation_stuck == {})
