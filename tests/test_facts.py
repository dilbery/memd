"""mem-facts: extraction, incremental re-extraction, closing, timeline queries,
the /timeline route and MCP tool, and recall's current-facts block.

Hermetic: temp git clones and SQLite indexes, respx-mocked chat endpoint.
"""
import asyncio
import json
import sqlite3
import subprocess

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import memd.mcp as mcp_mod
import memd.server as server_mod
from memd import facts as fx
from memd.config import Config
from memd.index import _FACTS_VERSION, _set_meta, open_db
from memd.store import Note

LLM_BASE = "http://llm.test"
LLM_URL = f"{LLM_BASE}/v1/chat/completions"
TOKEN = "facts-token"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _md(title, body, *, observed=None, slug=None, extra=""):
    fm = [f"title: {title}"]
    if slug:
        fm.append(f"slug: {slug}")
    if observed:
        fm.append(f"observed_at: '{observed}'")
    if extra:
        fm.append(extra)
    return "---\n" + "\n".join(fm) + "\n---\n" + body + "\n"


NOTES = {
    # Undated in the text: the note's observed_at dates the fact.
    "proxy-on-gpuhost.md": _md("Reverse proxy host", "The nginx proxy runs on gpuhost since 2026-03-01.",
                                slug="proxy-on-gpuhost", observed="2026-03-01"),
    "proxy-moved.md": _md("Proxy migration", "On 2026-08-19, nginx proxy moved to vmhost.",
                          slug="proxy-moved", observed="2026-08-20"),
    "kitchen.md": _md("Kitchen tap washer", "The kitchen tap takes a 12 mm washer.", slug="kitchen"),
}


def _commit(repo, files, message="edit"):
    for name, text in files.items():
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def clone(tmp_path):
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@memd")
    _git(repo, "config", "user.name", "memd-test")
    _commit(repo, NOTES, "seed")
    return repo


def _cfg(clone, llm=True):
    return Config(clone=clone, db=clone.parent / "memd.db",
                  llm_url=LLM_BASE if llm else None, llm_model="test-model" if llm else "")


def _reply(payload):
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


def _by_slug(replies):
    """A chat mock answering per note slug named in the prompt."""
    def handler(request):
        user = json.loads(request.content)["messages"][1]["content"]
        for slug, payload in replies.items():
            if f"[{slug}]" in user:
                return _reply(payload)
        return _reply({"facts": []})
    return handler


# --------------------------------------------------------------------------- parsing


def test_parse_facts_validates_items_and_defaults_dates():
    text = "```json\n" + json.dumps({"facts": [
        {"subject": "Grafana", "predicate": "Runs On", "object": "gpuhost.local",
         "valid_from": None, "valid_to": None},
        {"subject": "grafana", "predicate": "version", "object": "11.2",
         "valid_from": "2026-02-30", "valid_to": "2026-09-01"},     # invalid date -> default
        {"subject": "", "predicate": "uses", "object": "x", "valid_from": None, "valid_to": None},
        {"subject": "it", "predicate": "uses", "object": "x", "valid_from": None, "valid_to": None},
        "not an object",
    ]}) + "\n```"
    facts = fx.parse_facts(text, "2026-05-01")
    assert [(f.subject, f.keys[1], f.keys[2], f.valid_from, f.stated_to) for f in facts] == [
        ("Grafana", "runs on", "gpuhost", "2026-05-01", None),
        ("grafana", "version", "11.2", "2026-05-01", "2026-09-01"),
    ]
    assert all(f.method == "llm" for f in facts)


@pytest.mark.parametrize("reply", ["not json at all", "{\"facts\": \"nope\"}", "[1, 2]",
                                   "{\"facts\": [ {\"subject\": "])
def test_parse_facts_rejects_malformed_replies(reply):
    with pytest.raises(fx.FactParseError):
        fx.parse_facts(reply)


