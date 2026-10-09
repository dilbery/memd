"""Memory health report (memd.insights, GET /insights, mem-health).

Hermetic: a temp Git store whose seed commit predates the usage window, a seeded
usage log, fact rows and verification markers. No model services.
"""
import datetime as dt
import json
import os
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

import memd.server as server_mod
from memd import insights, usage
from memd.config import Config
from memd.index import open_db

TOKEN = "h" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DAY = 86400
# memd.insights ages notes against the UTC date; seeding from the local date made
# every age off by one wherever the local and UTC dates differ.
TODAY = dt.datetime.now(dt.timezone.utc).date()


def _date(days_ago: int) -> str:
    return (TODAY - dt.timedelta(days=days_ago)).isoformat()


def _note(slug, *, tags=("ops",), host="any", importance=3, extra="", body=None):
    return (f"---\ntitle: {slug.replace('-', ' ').title()}\nslug: {slug}\nprofile: amber\n"
            f"host: {host}\nimportance: {importance}\ntags: [{', '.join(tags)}]\n{extra}---\n"
            f"{body or 'Body of ' + slug + '.'}\n")


OLD_NOTES = {
    "proxy-config": _note("proxy-config", tags=("network", "proxy"), host="node-a",
                          extra=f"volatility: state\nobserved_at: {_date(120)}\n"),
    "backup-plan": _note("backup-plan", tags=("backup",), importance=5,
                         extra=f"verified_at: {_date(3)}\nvolatility: state\n"),
    "dns-resolver": _note("dns-resolver", tags=("network",), host="node-b", extra=(
        "verify:\n- tcp: 127.0.0.1:53\nverification:\n  status: failed\n"
        f"  checked_at: {_date(2)}\n  failed:\n  - 'tcp 127.0.0.1:53: connection refused'\n")),
    "cache-old": _note("cache-old", tags=("cache",)),
    "cache-new": _note("cache-new", tags=("cache",)),
    "mirror-a": _note("mirror-a", tags=("mirror",)),
    "mirror-b": _note("mirror-b", tags=("mirror",)),
    "untagged-note": _note("untagged-note", tags=()),
    "summary-network": _note("summary-network", tags=("network",), extra=(
        "kind: summary\nsources: [proxy-config, dns-resolver, gone-note]\n"
        "source_revisions: {proxy-config: stale-blob}\n")),
}


def _git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


@pytest.fixture
def store(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "config", "user.email", "test@memd")
    _git(clone, "config", "user.name", "memd-test")
    for slug, text in OLD_NOTES.items():
        (clone / f"{slug}.md").write_text(text)
    _git(clone, "add", "-A")
    old = f"@{int(time.time() - 200 * DAY)} +0000"
    _git(clone, "commit", "-q", "-m", "seed", env={**os.environ, "GIT_AUTHOR_DATE": old,
                                                     "GIT_COMMITTER_DATE": old})
    (clone / "brand-new.md").write_text(_note("brand-new", tags=("cache",)))
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "new")
    db = tmp_path / "memd.db"
    for key, value in {"MEMD_CLONE": str(clone), "MEMD_DB": str(db), "MEMD_PROFILE": "amber",
                       "MEMD_AMBER_CLONE": str(clone), "MEMD_AMBER_DB": str(db),
                       "MEMD_TOKEN": TOKEN, "MEMD_LOCAL_HOST": "any", "MEMD_USAGE_LOG": "on",
                       "MEMD_EMBED_URL": "http://127.0.0.1:9",
                       "MEMD_RERANK_URL": "http://127.0.0.1:9"}.items():
        monkeypatch.setenv(key, value)
    insights._cache.clear()
    cfg = Config.from_env(env_file=None)
    from memd.refresh import ensure_lexical
    ensure_lexical(cfg)
    return cfg


