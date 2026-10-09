"""Save's advisory conflict warnings (memd.conflicts).

Hermetic: temp git clones and SQLite indexes, respx-mocked chat endpoint; the
socket guard (tests/netguard.py) blocks everything else.
"""
import asyncio
import dataclasses
import json
import subprocess
import time

import httpx
import pytest
import respx

import memd.save as save_mod
from memd import conflicts
from memd.config import Config
from memd.store import Note, dump_note, read_note

LLM_URL = "http://llm.test/v1/chat/completions"


def git(clone, *args):
    return subprocess.run(["git", "-C", str(clone), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def store(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    git(clone, "init", "-q")
    git(clone, "config", "user.email", "test@memd")
    git(clone, "config", "user.name", "test")
    git(clone, "commit", "--allow-empty", "-qm", "seed")
    monkeypatch.setattr(save_mod, "_pull_rebase_push", lambda clone: None)
    monkeypatch.setattr(save_mod, "_vector_near_matches", lambda cfg, body: [])
    return Config(clone=clone, db=tmp_path / "index.db", profile="amber")


def seed(cfg, filename, **fields):
    note = Note(path=filename, **fields)
    (cfg.clone / filename).write_text(dump_note(note))
    git(cfg.clone, "add", "-A")
    git(cfg.clone, "commit", "-qm", "add fixture")
    return read_note(cfg.clone, note.slug)


def seed_proxy(cfg):
    return seed(cfg, "proxy.md", title="Proxy placement", slug="proxy-placement",
                body="The nginx proxy runs on vmhost since 2026-05-01.")


def llm_cfg(cfg, **kw):
    return dataclasses.replace(cfg, llm_url="http://llm.test", llm_model="chat-model",
                               conflict_check="llm", **kw)


def _reply(content):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                                              "content": content}}]})


def _verdicts(*items):
    return json.dumps({"verdicts": [dict(zip(("slug", "verdict", "claim"), i)) for i in items]})


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_defaults_and_clamps():
    cfg = Config.from_env({}, env_file=None)
    assert cfg.conflict_check == "facts" and cfg.conflict_deadline_ms == 1500
    cfg = Config.from_env({"MEMD_CONFLICT_CHECK": " LLM ", "MEMD_CONFLICT_DEADLINE_MS": "99999"},
                          env_file=None)
    assert cfg.conflict_check == "llm" and cfg.conflict_deadline_ms == 10000
    cfg = Config.from_env({"MEMD_CONFLICT_CHECK": "maybe", "MEMD_CONFLICT_DEADLINE_MS": "x"},
                          env_file=None)
    assert cfg.conflict_check == "facts" and cfg.conflict_deadline_ms == 1500
    assert Config.from_env({"MEMD_CONFLICT_DEADLINE_MS": "5"}, env_file=None).conflict_deadline_ms == 100


# ---------------------------------------------------------------------------
# Fact-based conflicts
# ---------------------------------------------------------------------------


def test_newer_fact_updates_other_note_and_receipt_offers_supersede(store):
    old = seed_proxy(store)
    result = save_mod.save({"title": "Proxy moved", "host": "remote",
                            "body": "The nginx proxy moved to gpuhost on 2026-08-19."}, cfg=store)
    assert result.saved and result.action == "created"
    assert result.flagged_for_review
    [c] = result.conflicts
    assert c["slug"] == "proxy-placement" and c["title"] == "Proxy placement"
    assert c["kind"] == "updates" and c["method"] == "facts"
    assert "vmhost" in c["evidence"] and "since 2026-05-01" in c["evidence"]
    assert "gpuhost" in c["evidence"] and "since 2026-08-19" in c["evidence"]
    assert f"slug={result.slug}" in c["suggested_action"]
    assert "supersedes=proxy-placement" in c["suggested_action"]
    assert any(w.startswith("Updates proxy-placement:") for w in result.warnings)
    # Never automatic: the other note is untouched.
    assert read_note(store.clone, old.slug).superseded_by is None
    assert read_note(store.clone, old.slug).git_blob == old.git_blob


def test_undated_different_value_contradicts(store):
    seed_proxy(store)
    seed(store, "undated.md", title="Proxy since", slug="proxy-since",
         body="Something unrelated about the garden.")
    result = save_mod.save({"title": "Proxy host", "host": "remote",
                            "body": "Since 2026-05-01 the nginx proxy runs on lapbox."}, cfg=store)
    [c] = result.conflicts
    assert c["slug"] == "proxy-placement" and c["kind"] == "contradicts"
    assert any(w.startswith("Contradicts proxy-placement:") for w in result.warnings)


