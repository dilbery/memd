"""Entity pages (memd.entities, GET /entities, GET /entities/{kind}/{name}).

Hermetic: a temp Git store with notes, seeded fact rows, verification markers and
an inbox side file. Host names are placeholders; no model services.
"""
import datetime as dt
import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import memd.server as server_mod
from memd import entities
from memd.config import Config
from memd.index import open_db

TOKEN = "e" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TODAY = dt.date.today()
WEB = Path(__file__).resolve().parents[1] / "memd" / "web"


def _date(days_ago: int) -> str:
    return (TODAY - dt.timedelta(days=days_ago)).isoformat()


def _note(slug, *, tags=("misc",), host="any", extra="", body=None):
    return (f"---\ntitle: {slug.replace('-', ' ').title()}\nslug: {slug}\nprofile: amber\n"
            f"host: {host}\nimportance: 3\ntags: [{', '.join(tags)}]\n{extra}---\n"
            f"{body or 'Body of ' + slug + '.'}\n")


NOTES = {
    "proxy-config": _note("proxy-config", tags=("network", "backup"), host="node-a",
                          extra=f"volatility: state\nobserved_at: {_date(120)}\n",
                          body="The web proxy listens on port 8443."),
    "dns-resolver": _note("dns-resolver", tags=("network",), host="node-b", extra=(
        "verify:\n- tcp: node-b:53\nverification:\n  status: failed\n"
        f"  checked_at: {_date(2)}\n  failed:\n  - 'tcp node-b:53: connection refused'\n"),
        body="Resolver forwards to node-a when the cache is cold."),
    "cache-old": _note("cache-old", tags=("backup",)),
    "cache-new": _note("cache-new", tags=("cache-layer",), extra=f"verified_at: {_date(1)}\n"),
    "mirror-a": _note("mirror-a", tags=("repo:tools", "ops")),
    "mirror-b": _note("mirror-b", tags=("ops",)),
    "lonely": _note("lonely", tags=("singleton",)),
    # superseded in the index below (as a save with supersedes leaves it)
    "old-node-a": _note("old-node-a", host="node-a", body="node-a used to run everything."),
}


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def store(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "config", "user.email", "test@memd")
    _git(clone, "config", "user.name", "memd-test")
    for slug, text in NOTES.items():
        (clone / f"{slug}.md").write_text(text)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "seed")
    db = tmp_path / "memd.db"
    for key, value in {"MEMD_CLONE": str(clone), "MEMD_DB": str(db), "MEMD_PROFILE": "amber",
                       "MEMD_AMBER_CLONE": str(clone), "MEMD_AMBER_DB": str(db),
                       "MEMD_TOKEN": TOKEN, "MEMD_LOCAL_HOST": "any",
                       "MEMD_EMBED_URL": "http://127.0.0.1:9",
                       "MEMD_RERANK_URL": "http://127.0.0.1:9"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MEMD_HOST_NAMES", raising=False)
    entities._cache.clear()
    cfg = Config.from_env(env_file=None)
    from memd.refresh import ensure_lexical
    ensure_lexical(cfg)
    _seed_facts(cfg)
    return cfg


def _seed_facts(cfg):
    db = open_db(cfg.db, dim=cfg.embed_dim)
    blobs = dict(db.execute("SELECT slug, git_blob FROM notes"))
    rows = [
        ("cache-old", "Cache", "runs on", "node-a", "2026-01-01"),
        ("cache-new", "cache", "runs on", "node-b", "2026-03-01"),
        ("mirror-a", "mirror", "points to", "mirror-one.example", None),
        ("mirror-b", "mirror", "points to", "mirror-two.example", None),
        ("old-node-a", "legacy app", "runs on", "node-a", "2025-01-01"),
    ]
    with db:
        for slug, s, p, o, since in rows:
            db.execute("INSERT INTO facts(slug,git_blob,subject,predicate,object,subject_key,"
                       "predicate_key,object_key,valid_from,stated_to,valid_to,closed_by,method) "
                       "VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,'pattern')",
                       (slug, blobs[slug], s, p, o, s.casefold(), p, o, since))
        from memd.facts import close_facts
        close_facts(db)
        db.execute("UPDATE notes SET superseded_by='proxy-config' WHERE slug='old-node-a'")
    db.close()


def _seed_inbox(cfg, *items):
    from memd.inbox import _connect, inbox_file
    conn = _connect(inbox_file(cfg.db))
    with conn:
        for i, (title, body, status) in enumerate(items):
            conn.execute("INSERT INTO candidates(id, created, source, proposer, title, body, meta, "
                         "content_hash, status) VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"{i:016x}", time.time() - i, "agent", "laptop", title, body,
                          json.dumps({"tags": []}), f"h{i}", status))
    conn.close()


def _by_name(listing):
    return {(e["kind"], e["name"]): e for e in listing["entities"]}