def test_parse_facts_finds_object_inside_prose():
    facts = fx.parse_facts('Sure! {"facts": [{"subject": "dns", "predicate": "points to", '
                           '"object": "10.10.1.20", "valid_from": "2026-04-02", "valid_to": null}]} ok')
    assert [(f.subject, f.object, f.valid_from) for f in facts] == [("dns", "10.10.1.20", "2026-04-02")]


def test_pattern_facts_explicit_phrasings_only():
    body = ("On 2026-08-19, nginx moved to vmhost.\n"
            "- The backup job was replaced by restic on 2026-05-01\n"
            "The DNS resolver uses unbound until 2026-06-01.\n"
            "Grafana runs on gpuhost.\n"             # undated state sentence: not a fact
            "It moved to vmhost.\n"                  # pronoun subject
            "Nightly backup moved to 03:00 to avoid the scrub.\n")   # a time, not a place
    got = [(f.subject, f.keys[1], f.object, f.valid_from, f.stated_to)
           for f in fx.pattern_facts(body, "2026-01-01")]
    assert got == [
        ("nginx", "runs on", "vmhost", "2026-08-19", None),
        ("The backup job", "replaced by", "restic", "2026-05-01", None),
        ("The DNS resolver", "uses", "unbound", "2026-01-01", "2026-06-01"),
    ]


def test_keys_are_light_and_reuse_canonical_host(monkeypatch):
    monkeypatch.setenv("MEMD_HOST_NAMES", "gpuhost=ws1")
    assert fx.entity_key("  The GPUHOST.lan. ") == "ws1"
    assert fx.entity_key("Home  Assistant") == "home assistant"
    assert fx.predicate_key("Migrated to") == "runs on"


# --------------------------------------------------------------------------- run


def test_run_without_model_uses_patterns_and_closes_older_fact(clone):
    report = fx.run(_cfg(clone, llm=False))
    assert report["model"] is False
    assert {e["slug"] for e in report["extracted"]} == {"proxy-on-gpuhost", "proxy-moved", "kitchen"}
    db = open_db(clone.parent / "memd.db")
    rows = db.execute("SELECT slug, object_key, valid_from, valid_to FROM facts ORDER BY valid_from").fetchall()
    db.close()
    assert rows == [("proxy-on-gpuhost", "gpuhost", "2026-03-01", "2026-08-19"),
                    ("proxy-moved", "vmhost", "2026-08-19", None)]
    assert report["supersede"] == [{"slug": "proxy-on-gpuhost", "facts": 1, "closed_by": ["proxy-moved"]}]
    # The notes themselves are never touched.
    assert _git(clone, "status", "--porcelain") == ""


@respx.mock
def test_run_is_incremental_by_blob(clone):
    route = respx.post(LLM_URL).mock(side_effect=_by_slug({
        "kitchen": {"facts": [{"subject": "kitchen tap", "predicate": "uses", "object": "12 mm washer",
                               "valid_from": None, "valid_to": None}]},
    }))
    cfg = _cfg(clone)
    first = fx.run(cfg)
    assert route.call_count == 3 and first["model"] is True
    assert all(e["llm"] for e in first["extracted"])
    body = json.loads(route.calls[0].request.content)
    assert body["response_format"]["type"] == "json_schema"

    again = fx.run(cfg)
    assert route.call_count == 3 and again["extracted"] == []

    _commit(clone, {"kitchen.md": _md("Kitchen tap washer", "The kitchen tap takes a 15 mm washer.",
                                      slug="kitchen")})
    third = fx.run(cfg)
    assert route.call_count == 4
    assert [e["slug"] for e in third["extracted"]] == ["kitchen"]

    # Deleting a note prunes its facts (with the note's index row, or by the job).
    _git(clone, "rm", "-q", "kitchen.md")
    _git(clone, "commit", "-q", "-m", "rm")
    fourth = fx.run(cfg)
    assert route.call_count == 4 and fourth["extracted"] == []
    db = open_db(cfg.db)
    assert db.execute("SELECT COUNT(*) FROM facts WHERE slug='kitchen'").fetchone()[0] == 0
    db.close()