def test_same_value_older_fact_and_unrelated_subject_are_not_conflicts(store):
    seed_proxy(store)
    same = save_mod.save({"title": "Proxy confirmed", "host": "remote",
                          "body": "The nginx proxy runs on vmhost since 2026-06-01."}, cfg=store)
    assert same.conflicts == []
    history = save_mod.save({"title": "Proxy history", "host": "remote",
                             "body": "The nginx proxy moved to lapbox on 2026-01-10."}, cfg=store)
    assert history.conflicts == []     # the store already holds a newer value
    other = save_mod.save({"title": "DNS host", "host": "remote",
                           "body": "The dns resolver moved to gpuhost on 2026-08-19."}, cfg=store)
    assert other.conflicts == []


def test_closed_fact_of_other_note_is_not_current(store):
    seed(store, "a.md", title="Proxy old", slug="proxy-old",
         body="The nginx proxy runs on lapbox since 2026-01-01.")
    seed_proxy(store)      # a later value closes lapbox
    result = save_mod.save({"title": "Proxy again", "host": "remote",
                            "body": "The nginx proxy runs on vmhost since 2026-07-01."}, cfg=store)
    assert result.conflicts == []


def test_supersedes_target_and_self_are_excluded(store):
    old = seed_proxy(store)
    result = save_mod.save({"title": "Proxy moved", "host": "remote", "supersedes": old.slug,
                            "body": "The nginx proxy moved to gpuhost on 2026-08-19."}, cfg=store)
    assert result.action == "superseded" and result.conflicts == []
    # Updating a note never reports the note's own previous text.
    again = save_mod.save({"title": "Proxy moved", "slug": result.slug, "host": "remote",
                           "body": "The nginx proxy moved to lapbox on 2026-09-01."}, cfg=store)
    assert again.action == "updated" and again.conflicts == []


def test_superseded_notes_are_never_reported(store):
    seed(store, "gone.md", title="Proxy gone", slug="proxy-gone", superseded_by="proxy-placement",
         body="The nginx proxy runs on lapbox since 2026-06-01.")
    seed_proxy(store)
    result = save_mod.save({"title": "Proxy moved", "host": "remote",
                            "body": "The nginx proxy moved to gpuhost on 2026-08-19."}, cfg=store)
    assert [c["slug"] for c in result.conflicts] == ["proxy-placement"]


def test_suggested_supersede_call_retires_the_other_note(store):
    old = seed_proxy(store)
    body = "The nginx proxy moved to gpuhost on 2026-08-19."
    first = save_mod.save({"title": "Proxy moved", "host": "remote", "body": body}, cfg=store)
    assert first.conflicts
    again = save_mod.save({"title": "Proxy moved", "slug": first.slug, "supersedes": old.slug,
                           "host": "remote", "body": body}, cfg=store)
    assert again.saved and again.slug == first.slug and again.action == "superseded"
    assert again.conflicts == []
    assert read_note(store.clone, old.slug).superseded_by == first.slug
    live = [n.slug for n in save_mod.list_notes(store.clone) if not n.superseded_by]
    assert sorted(live) == [first.slug]


def test_index_fact_table_contributes_model_facts(store, tmp_path):
    from memd.facts import _insert, Fact
    from memd.index import open_db
    old = seed(store, "p.md", title="Proxy notes", slug="proxy-notes",
               body="Where the nginx proxy lives is described elsewhere.")
    db = open_db(store.db)
    _insert(db, old, [Fact("nginx proxy", "runs on", "vmhost", "2026-05-01", None, "llm")])
    db.commit()
    db.close()
    result = save_mod.save({"title": "Proxy moved", "host": "remote",
                            "body": "The nginx proxy moved to gpuhost on 2026-08-19."}, cfg=store)
    assert [(c["slug"], c["kind"]) for c in result.conflicts] == [("proxy-notes", "updates")]


def test_disabled_check_reports_nothing(store):
    seed_proxy(store)
    off = dataclasses.replace(store, conflict_check="off")
    result = save_mod.save({"title": "Proxy moved", "host": "remote",
                            "body": "The nginx proxy moved to gpuhost on 2026-08-19."}, cfg=off)
    assert result.saved and result.conflicts == []
    assert not any("Conflict" in w or "Updates" in w for w in result.warnings)