def _seed_usage(cfg, *, start_days=30):
    now = time.time()
    t0 = now - start_days * DAY
    ids = []
    # cache-old: shown 4 times, never read. backup-plan: shown 4, read 3.
    for i in range(4):
        ids.append(usage.log_recall(cfg.db, f"query {i}", ["backup-plan", "cache-old"],
                                    cfg=cfg, now=t0 + i * DAY))
    for i in range(3):
        usage.log_read(cfg.db, "backup-plan", cfg=cfg, recall_id=ids[i], now=t0 + i * DAY + 60)
    usage.log_recall(cfg.db, "proxy", ["proxy-config"], cfg=cfg, now=now - DAY)
    usage.log_read(cfg.db, "proxy-config", cfg=cfg, now=now - DAY + 30)
    return ids


def _seed_facts(cfg):
    db = open_db(cfg.db, dim=cfg.embed_dim)
    blobs = dict(db.execute("SELECT slug, git_blob FROM notes"))
    rows = [
        # cache-old's only fact is closed by cache-new's newer one: a supersede candidate.
        ("cache-old", "cache", "runs on", "node-a", "2026-01-01"),
        ("cache-new", "cache", "runs on", "node-b", "2026-03-01"),
        # Undated current facts disagree across two notes: a contradiction cluster.
        ("mirror-a", "mirror", "points to", "10.10.1.1", None),
        ("mirror-b", "mirror", "points to", "10.10.1.2", None),
    ]
    with db:
        for slug, s, p, o, since in rows:
            db.execute("INSERT INTO facts(slug,git_blob,subject,predicate,object,subject_key,"
                       "predicate_key,object_key,valid_from,stated_to,valid_to,closed_by,method) "
                       "VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,'pattern')",
                       (slug, blobs[slug], s, p, o, s, p, o, since))
        from memd.facts import close_facts
        close_facts(db)
    db.close()


def _report(cfg, **kw):
    clone, db = insights.store_paths(cfg, "amber")
    return insights.compute(cfg, "amber", clone=clone, db_path=db, **kw)


def test_full_report_on_a_seeded_store(store):
    _seed_usage(store)
    _seed_facts(store)
    r = _report(store)
    assert r["ok"] and r["profile"] == "amber"
    assert r["coverage"]["notes"] == 10 and r["coverage"]["pending_vectors"] == 10

    u = r["usage"]
    assert u["available"] and u["recalls"] == 5 and u["reads"] == 4 and 29 < u["window_days"] < 31

    never = r["never_recalled"]
    slugs = [i["slug"] for i in never["items"]]
    # brand-new was committed inside the window and is not judged; recalled notes are excluded.
    assert "brand-new" not in slugs and "cache-old" not in slugs and "backup-plan" not in slugs
    assert "mirror-a" in slugs and never["eligible"] == 9 and never["basis"] == "git"
    assert never["total"] == 6

    assert [i["slug"] for i in r["recalled_unread"]["items"]] == ["cache-old"]
    assert r["recalled_unread"]["items"][0]["shown"] == 4
    useful = r["most_useful"]["items"]
    assert useful[0]["slug"] == "backup-plan" and useful[0]["reads"] == 3 and useful[0]["read_rate"] == 0.75

    stale = {i["slug"]: i for i in r["stale"]["items"]}
    assert set(stale) == {"proxy-config", "dns-resolver"}
    assert stale["proxy-config"]["age_days"] == 120 and "state" in stale["proxy-config"]["reason"]
    assert stale["dns-resolver"]["reason"] == "verification failed"
    failed = r["failed_verifications"]["items"]
    assert [f["slug"] for f in failed] == ["dns-resolver"] and "connection refused" in failed[0]["probe"]

    heat = r["heatmap"]
    assert heat["buckets"] == ["0-7d", "8-30d", "31-90d", "91-365d", ">1y", "undated"]
    tags = {row["key"]: row for row in heat["tags"]["rows"]}
    assert tags["network"]["stale"] == 2 and tags["network"]["cells"][3] == 1  # proxy-config at 120 days
    assert tags["(untagged)"]["notes"] == 1 and tags["cache"]["cells"][5] == 3
    hosts = {row["key"]: row for row in heat["hosts"]["rows"]}
    assert hosts["node-a"]["stale"] == 1 and hosts["any"]["notes"] == 8
    assert heat["tags"]["rows"][0]["key"] == "network"      # most stale first

    sup = r["supersede_candidates"]
    assert [i["slug"] for i in sup["items"]] == ["cache-old"] and sup["items"][0]["closed_by"] == ["cache-new"]
    con = r["contradictions"]
    assert con["total"] == 1
    cluster = con["items"][0]
    assert cluster["subject"] == "mirror" and cluster["notes"] == 2
    assert sorted(v["object"] for v in cluster["values"]) == ["10.10.1.1", "10.10.1.2"]

    summ = r["stale_summaries"]
    assert summ["summaries"] == 1 and summ["items"][0]["slug"] == "summary-network"
    reasons = " ".join(summ["items"][0]["reasons"])
    assert "gone-note no longer exists" in reasons and "proxy-config changed" in reasons

    s = r["summary"]
    assert s["stale"] == 2 and s["contradictions"] == 1 and s["supersede_candidates"] == 1
    assert s["pending_review"] is None and r["inbox"]["available"] is False