@respx.mock
def test_run_budget_and_malformed_reply_keep_pattern_facts_and_retry(clone):
    respx.post(LLM_URL).mock(return_value=_reply("this is not json"))
    cfg = _cfg(clone)
    report = fx.run(cfg, max_notes=1)
    assert report["deferred"] == 2
    failed = [e for e in report["extracted"] if e["error"]]
    assert len(failed) == 1 and "JSON" in failed[0]["error"]
    # Pattern facts were still stored for every changed note.
    assert report["facts"] == 2
    db = open_db(cfg.db)
    assert db.execute("SELECT COUNT(*) FROM fact_sources WHERE llm=1").fetchone()[0] == 0
    db.close()
    # A later run retries the model on notes it has not seen, up to the budget.
    assert len(fx.run(cfg, max_notes=1)["extracted"]) == 1


@respx.mock
def test_json_schema_refusal_falls_back_to_plain_prompt(clone):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if "response_format" in calls[-1]:
            return httpx.Response(400, json={"error": "response_format not supported"})
        return _reply({"facts": []})
    respx.post(LLM_URL).mock(side_effect=handler)
    note = Note(title="t", slug="s", path="s.md", body="b", observed_at="2026-01-01")
    assert fx.model_facts(note, _cfg(clone)) == []
    assert ["response_format" in c for c in calls] == [True, False]


def test_dry_run_writes_nothing(clone, capsys):
    cfg = _cfg(clone, llm=False)
    report = fx.run(cfg, dry_run=True)
    assert report["dry_run"] and report["facts"] == 2 and report["supersede"]
    db = open_db(cfg.db)
    assert db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0] == 0
    db.close()


def test_cli_dry_run_prints_supersede_candidates(clone, monkeypatch, capsys):
    monkeypatch.setenv("MEMD_CLONE", str(clone))
    monkeypatch.setenv("MEMD_DB", str(clone.parent / "memd.db"))
    assert fx.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "supersede candidates" in out and "proxy-on-gpuhost" in out
    assert "nginx proxy | runs on | vmhost (2026-08-19 .. now; proxy-moved)" in out
    assert "dry-run: nothing written" in out
    assert fx.main([]) == 0
    capsys.readouterr()
    assert fx.main(["--subject", "nginx proxy", "--at", "2026-05-01"]) == 0
    assert "gpuhost (2026-03-01 .. 2026-08-19; proxy-on-gpuhost)" in capsys.readouterr().out


# --------------------------------------------------------------------------- closing


def _db_with(tmp_path, rows):
    db = open_db(tmp_path / "c.db")
    for i, (slug, obj, start, stated) in enumerate(rows):
        note = Note(title=slug, slug=slug, path=f"{slug}.md", body="", git_blob=f"b{i}")
        fx._insert(db, note, [fx.Fact("svc", "runs on", obj, start, stated)])
    fx.close_facts(db)
    return db


def test_closing_rules(tmp_path):
    db = _db_with(tmp_path, [
        ("a", "gpuhost", "2026-01-01", None),
        ("b", "gpuhost", "2026-02-01", None),     # same object: confirms, does not close a
        ("c", "vmhost", "2026-03-01", "2026-06-01"),
        ("d", "lapbox", "2026-05-01", None),
        ("e", "apphost", None, None),              # undated: neither closes nor is closed
        ("f", "vmhost", "2026-05-01", None),       # same day, other object: both stay open
    ])
    got = dict((s, (vt, cb)) for s, vt, cb in db.execute(
        "SELECT f.slug, f.valid_to, c.slug FROM facts f LEFT JOIN facts c ON c.id=f.closed_by"))
    assert got == {
        "a": ("2026-03-01", "c"), "b": ("2026-03-01", "c"),
        "c": ("2026-05-01", "d"),                  # the newer fact is earlier than "until"
        "d": (None, None), "e": (None, None), "f": (None, None),
    }
    # Idempotent: a second pass changes nothing.
    before = db.execute("SELECT id, valid_to, closed_by FROM facts ORDER BY id").fetchall()
    fx.close_facts(db)
    assert db.execute("SELECT id, valid_to, closed_by FROM facts ORDER BY id").fetchall() == before
    db.close()