def test_fact_check_failure_never_fails_the_save(store, monkeypatch):
    seed_proxy(store)

    def boom(*a, **kw):
        raise RuntimeError("pattern engine broke")
    monkeypatch.setattr(conflicts, "fact_conflicts", boom)
    result = save_mod.save({"title": "Proxy moved", "host": "remote",
                            "body": "The nginx proxy moved to gpuhost on 2026-08-19."}, cfg=store)
    assert result.saved and result.revision and result.conflicts == []
    assert any(w.startswith("Conflict check skipped: RuntimeError") for w in result.warnings)


# ---------------------------------------------------------------------------
# Model verdicts
# ---------------------------------------------------------------------------


def test_parse_verdicts_validates_items():
    text = "```json\n" + _verdicts(
        ("a", "contradicts", "  A runs   on X "), ("b", "updates", ""), ("c", "consistent", "x"),
        ("unknown", "contradicts", "claim"), ("d", "bogus", "claim"), ("a", "consistent", ""),
    ) + "\n```"
    out = conflicts.parse_verdicts(text, {"a", "b", "c", "d"})
    assert out == {"a": ("contradicts", "A runs on X"), "c": ("consistent", "")}
    assert conflicts.parse_verdicts('Sure: {"verdicts": []} done', {"a"}) == {}
    for bad in ("not json", "[1, 2]", '{"verdicts": "no"}', "{broken"):
        with pytest.raises(conflicts.VerdictParseError):
            conflicts.parse_verdicts(bad, {"a"})


def _related(store, monkeypatch, slug="proxy-notes"):
    monkeypatch.setattr(save_mod, "_vector_near_matches", lambda cfg, body: [(slug, 0.94)])
    return seed(store, "p.md", title="Proxy notes", slug=slug,
                body="Our reverse proxy is nginx on vmhost.")


@respx.mock
def test_model_verdict_becomes_conflict(store, monkeypatch):
    _related(store, monkeypatch)
    route = respx.post(LLM_URL).mock(return_value=_reply(_verdicts(
        ("proxy-notes", "contradicts", "Our reverse proxy is nginx on vmhost."))))
    result = save_mod.save({"title": "Proxy now caddy", "host": "remote",
                            "body": "The reverse proxy is caddy on gpuhost."}, cfg=llm_cfg(store))
    assert result.saved and route.called
    [c] = result.conflicts
    assert c["slug"] == "proxy-notes" and c["kind"] == "contradicts" and c["method"] == "llm"
    assert "Our reverse proxy is nginx on vmhost." in c["evidence"]
    assert "supersedes=proxy-notes" in c["suggested_action"]
    assert read_note(store.clone, "proxy-notes").superseded_by is None
    body = json.loads(route.calls[0].request.content)
    assert body["response_format"]["type"] == "json_schema"
    prompt = body["messages"][1]["content"]
    assert "[proxy-notes]" in prompt and "caddy on gpuhost" in prompt


@respx.mock
def test_model_consistent_verdict_and_schema_fallback(store, monkeypatch):
    _related(store, monkeypatch)
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if "response_format" in calls[-1]:
            return httpx.Response(400, text="response_format unsupported")
        return _reply(_verdicts(("proxy-notes", "consistent", "")))
    respx.post(LLM_URL).mock(side_effect=handler)
    result = save_mod.save({"title": "Proxy detail", "host": "remote",
                            "body": "The nginx proxy has gzip enabled."}, cfg=llm_cfg(store))
    assert len(calls) == 2 and result.conflicts == []
    assert not any("skipped" in w for w in result.warnings)


@respx.mock
def test_malformed_model_reply_is_skipped_not_failed(store, monkeypatch):
    _related(store, monkeypatch)
    respx.post(LLM_URL).mock(return_value=_reply("I think they conflict, probably."))
    result = save_mod.save({"title": "Proxy now caddy", "host": "remote",
                            "body": "The reverse proxy is caddy on gpuhost."}, cfg=llm_cfg(store))
    assert result.saved and result.revision and result.conflicts == []
    assert any(w.startswith("Conflict check skipped: model reply unusable") for w in result.warnings)


