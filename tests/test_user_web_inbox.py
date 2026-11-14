"""The personal console's Inbox view (memd.user_web + memd.inbox).

Hermetic: synthetic OIDC (respx), temp per-user stores; reuses the console
fixtures from tests/test_user_web.py.
"""
import sqlite3
import uuid

import pytest
import respx

import memd.save as save_mod
from memd import inbox, user_memory
from memd.identity import bind_identity, clear_identity
from memd.store import read_note
from tests.test_admin_web import key  # noqa: F401  (fixture)
from tests.test_personal_tokens import directory  # noqa: F401  (fixture)
from tests.test_user_memory import stores  # noqa: F401  (fixture)
from tests.test_user_web import ORIGIN, headers, sign_in, userweb  # noqa: F401  (fixture)


def _propose(user, fact, **kw):
    bind_identity(user)
    try:
        cfg = user_memory.owner_config()
        return inbox.propose(fact, cfg.profile, cfg=cfg, **kw)["id"], cfg
    finally:
        clear_identity()


@pytest.fixture(autouse=True)
def _no_vectors(monkeypatch):
    monkeypatch.setattr(save_mod, "_vector_near_matches", lambda cfg, body: [])


def test_inbox_page_lists_own_candidates_and_approves_with_edits(userweb, key):  # noqa: F811
    client, registry, _ = userweb
    cid, cfg = _propose("alice@example.invalid", {"title": "Desk lamp", "body": "The desk lamp is on plug 3.",
                                                 "tags": ["home"]}, proposer="alice-laptop")
    bob_id, _ = _propose("bob@example.invalid", {"title": "Bob secret", "body": "Only for Bob's store."})
    with respx.mock(assert_all_called=False) as router:
        assert sign_in(client, router, key)[0].status_code == 303
        page = client.get("/memories/inbox")
        assert page.status_code == 200 and 'src="/assets/inbox.js"' in page.text
        assert "default-src 'none'" in page.headers["content-security-policy"]
        assert client.get("/assets/inbox.js").status_code == 200
        assert client.get("/assets/inbox.css").headers["content-type"].startswith("text/css")
        listed = client.get("/memories/api/inbox").json()
        assert [i["id"] for i in listed["items"]] == [cid] and listed["counts"]["pending"] == 1
        assert "Bob secret" not in str(listed)
        assert client.get(f"/memories/api/inbox/{bob_id}").status_code == 404
        assert client.get("/memories/api/inbox?profile=bob@example.invalid").status_code == 400
        item = client.get(f"/memories/api/inbox/{cid}").json()
        assert item["lint"]["action"] == "create" and item["proposer"] == "alice-laptop"
        h = headers(client, registry)
        # CSRF and origin are enforced on decisions.
        assert client.post(f"/memories/api/inbox/{cid}/approve", json={"edits": {}}).status_code == 403
        assert client.post(f"/memories/api/inbox/{cid}/approve", headers={**h, "Origin": "https://evil.invalid"},
                           json={"edits": {}}).status_code == 403
        assert client.post(f"/memories/api/inbox/{cid}/approve", headers=h,
                           json={"edits": {"host": "elsewhere"}}).status_code == 400
        r = client.post(f"/memories/api/inbox/{cid}/approve", headers=h,
                        json={"edits": {"body": "The desk lamp is on smart plug 3.", "tags": ["home", "lights"]}})
        assert r.status_code == 200, r.text
        assert r.json()["edited"] and r.json()["receipt"]["saved"]
        assert client.post(f"/memories/api/inbox/{cid}/approve", headers=h, json={"edits": {}}).status_code == 409
        assert client.get("/memories/api/inbox?status=approved").json()["total"] == 1
    note = read_note(cfg.clone, "desk-lamp")
    assert note.body == "The desk lamp is on smart plug 3." and note.tags == ["home", "lights"]
    assert note.saved_by == "alice-laptop"