def test_stated_until_closes_without_a_newer_fact(tmp_path):
    db = _db_with(tmp_path, [("a", "gpuhost", "2026-01-01", "2026-02-01")])
    assert db.execute("SELECT valid_to, closed_by FROM facts").fetchone() == ("2026-02-01", None)
    # Closed only by its own "until", so it is not a supersede candidate.
    assert fx.supersede_candidates(db) == []
    db.close()


def test_facts_version_bump_rebuilds_only_facts(tmp_path):
    db = _db_with(tmp_path, [("a", "gpuhost", "2026-01-01", None)])
    with db:
        _set_meta(db, "facts_version", "0")
    db.close()
    db = open_db(tmp_path / "c.db")
    assert db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    assert db.execute("SELECT value FROM meta WHERE key='facts_version'").fetchone()[0] == _FACTS_VERSION
    db.close()


# --------------------------------------------------------------------------- query


@pytest.fixture
def indexed(clone, monkeypatch):
    """A clone with its lexical index built and facts extracted (patterns only)."""
    from memd.refresh import ensure_lexical
    db = clone.parent / "memd.db"
    for key, value in {"MEMD_CLONE": str(clone), "MEMD_DB": str(db), "MEMD_PROFILE": "amber",
                       "MEMD_AMBER_CLONE": str(clone), "MEMD_AMBER_DB": str(db),
                       "MEMD_TOKEN": TOKEN}.items():
        monkeypatch.setenv(key, value)
    cfg = Config.from_env(env_file=None)
    ensure_lexical(cfg)
    fx.run(cfg)
    return cfg


def test_timeline_current_and_at_date(indexed):
    now = fx.timeline("NGINX proxy", profile="amber", cfg=indexed)
    assert now["match"] == "exact"
    assert [(f["object"], f["valid_from"], f["valid_to"], f["source"], f["current"])
            for f in now["facts"]] == [("vmhost", "2026-08-19", None, "proxy-moved", True)]
    then = fx.timeline("nginx proxy", "hosted on", "2026-05-01", profile="amber", cfg=indexed)
    assert [(f["object"], f["valid_to"], f["closed_by"]) for f in then["facts"]] == \
        [("gpuhost", "2026-08-19", "proxy-moved")]
    # The boundary day belongs to the newer fact; before any fact there is none.
    assert [f["object"] for f in fx.timeline("nginx proxy", at="2026-08-19", cfg=indexed)["facts"]] == ["vmhost"]
    assert fx.timeline("nginx proxy", at="2025-01-01", cfg=indexed)["facts"] == []
    # A subject named more briefly than it was written still resolves, flagged partial.
    assert fx.timeline("nginx", cfg=indexed)["match"] == "partial"
    assert fx.timeline("printer", cfg=indexed)["match"] == "none"
    with pytest.raises(ValueError):
        fx.timeline("nginx", at="last tuesday", cfg=indexed)