@respx.mock
def test_model_error_is_skipped_not_failed(store, monkeypatch):
    _related(store, monkeypatch)
    respx.post(LLM_URL).mock(return_value=httpx.Response(503, text="overloaded"))
    result = save_mod.save({"title": "Proxy now caddy", "host": "remote",
                            "body": "The reverse proxy is caddy on gpuhost."}, cfg=llm_cfg(store))
    assert result.saved and result.conflicts == []
    assert any(w.startswith("Conflict check skipped: model unavailable") for w in result.warnings)


def test_model_deadline_bounds_the_save(store, monkeypatch):
    _related(store, monkeypatch)

    def slow(messages, cfg):
        time.sleep(2.0)
        return _verdicts(("proxy-notes", "contradicts", "claim"))
    monkeypatch.setattr(conflicts, "_ask", slow)
    cfg = llm_cfg(store, conflict_deadline_ms=200)
    started = time.monotonic()
    result = save_mod.save({"title": "Proxy now caddy", "host": "remote",
                            "body": "The reverse proxy is caddy on gpuhost."}, cfg=cfg)
    assert time.monotonic() - started < 1.8
    assert result.saved and result.conflicts == []
    assert any("model did not answer within 200 ms" in w for w in result.warnings)


@respx.mock
def test_model_check_disabled_without_llm_url_or_mode(store, monkeypatch):
    _related(store, monkeypatch)
    route = respx.post(LLM_URL).mock(return_value=_reply(_verdicts(
        ("proxy-notes", "contradicts", "claim"))))
    body = {"title": "Proxy now caddy", "host": "remote",
            "body": "The reverse proxy is caddy on gpuhost."}
    no_url = dataclasses.replace(store, conflict_check="llm")
    assert save_mod.save(dict(body), cfg=no_url).conflicts == []
    facts_only = dataclasses.replace(llm_cfg(store), conflict_check="facts")
    assert save_mod.save(dict(body, title="Second"), cfg=facts_only).conflicts == []
    assert not route.called


@respx.mock
def test_model_never_sees_the_superseded_note(store, monkeypatch):
    old = _related(store, monkeypatch)
    route = respx.post(LLM_URL).mock(return_value=_reply(_verdicts()))
    result = save_mod.save({"title": "Proxy now caddy", "host": "remote", "supersedes": old.slug,
                            "body": "The reverse proxy is caddy on gpuhost."}, cfg=llm_cfg(store))
    assert result.action == "superseded" and result.conflicts == []
    assert not route.called     # the only related note is the one being replaced


# ---------------------------------------------------------------------------
# Receipt and MCP surface
# ---------------------------------------------------------------------------


def test_receipt_shape_keeps_existing_fields(store):
    seed_proxy(store)
    receipt = save_mod.save({"title": "Proxy moved", "host": "remote",
                             "body": "The nginx proxy moved to gpuhost on 2026-08-19."},
                            cfg=store).to_dict()
    for key in ("slug", "action", "saved", "synced", "indexed", "lexical_indexed", "revision",
                "related", "warnings", "flagged_for_review", "grounding", "path"):
        assert key in receipt
    [c] = receipt["conflicts"]
    assert set(c) == {"slug", "title", "kind", "evidence", "suggested_action", "method"}
    json.dumps(receipt)


def test_mcp_save_schema_and_result_carry_conflicts(store, monkeypatch):
    from memd import mcp
    tools = asyncio.run(mcp.list_tools())
    save_tool = next(t for t in tools if t.name == "save")
    schema = save_tool.output_schema["properties"]["conflicts"]
    assert schema["type"] == "array"
    item = schema["items"]["properties"]
    assert {"slug", "title", "kind", "evidence", "suggested_action", "method"} <= set(item)
    assert item["kind"]["enum"] == ["contradicts", "updates"]
    assert "supersedes" in save_tool.description and "conflicts" in save_tool.description

    seed_proxy(store)
    monkeypatch.setattr(mcp, "_cfg_for", lambda profile: store)
    monkeypatch.setattr(mcp, "_core_save",
                        lambda fact, profile, cfg: save_mod.save(fact, profile=profile, cfg=cfg))
    out = asyncio.run(mcp.call_tool("save", {"title": "Proxy moved", "host": "remote",
                                             "body": "The nginx proxy moved to gpuhost on 2026-08-19."}))
    assert not out.is_error, out.content[0].text
    assert out.structured_content["conflicts"][0]["slug"] == "proxy-placement"
    assert json.loads(out.content[0].text)["conflicts"][0]["kind"] == "updates"