def test_no_data_reasons_instead_of_zeros(store):
    r = _report(store)
    for key in ("never_recalled", "recalled_unread", "most_useful"):
        assert r[key]["available"] is False and "No recalls" in r[key]["reason"]
    for key in ("supersede_candidates", "contradictions"):
        assert r[key]["available"] is False and "mem-facts" in r[key]["reason"]
    assert r["summary"]["never_recalled"] is None and r["summary"]["contradictions"] is None
    assert r["usage"]["available"] is False

    off = Config.from_env({**os.environ, "MEMD_USAGE_LOG": "off"}, env_file=None)
    assert "MEMD_USAGE_LOG=off" in _report(off)["usage"]["reason"]


def test_sources_without_markers_are_reported_as_no_data(store):
    db = open_db(store.db, dim=store.embed_dim)
    with db:
        db.execute("UPDATE notes SET metadata='{}'")
    db.close()
    r = _report(store)
    assert r["stale"]["available"] is False and "volatility" in r["stale"]["reason"]
    assert r["failed_verifications"]["available"] is False
    assert r["stale_summaries"]["available"] is False and "mem-summarize" in r["stale_summaries"]["reason"]
    assert r["heatmap"]["available"] is True     # dates and tags are optional; the grid still renders


def test_young_usage_log_and_young_store(store):
    now = time.time()
    usage.log_recall(store.db, "q", ["backup-plan"], cfg=store, now=now - 3600)
    r = _report(store)
    assert r["usage"]["available"] is False and "less than a day" in r["usage"]["reason"]
    usage.log_recall(store.db, "q", ["backup-plan"], cfg=store, now=now - 300 * DAY)
    r = _report(store)
    # The whole history is younger than a 300-day window... except retention caps it at 90 days.
    assert r["usage"]["window_days"] == pytest.approx(90, abs=0.2)
    assert r["never_recalled"]["available"] is True


def test_mtime_fallback_when_git_cannot_answer(store, monkeypatch):
    _seed_usage(store)
    monkeypatch.setattr(insights, "_paths_before", lambda clone, cutoff: None)
    r = _report(store)
    # Checked-out files are all fresh, so nothing is certainly older than the window.
    assert r["never_recalled"]["available"] is False and r["never_recalled"]["basis"] == "mtime"
    past = time.time() - 100 * DAY
    for md in store.clone.glob("*.md"):
        os.utime(md, (past, past))
    r = _report(store)
    assert r["never_recalled"]["available"] and r["never_recalled"]["eligible"] == 10


def test_lists_are_capped_with_totals(store):
    _seed_usage(store)
    r = _report(store, limit=2)
    assert r["limit"] == 2
    assert len(r["never_recalled"]["items"]) == 2 and r["never_recalled"]["total"] == 6
    assert _report(store, limit=10**6)["limit"] == insights.MAX_LIMIT
    monkey_rows = insights.HEATMAP_ROWS
    try:
        insights.HEATMAP_ROWS = 2
        heat = _report(store)["heatmap"]["tags"]
        assert len(heat["rows"]) == 2 and heat["total_rows"] == 6
    finally:
        insights.HEATMAP_ROWS = monkey_rows