def test_list_aggregates_hosts_services_and_tags(store):
    out = entities.list_entities(store, "amber")
    assert out["ok"] and out["profile"] == "amber" and not out["truncated"]
    got = _by_name(out)
    node_a = got[("host", "node-a")]
    # scoped (proxy-config), fact object (cache-old), mention (dns-resolver); never the superseded note
    assert node_a["notes"] == 3 and node_a["current_facts"] == 0
    assert node_a["stale"] == 2 and node_a["failed_verification"] == 1
    assert node_a["last_activity"] == max(_date(120), "2026-01-01")     # as-of date or fact date
    node_b = got[("host", "node-b")]
    assert node_b["current_facts"] == 1 and node_b["failed_verification"] == 1
    cache = got[("service", "cache")]
    assert cache["notes"] == 3 and cache["current_facts"] == 1 and cache["display"] in ("cache", "Cache")
    assert got[("service", "mirror")]["current_facts"] == 2
    assert ("tag", "backup") in got and ("tag", "network") in got
    # generic, prefixed, single-use tags and superseded notes' facts are not entities
    names = {name for _, name in got}
    assert not names & {"ops", "repo:tools", "singleton", "misc", "legacy app"}


def test_entity_detail(store):
    _seed_inbox(store, ("Move node-a backups", "node-a gets a new disk.", "pending"),
                ("Unrelated", "nothing here", "pending"),
                ("Old", "node-a", "rejected"))
    page = entities.entity(store, "amber", "service", "cache")
    assert page["ok"] and page["entity"]["name"] == "cache"
    assert [(f["object"], f["since"], f["source"]) for f in page["current_facts"]["items"]] == \
        [("node-b", "2026-03-01", "cache-new")]
    assert page["current_facts"]["items"][0]["source_title"] == "Cache New"
    line = [(f["object"], f["valid_from"], f["valid_to"], f["current"]) for f in page["timeline"]["items"]]
    assert line == [("node-b", "2026-03-01", None, True), ("node-a", "2026-01-01", "2026-03-01", False)]
    notes = {n["slug"]: n for n in page["notes"]["items"]}
    assert set(notes) == {"cache-old", "cache-new", "dns-resolver"}     # "the cache is cold"
    assert notes["cache-new"]["verification"] == {"status": "verified", "verified_at": _date(1)}
    assert "fact" in notes["cache-old"]["why"]
    related = {(r["kind"], r["name"]): r for r in page["related"]["items"]}
    assert related[("host", "node-b")]["fact_link"] and related[("host", "node-a")]["fact_link"]

    host = entities.entity(store, "amber", "host", "node-a")
    notes = {n["slug"]: n for n in host["notes"]["items"]}
    assert set(notes) == {"proxy-config", "cache-old", "dns-resolver"}
    assert notes["proxy-config"]["why"] == ["scoped"] and notes["proxy-config"]["stale"]
    assert notes["proxy-config"]["stale_reason"] == "state, older than 30 days"
    assert notes["dns-resolver"]["why"] == ["mention"]
    assert notes["dns-resolver"]["verification"]["status"] == "failed"
    assert host["current_facts"]["total"] == 0 and host["timeline"]["total"] == 1
    assert host["timeline"]["items"][0]["role"] == "object"
    # counts only for a caller who may not review; the pending candidate is found by mention
    assert host["inbox"] == {"available": True, "pending": 1, "can_review": False, "items": None,
                             "truncated": False}
    reviewer = entities.entity(store, "amber", "host", "node-a", can_review=True)
    assert [i["title"] for i in reviewer["inbox"]["items"]] == ["Move node-a backups"]

    mirror = entities.entity(store, "amber", "service", "Mirror")
    assert mirror["conflicts"]["total"] == 1
    values = {v["object"] for v in mirror["conflicts"]["items"][0]["values"]}
    assert values == {"mirror-one.example", "mirror-two.example"}
    tag = entities.entity(store, "amber", "tag", "backup")
    assert {n["slug"] for n in tag["notes"]["items"]} == {"proxy-config", "cache-old"}


@pytest.mark.parametrize("kind,name", [("host", "nowhere"), ("service", "node-a"), ("planet", "cache"),
                                       ("tag", "ops"), ("tag", "singleton"), ("host", "any"),
                                       ("host", " "), ("service", "x" * 500)])
def test_unknown_entities(store, kind, name):
    with pytest.raises(entities.UnknownEntity):
        entities.entity(store, "amber", kind, name)


def test_caps_and_entities_past_the_cap(store, monkeypatch):
    monkeypatch.setattr(entities, "MAX_ENTITIES", 2)
    monkeypatch.setattr(entities, "DETAIL_NOTES", 1)
    out = entities.list_entities(store, "amber", fresh=True)
    assert len(out["entities"]) == 2 and out["truncated"] and out["total"] > 2
    listed = {e["name"] for e in out["entities"]}
    assert "backup" not in listed
    page = entities.entity(store, "amber", "tag", "backup")      # past the cap: still served
    assert page["notes"]["total"] == 2 and len(page["notes"]["items"]) == 1