def test_timeline_route_auth_and_shape(indexed, monkeypatch):
    monkeypatch.setenv("MEMD_REQUIRE_RECALL_TOKEN", "1")
    client = TestClient(server_mod.app)
    assert client.post("/timeline", json={"subject": "nginx proxy"}).status_code == 401
    assert client.post("/timeline", json={"subject": "nginx proxy"},
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    auth = {"Authorization": f"Bearer {TOKEN}"}
    r = client.post("/timeline", json={"subject": "nginx proxy"}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["profile"] == "amber" and body["match"] == "exact"
    assert set(body["facts"][0]) >= {"subject", "predicate", "object", "valid_from",
                                     "valid_to", "source", "revision"}
    assert client.post("/timeline", json={}, headers=auth).status_code == 400
    assert client.post("/timeline", json={"subject": "x", "at": "soon"}, headers=auth).status_code == 400


def test_timeline_mcp_tool(indexed):
    tools = asyncio.run(mcp_mod.list_tools())
    tool = next(t for t in tools if t.name == "timeline")
    assert tool.input_schema["required"] == []
    assert tool.annotations.read_only_hint is True
    assert "profile" in tool.input_schema["properties"]
    out = asyncio.run(mcp_mod.call_tool("timeline", {"subject": "nginx proxy", "at": "2026-04-01"}))
    assert not out.is_error
    assert out.structured_content["facts"][0]["object"] == "gpuhost"
    assert "proxy-on-gpuhost" in out.content[0].text
    missing = asyncio.run(mcp_mod.call_tool("timeline", {}))
    assert missing.is_error and "subject is required" in missing.content[0].text


def test_timeline_mcp_over_http_requires_token(indexed):
    headers = {"Accept": "application/json, text/event-stream"}
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "timeline", "arguments": {"subject": "nginx proxy"}}}
    with TestClient(server_mod.create_token_app()) as client:
        refused = client.post("/mcp/", headers=headers, json=call)
        assert refused.status_code == 401
        ok = client.post("/mcp/", headers={**headers, "Authorization": f"Bearer {TOKEN}"}, json=call)
        result = ok.json()["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["facts"][0]["object"] == "vmhost"


# --------------------------------------------------------------------------- recall block


def test_current_facts_block_gated_on_intent_and_budget(indexed):
    shaped = {"text": "## Recalled memory (memd)\n\nsome notes"}
    fx.append_current_facts(shaped, query="where does the nginx proxy run", db_path=indexed.db,
                            max_chars=14000)
    assert "current_facts" not in shaped and "Current facts" not in shaped["text"]

    fx.append_current_facts(shaped, query="where does the nginx proxy run now?", db_path=indexed.db,
                            max_chars=14000)
    assert shaped["current_facts"] == 1
    assert shaped["text"].endswith("### Current facts (timeline)\n"
                                   "- nginx proxy | runs on | vmhost (since 2026-08-19; proxy-moved)")

    tight = {"text": "x" * 250}
    fx.append_current_facts(tight, query="current nginx proxy host", db_path=indexed.db, max_chars=256)
    assert tight == {"text": "x" * 250}

    for db_path in (indexed.db.parent / "absent.db", "/nonexistent/dir/x.db"):
        untouched = {"text": "t"}
        fx.append_current_facts(untouched, query="current nginx proxy", db_path=db_path, max_chars=14000)
        assert untouched == {"text": "t"}


def test_current_facts_block_ignores_index_without_fact_tables(tmp_path):
    path = tmp_path / "old.db"
    sqlite3.connect(path).execute("CREATE TABLE notes(slug TEXT)").connection.commit()
    assert fx.current_facts_block(path, "current nginx") == ("", 0)
    assert not path.with_suffix(".db-wal").exists()


def test_recall_route_appends_block_without_changing_notes(indexed, monkeypatch):
    note = Note(title="Proxy migration", slug="proxy-moved", path="proxy-moved.md",
                body="On 2026-08-19, nginx proxy moved to vmhost.", git_blob="abc")
    monkeypatch.setattr(server_mod, "_core_recall", lambda query, profile="amber", k=8, **kw: [note])
    client = TestClient(server_mod.app)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    plain = client.post("/recall", json={"query": "nginx proxy host"}, headers=auth).json()
    current = client.post("/recall", json={"query": "what is the current nginx proxy host"},
                          headers=auth).json()
    assert "Current facts" not in plain["context"]
    assert "current_facts" not in plain["rendering"]
    assert "- nginx proxy | runs on | vmhost (since 2026-08-19; proxy-moved)" in current["context"]
    assert current["rendering"]["current_facts"] == 1
    assert [n["slug"] for n in current["notes"]] == [n["slug"] for n in plain["notes"]] == ["proxy-moved"]
    assert current["rendering"]["excerpts"] == plain["rendering"]["excerpts"]


def test_mcp_recall_appends_block_on_present_state_query(indexed, monkeypatch):
    monkeypatch.setattr(mcp_mod, "_core_recall", lambda *a, **kw: [])
    out = asyncio.run(mcp_mod.call_tool("recall", {"query": "which host runs the nginx proxy right now"}))
    assert not out.is_error
    assert out.structured_content["current_facts"] == 1
    assert "vmhost" in out.structured_content["text"]
    plain = asyncio.run(mcp_mod.call_tool("recall", {"query": "nginx proxy"}))
    assert plain.structured_content["text"] == "No matching memory notes."


# --------------------------------------------------------------------------- review regressions


def _facts_store(tmp_path, monkeypatch, notes):
    import subprocess
    import memd.save as save_mod
    from memd.store import Note, dump_note
    clone = tmp_path / "clone"
    clone.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(clone), *a], check=True, capture_output=True)
    run("init", "-q"); run("config", "user.email", "t@memd"); run("config", "user.name", "t")
    for slug, body in notes:
        (clone / f"{slug}.md").write_text(dump_note(Note(title=slug, slug=slug, path=f"{slug}.md", body=body)))
    run("add", "-A"); run("commit", "-qm", "seed")
    monkeypatch.setattr(save_mod, "_pull_rebase_push", lambda clone: None)
    monkeypatch.setattr(save_mod, "_vector_near_matches", lambda cfg, body: [])
    from memd.config import Config
    return Config(clone=clone, db=tmp_path / "index.db", profile="amber", conflict_check="off")


