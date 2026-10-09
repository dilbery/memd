"""Team sharing with provenance (memd.share): publish, federated recall, upstream drift.

Security first. Hermetic: temp Git/SQLite stores behind the administered access
layer (accounts, grants, scoped tokens), model backends replaced by stubs, the
socket guard blocks everything else. Every refusal also asserts that nothing
was written and nothing from the refused store came back.
"""
import json
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

import memd.recall as recall_mod
import memd.save as save_mod
from memd import control, server, share, sources
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