def test_inbox_reject_and_unauthenticated_access(userweb, key):  # noqa: F811
    client, registry, _ = userweb
    assert client.get("/memories/api/inbox").status_code == 401
    assert client.get("/memories/inbox").status_code == 303
    cid, cfg = _propose("alice@example.invalid", {"title": "Wrong guess", "body": "The router is purple."})
    with respx.mock(assert_all_called=False) as router:
        sign_in(client, router, key)
        h = headers(client, registry)
        assert client.post(f"/memories/api/inbox/{cid}/reject", headers=h,
                           json={"reason": "not true", "edits": {}}).status_code == 400
        r = client.post(f"/memories/api/inbox/{cid}/reject", headers=h, json={"reason": "not true"})
        assert r.status_code == 200 and r.json()["status"] == "rejected"
        assert client.post(f"/memories/api/inbox/{cid}/reject", headers=h, json={}).status_code == 409
        assert client.post(f"/memories/api/inbox/{cid}/delete", headers=h, json={}).status_code == 404
        assert client.get("/memories/api/inbox/not-an-id").status_code == 400
    assert read_note(cfg.clone, "wrong-guess") is None


def _decision(cfg, cid):
    conn = sqlite3.connect(inbox.inbox_file(cfg.db))
    try:
        return conn.execute("SELECT status, reviewer, reviewer_account, proposer_account, proposer_credential "
                            "FROM candidates WHERE id=?", (cid,)).fetchone()
    finally:
        conn.close()


def test_console_decisions_record_the_signed_in_identity_as_the_reviewing_account(userweb, key):  # noqa: F811
    client, registry, _ = userweb
    kept, cfg = _propose("alice@example.invalid", {"title": "Desk fan", "body": "The desk fan is on plug 4."},
                         proposer="alice-laptop")
    dropped, _ = _propose("alice@example.invalid", {"title": "Wrong guess", "body": "The router is purple."})
    with respx.mock(assert_all_called=False) as router:
        sign_in(client, router, key)
        h = headers(client, registry)
        assert client.post(f"/memories/api/inbox/{kept}/approve", headers=h, json={"edits": {}}).status_code == 200
        assert client.post(f"/memories/api/inbox/{dropped}/reject", headers=h, json={}).status_code == 200
    account = "identity:alice@example.invalid"
    assert _decision(cfg, kept) == ("approved", "alice@example.invalid", account, None, None)
    assert _decision(cfg, dropped) == ("rejected", "alice@example.invalid", account, None, None)
    assert read_note(cfg.clone, "desk-fan").saved_by == "alice-laptop"


def test_a_personal_token_proposes_as_its_owner_with_its_own_id(userweb, key, directory, stores):  # noqa: F811
    client, registry, _ = userweb
    issued = registry.issue_personal(subject="alice-sub", store="alice@example.invalid", label="laptop",
                                     operation_id=str(uuid.uuid4()))
    r = client.post("/propose", headers={"Authorization": "Bearer " + issued["secret"]},
                    json={"title": "Desk fan", "body": "The desk fan is on plug 4."})
    assert r.status_code == 200, r.text
    cid, cfg, account = r.json()["id"], stores["alice@example.invalid"], "identity:alice@example.invalid"
    # The account is the token's bound identity, the credential the token itself.
    assert _decision(cfg, cid) == ("pending", None, None, account, issued["token"]["id"])
    with respx.mock(assert_all_called=False) as router:
        sign_in(client, router, key)
        assert client.post(f"/memories/api/inbox/{cid}/approve", headers=headers(client, registry),
                           json={"edits": {}}).status_code == 200
    assert _decision(cfg, cid) == ("approved", "alice@example.invalid", account, account, issued["token"]["id"])
    assert read_note(cfg.clone, "desk-fan").saved_by == "laptop"


def test_the_console_reviewer_is_only_for_the_signed_in_users_own_store():
    bind_identity("alice@example.invalid")
    try:
        with pytest.raises(PermissionError):
            inbox.console_reviewer("bob@example.invalid")
        with pytest.raises(PermissionError):
            inbox.console_reviewer("")
        assert inbox.console_reviewer("alice@example.invalid").account == "identity:alice@example.invalid"
    finally:
        clear_identity()
    with pytest.raises(PermissionError):
        inbox.console_reviewer("alice@example.invalid")