def test_retiring_a_note_reopens_the_facts_it_had_closed(tmp_path, monkeypatch):
    import memd.save as save_mod
    cfg = _facts_store(tmp_path, monkeypatch, [
        ("a", "The nginx proxy runs on hosta since 2026-01-01."),
        ("b", "The nginx proxy runs on hostb since 2026-06-01.")])
    fx.run(cfg, use_llm=False)
    before = fx.timeline("nginx proxy", cfg=cfg, profile="amber")["facts"]
    assert [(f["object"], f["current"]) for f in before] == [("hostb", True)]
    save_mod.save({"slug": "c", "title": "c", "body": "Proxy notes rewritten; see ops.", "supersedes": "b"},
                  "amber", cfg=cfg)
    after = fx.timeline("nginx proxy", cfg=cfg, profile="amber")
    assert after["match"] == "exact"
    assert [(f["object"], f["current"], f["closed_by"]) for f in after["facts"]] == [("hosta", True, None)]


def test_current_facts_block_skips_facts_the_note_no_longer_states(tmp_path, monkeypatch):
    import memd.save as save_mod
    cfg = _facts_store(tmp_path, monkeypatch, [("a", "The nginx proxy runs on hostb since 2026-06-01.")])
    fx.run(cfg, use_llm=False)
    query = "what is the current nginx proxy host?"
    assert "hostb" in fx.append_current_facts({"text": "notes"}, query=query, db_path=cfg.db,
                                                 max_chars=14000)["text"]
    save_mod.save({"slug": "a", "title": "a", "body": "The nginx proxy runs on hostc since 2026-09-01."},
                  "amber", cfg=cfg)
    text = fx.append_current_facts({"text": "notes"}, query=query, db_path=cfg.db, max_chars=14000)["text"]
    assert "hostb" not in text