def test_report_never_writes(store):
    _seed_usage(store)
    def files():   # SQLite's WAL reader files (-wal/-shm) are not data
        return {p: p.stat().st_mtime_ns for p in store.db.parent.glob("memd*")
                if not p.name.endswith(("-wal", "-shm"))}
    before = files()
    _report(store)
    after = files()
    assert before == after
    assert not insights.store_paths(store, "amber")[1].with_name("memd.inbox.db").exists()


def test_cache_expires_after_ttl(store, monkeypatch):
    calls = []
    real = insights.compute
    monkeypatch.setattr(insights, "compute", lambda *a, **k: calls.append(1) or real(*a, **k))
    clock = [1000.0]
    monkeypatch.setattr(insights, "_clock", lambda: clock[0])
    first = insights.report(store, "amber")
    assert first["cached"] is False
    clock[0] += 30
    second = insights.report(store, "amber")
    assert second["cached"] is True and second["age_s"] == 30.0 and len(calls) == 1
    assert insights.report(store, "amber", limit=5)["cached"] is False      # keyed by limit
    assert insights.report(store, "amber", fresh=True)["cached"] is False
    clock[0] += insights.CACHE_TTL_S + 1
    assert insights.report(store, "amber")["cached"] is False
    assert len(calls) == 4


def test_route_requires_auth_and_serves_the_report(store, monkeypatch):
    _seed_usage(store)
    with TestClient(server_mod.create_token_app()) as client:
        assert client.get("/insights").status_code == 401
        assert client.get("/insights", headers={"Authorization": "Bearer wrong"}).status_code == 401
        r = client.get("/insights?limit=3", headers=AUTH)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        body = r.json()
        assert body["ok"] and body["profile"] == "amber" and body["limit"] == 3
        assert body["never_recalled"]["total"] == 6
        assert client.get("/insights?limit=3", headers=AUTH).json()["cached"] is True
        assert client.get("/insights?limit=0", headers=AUTH).status_code == 422
        monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "1")
        assert client.get("/insights?profile=cobalt", headers=AUTH).status_code in (400, 403)


def test_route_does_not_open_the_store_without_auth(monkeypatch):
    monkeypatch.setenv("MEMD_TOKEN", "test-token")
    monkeypatch.setattr(insights, "report", lambda *a, **k: pytest.fail("unauthorized report"))
    assert TestClient(server_mod.app).get("/insights").status_code == 401


def test_cli_text_and_json(store, capsys):
    _seed_usage(store)
    _seed_facts(store)
    assert insights.main([]) == 0
    text = capsys.readouterr().out
    assert text.startswith("memory health for amber")
    assert "never recalled: 6" in text and "cache-old  shown 4" in text
    assert "mirror | points to: 10.10.1.1 (mirror-a); 10.10.1.2 (mirror-b)" in text
    assert "age by tag" in text and "inbox: no data" in text
    assert insights.main(["--json", "--limit", "1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and len(out["never_recalled"]["items"]) == 1


def test_cli_without_index(tmp_path, monkeypatch, capsys):
    clone = tmp_path / "c"
    clone.mkdir()
    monkeypatch.setenv("MEMD_CLONE", str(clone))
    monkeypatch.setenv("MEMD_DB", str(tmp_path / "none.db"))
    monkeypatch.setenv("MEMD_PROFILE", "amber")
    assert insights.main([]) == 1
    assert "no index yet" in capsys.readouterr().out
    assert not (tmp_path / "none.db").exists()


def test_a_stats_only_token_cannot_read_the_report(store, tmp_path, monkeypatch):
    """The report names notes and states facts; a monitoring (stats) token gets counts only."""
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
    _seed_facts(store)
    with TestClient(server_mod.create_token_app()) as client:
        monitor = {"Authorization": "Bearer " + stats_only["secret"]}
        assert client.get("/stats", headers=monitor).status_code == 200
        assert client.get("/insights?fresh=true", headers=monitor).status_code == 403
        ok = client.get("/insights?fresh=true", headers={"Authorization": "Bearer " + reader["secret"]})
        assert ok.status_code == 200 and ok.json()["ok"] is not False
