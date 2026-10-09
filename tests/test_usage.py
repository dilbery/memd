"""Recall usage log (memd.usage): logging over HTTP and MCP, signals, the bounded
boost and golden-set candidate export. Hermetic: temp stores, no model backends."""
import asyncio
import json
import os
import random
import sqlite3
import stat

import pytest
from fastapi.testclient import TestClient

import memd.mcp as mcp_mod
import memd.server as server_mod
from memd import actor, recall as recall_mod, usage
from memd.config import Config
from memd.recall_eval import load_golden

TOKEN = "u" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MCP_HEADERS = {**AUTH, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


@pytest.fixture
def store(git_clone, tmp_path, monkeypatch):
    """The conftest clone with its lexical index built; model backends unreachable."""
    from memd.refresh import ensure_lexical
    db = tmp_path / "memd.db"
    for key, value in {"MEMD_CLONE": str(git_clone), "MEMD_DB": str(db), "MEMD_PROFILE": "amber",
                       "MEMD_AMBER_CLONE": str(git_clone), "MEMD_AMBER_DB": str(db),
                       "MEMD_TOKEN": TOKEN, "MEMD_LOCAL_HOST": "any", "MEMD_USAGE_LOG": "on",
                       "MEMD_EMBED_URL": "http://127.0.0.1:9",
                       "MEMD_RERANK_URL": "http://127.0.0.1:9"}.items():
        monkeypatch.setenv(key, value)
    # Keyword-only recall: model backends are down, which recall tolerates.
    monkeypatch.setattr(recall_mod, "embed_with_deadline", lambda *a, **k: None)
    monkeypatch.setattr(recall_mod, "rerank", lambda *a, **k: None)
    actor.set_actor("")
    usage._weights_cache.clear()
    cfg = Config.from_env(env_file=None)
    ensure_lexical(cfg)
    return cfg


def _rows(cfg, table):
    conn = sqlite3.connect(usage.usage_path(cfg.db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY ts")]
    finally:
        conn.close()


def _mcp(client, name, arguments, rpc_id=1):
    r = client.post("/mcp/", headers=MCP_HEADERS, json={
        "jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


# --------------------------------------------------------------------------- HTTP


def test_http_recall_returns_recall_id_and_logs_the_shown_matches(store):
    with TestClient(server_mod.create_token_app()) as client:
        r = client.post("/recall", headers=AUTH, json={"query": "Lemonade  Vulkan TUNING"})
    assert r.status_code == 200, r.text
    body = r.json()
    # Existing fields are untouched; recall_id is additive.
    assert {"ok", "profile", "context", "rendering", "notes"} <= set(body)
    rid = body["recall_id"]
    assert isinstance(rid, str) and len(rid) == 16
    path = usage.usage_path(store.db)
    assert path.parent == store.db.parent and path.exists()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    [row] = _rows(store, "recalls")
    assert row["id"] == rid and row["caller"] == "legacy"
    assert row["query"] == "lemonade vulkan tuning"
    assert row["query_hash"] == usage.query_hash("lemonade vulkan tuning")
    shown = [e["slug"] for e in body["rendering"]["excerpts"]]
    assert json.loads(row["slugs"]) == shown and "gpuhost-inference-tuning" in shown


def test_http_read_with_recall_id_is_an_explicit_positive(store):
    with TestClient(server_mod.create_token_app()) as client:
        rid = client.post("/recall", headers=AUTH, json={"query": "lemonade vulkan"}).json()["recall_id"]
        r = client.post("/read", headers=AUTH, json={"slug": "gpuhost-inference-tuning", "recall_id": rid})
        assert r.status_code == 200 and r.json()["slug"] == "gpuhost-inference-tuning"
        assert "recall_id" not in r.json()
    [read] = _rows(store, "reads")
    assert (read["slug"], read["recall_id"], read["explicit"], read["caller"]) == \
        ("gpuhost-inference-tuning", rid, 1, "legacy")
    [rec] = usage.load_recalls(usage.usage_path(store.db))
    assert rec.read == {"gpuhost-inference-tuning"}


def test_http_read_without_recall_id_is_attributed_by_caller_and_window(store):
    with TestClient(server_mod.create_token_app()) as client:
        rid = client.post("/recall", headers=AUTH, json={"query": "lemonade vulkan"}).json()["recall_id"]
        client.post("/read", headers=AUTH, json={"slug": "gpuhost-inference-tuning"})
        # A slug the recall never returned is logged but credited to no recall.
        client.post("/read", headers=AUTH, json={"slug": "vmhost-proxmox-vm"})
        # A bogus recall_id falls back to inference instead of failing the read.
        assert client.post("/read", headers=AUTH, json={"slug": "repo-hosting-policy",
                                                        "recall_id": "not-an-id"}).status_code == 200
    reads = {r["slug"]: r for r in _rows(store, "reads")}
    assert reads["gpuhost-inference-tuning"]["recall_id"] == rid
    assert reads["gpuhost-inference-tuning"]["explicit"] == 0
    assert reads["vmhost-proxmox-vm"]["recall_id"] is None


def test_mcp_over_http_round_trips_recall_id(store):
    with TestClient(server_mod.create_token_app()) as client:
        out = _mcp(client, "recall", {"query": "proxmox trackr"})
        assert out["isError"] is False
        rid = out["structuredContent"]["recall_id"]
        assert isinstance(rid, str) and len(rid) == 16
        read = _mcp(client, "read", {"slug": "vmhost-proxmox-vm", "recall_id": rid}, rpc_id=2)
        assert read["isError"] is False
    [rec] = usage.load_recalls(usage.usage_path(store.db))
    assert rec.id == rid and rec.read == {"vmhost-proxmox-vm"} and rec.caller == "legacy"


def test_mcp_tool_schemas_advertise_recall_id():
    tools = {t.name: t for t in asyncio.run(mcp_mod.list_tools())}
    assert "recall_id" in tools["read"].input_schema["properties"]
    assert tools["read"].input_schema["required"] == []
    assert "recall_id" in tools["recall"].output_schema["properties"]


def test_stdio_mcp_recall_and_read_log(store):
    out = asyncio.run(mcp_mod.call_tool("recall", {"query": "lemonade vulkan"}))
    rid = out.structured_content["recall_id"]
    asyncio.run(mcp_mod.call_tool("read", {"slug": "gpuhost-inference-tuning", "recall_id": rid}))
    [rec] = usage.load_recalls(usage.usage_path(store.db))
    assert rec.caller == "" and rec.read == {"gpuhost-inference-tuning"}


def test_off_switch_records_nothing(store, monkeypatch):
    monkeypatch.setenv("MEMD_USAGE_LOG", "off")
    with TestClient(server_mod.create_token_app()) as client:
        body = client.post("/recall", headers=AUTH, json={"query": "lemonade vulkan"}).json()
        client.post("/read", headers=AUTH, json={"slug": "gpuhost-inference-tuning"})
    assert body["recall_id"] is None and body["ok"]
    assert not usage.usage_path(store.db).exists()


def test_hash_mode_keeps_no_query_text(store, monkeypatch):
    monkeypatch.setenv("MEMD_USAGE_LOG", "hash")
    with TestClient(server_mod.create_token_app()) as client:
        assert client.post("/recall", headers=AUTH, json={"query": "lemonade vulkan"}).json()["recall_id"]
    [row] = _rows(store, "recalls")
    assert row["query"] is None and row["query_hash"] == usage.query_hash("lemonade vulkan")
    assert usage.golden_candidates(usage.load_recalls(usage.usage_path(store.db))) == []


def test_no_index_means_no_log_file(tmp_path, monkeypatch):
    """A store whose index does not exist gets no usage file (nothing is created elsewhere)."""
    db = tmp_path / "absent" / "memd.db"
    monkeypatch.setenv("MEMD_DB", str(db))
    monkeypatch.setenv("MEMD_CLONE", str(tmp_path / "clone"))
    monkeypatch.setenv("MEMD_TOKEN", TOKEN)
    monkeypatch.setenv("MEMD_USAGE_LOG", "on")
    class Hit:
        def to_dict(self):
            return {"slug": "a", "body": "alpha", "matched": True}
    monkeypatch.setattr(server_mod, "_core_recall", lambda *a, **k: [Hit()])
    body = TestClient(server_mod.app).post("/recall", headers=AUTH, json={"query": "alpha"}).json()
    assert body["ok"] and body["recall_id"] is None
    assert not (tmp_path / "absent").exists()


def test_logging_failure_never_fails_recall(store, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(usage, "_connect", boom)
    with TestClient(server_mod.create_token_app()) as client:
        r = client.post("/recall", headers=AUTH, json={"query": "lemonade vulkan"})
        assert r.status_code == 200 and r.json()["recall_id"] is None
        assert client.post("/read", headers=AUTH, json={"slug": "gpuhost-inference-tuning"}).status_code == 200


def test_queries_never_reach_logs(store, monkeypatch, caplog):
    monkeypatch.setattr(usage, "_connect", lambda *a: (_ for _ in ()).throw(OSError("nope")))
    usage.log_recall(store.db, "secret wifi passphrase", ["a"], cfg=store)
    assert caplog.records and all("secret" not in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- retention and config


def test_retention_prunes_old_rows(tmp_path):
    db = tmp_path / "memd.db"
    db.touch()
    cfg = Config(usage_retention_days=90)
    now = 2_000_000_000.0
    old = usage.log_recall(db, "old query", ["a"], cfg=cfg, now=now - 91 * 86400)
    usage.log_read(db, "a", cfg=cfg, recall_id=old, now=now - 91 * 86400 + 5)
    kept = usage.log_recall(db, "recent query", ["a"], cfg=cfg, now=now - 89 * 86400)
    usage.log_recall(db, "new query", ["b"], cfg=cfg, now=now)
    ids = [r.id for r in usage.load_recalls(usage.usage_path(db))]
    assert old not in ids and kept in ids and len(ids) == 2
    conn = sqlite3.connect(usage.usage_path(db))
    assert conn.execute("SELECT COUNT(*) FROM reads").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("env, expected", [
    ({}, ("on", 90, False)),
    ({"MEMD_USAGE_LOG": "OFF", "MEMD_USAGE_RETENTION_DAYS": "7", "MEMD_USAGE_BOOST": "on"}, ("off", 7, True)),
    ({"MEMD_USAGE_LOG": "hash", "MEMD_USAGE_RETENTION_DAYS": "junk"}, ("hash", 90, False)),
    ({"MEMD_USAGE_LOG": "maybe", "MEMD_USAGE_RETENTION_DAYS": "0"}, ("on", 1, False)),
])
def test_config_parsing(env, expected):
    cfg = Config.from_env(env, env_file=None)
    assert (cfg.usage_log, cfg.usage_retention_days, cfg.usage_boost) == expected


# --------------------------------------------------------------------------- signals


def _recall(slugs, read=(), query="q", ts=0.0, rid=None):
    return usage.Recall(rid or f"{random.getrandbits(64):016x}", ts, "c",
                        usage.query_hash(query), query, list(slugs), set(read))


def test_signals_positive_and_weak_negatives():
    # Read at rank 5: everything above is skipped; rank 6 was never examined.
    r = _recall(["a", "b", "c", "d", "e", "f"], read={"e"})
    assert r.negatives() == ["a", "b", "c", "d"]
    # Read at rank 1: only the rest of the top 3 count as skipped.
    assert _recall(["a", "b", "c", "d"], read={"a"}).negatives() == ["b", "c"]
    # No read at all says nothing (the excerpt may have been enough).
    assert _recall(["a", "b", "c"]).negatives() == []
    sig = usage.note_signals([r, _recall(["e", "a"], read={"e"}), _recall(["a"])])
    assert (sig["e"].shown, sig["e"].reads, sig["e"].skips) == (2, 2, 0)
    assert (sig["a"].shown, sig["a"].reads, sig["a"].skips) == (3, 0, 2)
    assert (sig["f"].shown, sig["f"].reads, sig["f"].skips) == (1, 0, 0)


def test_read_outside_window_is_not_a_positive(tmp_path):
    db = tmp_path / "memd.db"
    db.touch()
    cfg = Config()
    rid = usage.log_recall(db, "q", ["a", "b"], cfg=cfg, now=1000.0)
    usage.log_read(db, "a", cfg=cfg, recall_id=rid, now=1000.0 + usage.READ_WINDOW_S + 1)
    usage.log_read(db, "b", cfg=cfg, recall_id=rid, now=1000.0 + 60)
    [rec] = usage.load_recalls(usage.usage_path(db))
    assert rec.read == {"b"}


def test_attribution_is_per_caller(tmp_path):
    db = tmp_path / "memd.db"
    db.touch()
    cfg = Config()
    usage.log_recall(db, "q", ["a"], cfg=cfg, caller="one", now=1000.0)
    usage.log_read(db, "a", cfg=cfg, caller="two", now=1010.0)
    assert usage.load_recalls(usage.usage_path(db))[0].read == set()


# --------------------------------------------------------------------------- learning


def test_weights_are_bounded_smoothed_and_neutral_without_evidence():
    assert usage.note_weights({}) == {}
    recalls = [_recall(["hot", "cold"], read={"hot"}) for _ in range(200)]
    recalls += [_recall(["once", "x", "y"], read={"y"})]
    w = usage.note_weights(usage.note_signals(recalls))
    assert all(1 - usage.BOOST_CAP - 1e-9 <= v <= 1 + usage.BOOST_CAP + 1e-9 for v in w.values())
    assert w["hot"] > 1.0 > w["cold"]
    assert w["hot"] == pytest.approx(1 + usage.BOOST_CAP, abs=0.01)
    # One skip moves a note far less than 200: the prior dominates thin evidence.
    assert 1 - w["once"] < (1 - w["cold"]) / 2
    # Returned but never judged (no read in its recall): no weight at all.
    w2 = usage.note_weights(usage.note_signals(recalls + [_recall(["unseen"])]))
    assert "unseen" not in w2


def test_bounded_reorder_never_moves_an_item_beyond_the_cap():
    rng = random.Random(7)
    for _ in range(300):
        base = [f"n{i}" for i in range(rng.randint(0, 30))]
        score = {s: rng.random() for s in base}
        out = usage.bounded_reorder(base, score)
        assert sorted(out) == sorted(base)
        assert all(abs(out.index(s) - base.index(s)) <= usage.MAX_SHIFT for s in base)
    base = ["a", "b", "c", "d", "e"]
    assert usage.bounded_reorder(base, {"e": 9.0}) == ["a", "b", "e", "c", "d"]
    assert usage.bounded_reorder(base, {s: -i for i, s in enumerate(base)}) == base


def test_fuse_applies_weights_only_when_given():
    from memd.query import Distilled
    vec = [f"n{i}" for i in range(10)]
    distilled = Distilled(terms=(), idf={}, n_docs=0, dropped_saturated=(), dropped_absent=())
    plain = recall_mod._fuse(vec, [], {}, distilled)
    assert plain == vec
    boosted = recall_mod._fuse(vec, [], {}, distilled, weights={"n9": 1 + usage.BOOST_CAP})
    assert boosted.index("n9") >= 9 - usage.MAX_SHIFT and boosted != plain


def test_boost_is_off_by_default(store, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("usage weights consulted with MEMD_USAGE_BOOST unset")
    monkeypatch.setattr(usage, "weights_for", forbidden)
    assert store.usage_boost is False
    recall_mod.recall("lemonade vulkan", cfg=store)


def test_boost_on_reads_the_log_and_traces_weights(store, monkeypatch):
    monkeypatch.setattr(usage, "weights_for", lambda db: {"gpuhost-inference-tuning": 1.1})
    import dataclasses
    trace = {}
    notes = recall_mod.recall("lemonade vulkan", cfg=dataclasses.replace(store, usage_boost=True), trace=trace)
    assert trace["usage_weights"] == {"gpuhost-inference-tuning": 1.1}
    assert any(n.slug == "gpuhost-inference-tuning" for n in notes)


def test_weights_for_reads_and_caches(tmp_path):
    db = tmp_path / "memd.db"
    db.touch()
    cfg = Config()
    usage._weights_cache.clear()
    assert usage.weights_for(db) == {}
    usage._weights_cache.clear()
    for _ in range(5):
        rid = usage.log_recall(db, "q", ["a", "b"], cfg=cfg)
        usage.log_read(db, "b", cfg=cfg, recall_id=rid)
    w = usage.weights_for(db)
    assert w["b"] > 1.0 > w["a"]
    usage.log_recall(db, "q", ["z"], cfg=cfg)
    assert usage.weights_for(db) is w      # cached within WEIGHTS_TTL_S


# --------------------------------------------------------------------------- export and eval


def _seeded(tmp_path):
    db = tmp_path / "memd.db"
    db.touch()
    cfg = Config()
    for query, shown, read in [("Where is the backup job?", ["backup-job", "nas"], ["backup-job"]),
                               ("where is the  BACKUP job", ["backup-job", "nas"], ["backup-job", "nas"]),
                               ("printer ip", ["printer"], []),
                               ("vpn setup", ["vpn", "wg"], ["wg"])]:
        rid = usage.log_recall(db, query, shown, cfg=cfg)
        for slug in read:
            usage.log_read(db, slug, cfg=cfg, recall_id=rid)
    return db


def test_export_golden_rows(tmp_path):
    db = _seeded(tmp_path)
    rows = usage.golden_candidates(usage.load_recalls(usage.usage_path(db)))
    # Case, spacing and punctuation variants of one question are one query.
    assert [r["query"] for r in rows] == ["where is the backup job", "vpn setup"]
    backup = rows[0]
    assert backup["gold"] == [{"slug": "backup-job", "grade": 2}, {"slug": "nas", "grade": 2}]
    assert backup["source"] == "usage" and backup["needs_review"] is True
    assert backup["category"] == "usage" and backup["id"].startswith("usage-")
    assert backup["split"] in {"dev", "test"}
    assert backup["split"] == usage._split(usage.query_hash(backup["query"]))
    strict = usage.golden_candidates(usage.load_recalls(usage.usage_path(db)), min_reads=2)
    assert [g["slug"] for r in strict for g in r["gold"]] == ["backup-job"]


def test_split_scheme_is_one_in_three_test():
    hashes = [usage.query_hash(f"query {i}") for i in range(3000)]
    share = sum(usage._split(h) == "test" for h in hashes) / len(hashes)
    assert 0.29 < share < 0.38


def test_export_cli_writes_owner_only_file_and_excludes_known_queries(tmp_path, capsys):
    db = _seeded(tmp_path)
    existing = tmp_path / "golden.jsonl"
    existing.write_text(json.dumps({"id": "q1", "query": "VPN setup", "category": "real",
                                    "gold": [{"slug": "wg", "grade": 2}]}) + "\n")
    out = tmp_path / "candidates.jsonl"
    assert usage.main(["--usage-db", str(usage.usage_path(db)), "export-golden", "--out", str(out),
                       "--exclude", str(existing), "--split", "fresh"]) == 0
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["split"] == "fresh" and "backup" in rows[0]["query"]
    # recall_eval accepts the exported rows as they are, and can skip them.
    loaded = load_golden(out)
    assert loaded[0]["needs_review"] is True and loaded[0]["source"] == "usage"
    assert load_golden(out, reviewed_only=True) == []


def test_recall_eval_rejects_malformed_usage_fields(tmp_path):
    bad = tmp_path / "bad.jsonl"
    row = {"id": "u1", "query": "q", "category": "usage", "gold": [{"slug": "a", "grade": 2}]}
    bad.write_text(json.dumps({**row, "needs_review": "yes"}) + "\n")
    with pytest.raises(ValueError, match="needs_review"):
        load_golden(bad)
    bad.write_text(json.dumps({**row, "source": 3}) + "\n")
    with pytest.raises(ValueError, match="source"):
        load_golden(bad)
    bad.write_text(json.dumps({**row, "source": "usage", "needs_review": False}) + "\n")
    assert load_golden(bad, reviewed_only=True)[0]["id"] == "u1"


def test_stats_cli_prints_counts_but_no_query_text(tmp_path, capsys):
    db = _seeded(tmp_path)
    assert usage.main(["--usage-db", str(usage.usage_path(db)), "stats"]) == 0
    text = capsys.readouterr().out
    assert "recalls 4" in text and "backup-job" in text
    assert "printer ip" not in text and "vpn setup" not in text
    assert usage.main(["--usage-db", str(usage.usage_path(db)), "stats", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["recalls"] == 4 and data["distinct_queries"] == 3 and data["recalls_with_read"] == 3
    assert data["read_rate_by_rank"]["1"] == pytest.approx(2 / 4)


def test_cli_on_missing_log_reports_empty(tmp_path, capsys):
    assert usage.main(["--usage-db", str(tmp_path / "none.usage.db"), "export-golden"]) == 0
    assert capsys.readouterr().out == ""