def test_cache_expires(store, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(entities, "_clock", lambda: clock[0])
    calls = []
    real = entities._build
    monkeypatch.setattr(entities, "_build", lambda *a: calls.append(1) or real(*a))
    entities.list_entities(store, "amber")
    entities.list_entities(store, "amber")
    assert entities.entity(store, "amber", "host", "node-a")["cached"] is False
    assert entities.entity(store, "amber", "host", "node-a")["cached"] is True
    assert len(calls) == 1
    clock[0] += entities.CACHE_TTL_S + 1
    entities.list_entities(store, "amber")
    assert len(calls) == 2


def test_read_only_and_no_index(tmp_path, monkeypatch):
    clone = tmp_path / "c"
    clone.mkdir()
    for key, value in {"MEMD_CLONE": str(clone), "MEMD_DB": str(tmp_path / "none.db"),
                       "MEMD_PROFILE": "amber"}.items():
        monkeypatch.setenv(key, value)
    entities._cache.clear()
    cfg = Config.from_env(env_file=None)
    assert entities.list_entities(cfg, "amber")["ok"] is False
    with pytest.raises(entities.UnknownEntity):
        entities.entity(cfg, "amber", "host", "node-a")
    assert not (tmp_path / "none.db").exists()


def test_routes(store, monkeypatch):
    with TestClient(server_mod.create_token_app()) as client:
        assert client.get("/entities").status_code == 401
        assert client.get("/entities/host/node-a", headers={"Authorization": "Bearer wrong"}).status_code == 401
        r = client.get("/entities", headers=AUTH)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        assert ("host", "node-a") in _by_name(r.json())
        page = client.get("/entities/host/node-a", headers=AUTH)
        assert page.status_code == 200 and page.json()["entity"]["notes"] == 3
        assert page.json()["inbox"]["can_review"] is False
        missing = client.get("/entities/host/elsewhere", headers=AUTH)
        assert missing.status_code == 404 and missing.json() == {"detail": "No such entity in this store."}
        assert client.get("/entities/planet/node-a", headers=AUTH).status_code == 404
        assert client.get("/entities/service/a%2Fb", headers=AUTH).status_code == 404
        monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "1")
        assert client.get("/entities?profile=cobalt", headers=AUTH).status_code in (400, 403)
        assert client.get("/entities/host/node-a?profile=cobalt", headers=AUTH).status_code in (400, 403)


def test_routes_do_not_open_the_store_without_auth(monkeypatch):
    monkeypatch.setenv("MEMD_TOKEN", "test-token")
    monkeypatch.setattr(entities, "_model", lambda *a, **k: pytest.fail("unauthorized read"))
    client = TestClient(server_mod.app)
    assert client.get("/entities").status_code == 401
    assert client.get("/entities/host/node-a").status_code == 401


def test_a_stats_only_token_is_refused(store, tmp_path, monkeypatch):
    import uuid
    from memd.registry import Registry, principal
    monkeypatch.setenv("MEMD_CONTROL_DB", str(tmp_path / "control" / "control.db"))
    monkeypatch.delenv("MEMD_TOKEN", raising=False)
    reg = Registry()
    reg.initialize()
    principal.set(None)
    stats_only = reg.issue(actor="a", operation_id=str(uuid.uuid4()), label="grafana", owner="Ops",
                           purpose="monitoring", stores=["amber"], operations=["stats"], days=30)
    reader = reg.issue(actor="a", operation_id=str(uuid.uuid4()), label="dash", owner="Ops",
                       purpose="dashboard", stores=["amber"], operations=["read", "stats"], days=30)
    with TestClient(server_mod.create_token_app()) as client:
        monitor = {"Authorization": "Bearer " + stats_only["secret"]}
        assert client.get("/entities", headers=monitor).status_code == 403
        assert client.get("/entities/host/node-a", headers=monitor).status_code == 403
        ok = client.get("/entities/host/node-a", headers={"Authorization": "Bearer " + reader["secret"]})
        assert ok.status_code == 200 and ok.json()["inbox"]["items"] is None


def test_entities_view_renders_text_only():
    source = (WEB / "entities.js").read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                 "new Function", "setAttribute(\"on", "javascript:"):
        assert sink not in source
    page = (WEB / "index.html").read_text()
    assert '<script src="/ui/assets/entities.js" defer></script>' in page
    assert 'id="tab-entities"' in page and 'id="entities-panel"' in page
    client = TestClient(server_mod.app)
    response = client.get("/ui/assets/entities.js")
    assert response.status_code == 200 and response.headers["x-content-type-options"] == "nosniff"
