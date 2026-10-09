"""mem-summarize: clustering, prompt, reply parsing, review-branch proposals.

Hermetic: temp git clones, respx-mocked chat endpoint, no real network.
"""
import datetime as dt
import json
import random
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

from memd import summarize as sm
from memd.config import Config
from memd.store import Note, list_notes, parse_text

LLM_BASE = "http://llm.test"
LLM_URL = f"{LLM_BASE}/v1/chat/completions"
TODAY = dt.date(2026, 9, 28)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _md(title, body, *, tags=("nas", "backup"), observed=None, volatility=None,
        slug=None, superseded_by=None, extra=""):
    fm = [f"title: {title}"]
    if slug:
        fm.append(f"slug: {slug}")
    fm.append(f"tags: [{', '.join(tags)}]")
    if observed:
        fm.append(f"observed_at: '{observed}'")
    if volatility:
        fm.append(f"volatility: {volatility}")
    if superseded_by:
        fm.append(f"superseded_by: {superseded_by}")
    if extra:
        fm.append(extra)
    return "---\n" + "\n".join(fm) + "\n---\n" + body + "\n"


BACKUP_NOTES = {
    "nas_backup_status_2026_07_01.md": _md(
        "NAS backup status 2026-07-01",
        "Nightly NAS backup runs at 02:00 to the offsite bucket. Retention is 30 days.",
        observed="2026-07-01", volatility="state"),
    "nas_backup_status_2026_08_01.md": _md(
        "NAS backup status 2026-08-01",
        "Nightly NAS backup moved to 03:00 to avoid the scrub. Retention is 30 days.",
        observed="2026-08-01", volatility="state"),
    "nas_backup_status_2026_09_01.md": _md(
        "NAS backup status 2026-09-01",
        "NAS backup now runs at 04:00 and retention was raised to 90 days.",
        observed="2026-09-01", volatility="state"),
}

OTHER_NOTES = {
    # dated and durable only: not a changeable-state topic
    "printer_setup_2026_01_01.md": _md("Printer setup 2026-01-01", "The office printer uses IPP.",
                                       tags=("printer",), observed="2026-01-01", volatility="durable"),
    "printer_setup_2026_02_01.md": _md("Printer setup 2026-02-01", "The office printer uses IPP everywhere.",
                                       tags=("printer",), observed="2026-02-01", volatility="durable"),
    "printer_setup_2026_03_01.md": _md("Printer setup 2026-03-01", "The office printer uses IPP only.",
                                       tags=("printer",), observed="2026-03-01", volatility="durable"),
    # changeable but only two dated notes
    "vpn_endpoint_2026_05_01.md": _md("VPN endpoint 2026-05-01", "VPN endpoint is vpn.example.com.",
                                      tags=("vpn",), observed="2026-05-01", volatility="state"),
    "vpn_endpoint_2026_06_01.md": _md("VPN endpoint 2026-06-01", "VPN endpoint moved to vpn2.example.com.",
                                      tags=("vpn",), observed="2026-06-01", volatility="state"),
    "unrelated.md": _md("Kitchen tap washer size", "The kitchen tap takes a 12 mm washer.",
                        tags=("house",)),
}


@pytest.fixture
def clone(tmp_path):
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@memd")
    _git(repo, "config", "user.name", "memd-test")
    for name, text in {**BACKUP_NOTES, **OTHER_NOTES}.items():
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _cfg(clone, **kw):
    return Config(clone=clone, db=clone.parent / "memd.db",
                  llm_url=kw.pop("llm_url", LLM_BASE), llm_model="test-model", **kw)


def _reply(sources=None, claim="NAS backup runs at 04:00 with 90-day retention."):
    sources = sources or ["nas-backup-status-2026-09-01"]
    content = json.dumps({
        "description": "NAS backup runs nightly at 04:00, 90-day retention.",
        "current": [{"claim": claim, "sources": sources}],
        "history": [{"date": "2026-07-01", "event": "Ran at 02:00 with 30-day retention.",
                     "sources": ["nas-backup-status-2026-07-01"]}],
    })
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


def _status(clone):
    return _git(clone, "status", "--porcelain")


# --------------------------------------------------------------------------- clustering


def test_cluster_groups_dated_changeable_topic_only(clone):
    clusters = sm.cluster_notes(list_notes(clone))
    assert [c.key for c in clusters] == ["stem:nas-backup"]
    c = clusters[0]
    assert c.slugs == ["nas-backup-status-2026-09-01", "nas-backup-status-2026-08-01",
                       "nas-backup-status-2026-07-01"]   # newest first
    assert c.topic == "nas backup"
    assert c.tags == ["backup", "nas"]


def test_cluster_is_deterministic_and_capped(clone):
    notes = list_notes(clone)
    first = sm.cluster_notes(notes, max_notes=2)
    shuffled = list(notes)
    random.Random(7).shuffle(shuffled)
    again = sm.cluster_notes(shuffled, max_notes=2)
    assert [(c.key, c.slugs) for c in first] == [(c.key, c.slugs) for c in again]
    assert first[0].slugs == ["nas-backup-status-2026-09-01", "nas-backup-status-2026-08-01"]


def test_cluster_skips_superseded_and_summary_notes(clone):
    notes = list_notes(clone)
    old = next(n for n in notes if n.slug == "nas-backup-status-2026-07-01")
    old.superseded_by = "nas-backup-status-2026-08-01"
    # two dated notes left: below MIN_DATED
    assert sm.cluster_notes(notes) == []
    summary = Note(title="Current state: nas backup", slug="current-state-nas-backup", path="s.md",
                   body="x", tags=["nas", "backup"], observed_at="2026-09-01", volatility="state",
                   metadata={"kind": "summary"})
    assert sm.cluster_notes([*notes, summary]) == []


def test_generic_key_on_most_of_a_large_corpus_is_ignored():
    notes = [Note(title=f"Topic {i} thing {i}", slug=f"topic{i}-thing{i}", path=f"{i}.md",
                  body=f"body {i}", tags=["everything"], observed_at=f"2026-01-{i + 1:02d}",
                  volatility="state") for i in range(25)]
    # identical titles link, so on a small corpus the shared tag is one topic ...
    assert [c.key for c in sm.cluster_notes(notes[:10])] == ["tag:everything"]
    # ... but a tag on most of a larger corpus says nothing about topic
    assert sm.cluster_notes(notes) == []


def test_slug_stem_drops_dates_and_filler():
    assert sm.slug_stem("nas-backup-status-2026-09-01") == "nas-backup"
    assert sm.slug_stem("project_gateway_dns_current") == "gateway-dns"
    assert sm.slug_stem("status-2026-09-01") is None


# --------------------------------------------------------------------------- prompt


def test_build_messages_lists_sources_newest_first_with_dates(clone):
    cluster = sm.cluster_notes(list_notes(clone))[0]
    cluster.notes[0].body = "x" * 5000
    msgs = sm.build_messages(cluster, today=TODAY, note_chars=100)
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "JSON" in msgs[0]["content"] and "newest" in msgs[0]["content"]
    user = msgs[1]["content"]
    assert "Topic: nas backup" in user and "Today: 2026-09-28" in user
    order = [user.index(f"[{s}]") for s in cluster.slugs]
    assert order == sorted(order)
    assert "date: 2026-09-01; volatility: state" in user
    assert "[... truncated]" in user and "x" * 101 not in user


# --------------------------------------------------------------------------- parsing

ALLOWED = ["a-2026", "b-2026"]


def test_parse_valid_reply():
    s = sm.parse_summary(json.dumps({
        "description": "It is X.",
        "current": [{"claim": "It is X.", "sources": ["a-2026"]}],
        "history": [{"date": "2026-01-02", "event": "Was Y.", "sources": ["b-2026"]}],
    }), ALLOWED)
    assert s.current == [("It is X.", ["a-2026"])]
    assert s.history == [("2026-01-02", "Was Y.", ["b-2026"])]
    assert s.description == "It is X."


def test_parse_tolerates_fences_prose_and_bracketed_slugs():
    body = {"current": [{"claim": "It  is\nX.", "sources": ["[a-2026]", "`b-2026`"]}]}
    for text in (f"```json\n{json.dumps(body)}\n```", f"Sure! Here it is: {json.dumps(body)} Hope that helps."):
        s = sm.parse_summary(text, ALLOWED)
        assert s.current == [("It is X.", ["a-2026", "b-2026"])]
        assert s.description == "It is X."
        assert s.history == []


def test_parse_drops_uncited_and_invented_sources():
    s = sm.parse_summary(json.dumps({
        "current": [
            {"claim": "Invented.", "sources": ["nope"]},
            {"claim": "No cite."},
            "not a dict",
            {"claim": "Real.", "source": "a-2026"},
        ],
        "history": [{"date": "yesterday", "event": "Was Y.", "sources": ["b-2026"]},
                    {"event": "Ghost.", "sources": ["ghost"]}],
    }), ALLOWED)
    assert s.current == [("Real.", ["a-2026"])]
    assert s.history == [("", "Was Y.", ["b-2026"])]


@pytest.mark.parametrize("text", [
    "", "no json here", "{not json}", "[1, 2]",
    json.dumps({"current": []}),
    json.dumps({"current": [{"claim": "x", "sources": ["unknown"]}]}),
    json.dumps({"current": "a string"}),
])
def test_parse_rejects_malformed(text):
    with pytest.raises(sm.SummaryParseError):
        sm.parse_summary(text, ALLOWED)


# --------------------------------------------------------------------------- run: proposals


@respx.mock
def test_run_proposes_on_review_branch_and_never_touches_the_store(clone):
    route = respx.post(LLM_URL).mock(return_value=_reply())
    head = _git(clone, "rev-parse", "HEAD")
    before = sorted(n.slug for n in list_notes(clone))

    report = sm.run(_cfg(clone), today=TODAY)

    assert route.call_count == 1
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "test-model" and sent["messages"][0]["role"] == "system"
    assert report["proposed"] == 1 and report["branch"] == sm.DEFAULT_BRANCH
    # the store is untouched: same HEAD, same branch, clean tree, same notes
    assert _git(clone, "rev-parse", "HEAD") == head
    assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _status(clone) == ""
    assert sorted(n.slug for n in list_notes(clone)) == before
    assert not (clone / "current_state_nas_backup.md").exists()
    # the proposal is a normal note on the branch, parented on the store's HEAD
    assert _git(clone, "rev-parse", f"{sm.DEFAULT_BRANCH}^") == head
    text = _git(clone, "show", f"{sm.DEFAULT_BRANCH}:current_state_nas_backup.md")
    note = parse_text(text, path="current_state_nas_backup.md")
    assert note.slug == "current-state-nas-backup"
    assert note.title == "Current state: nas backup"
    assert note.metadata["kind"] == "summary"
    assert note.metadata["cluster"] == "stem:nas-backup"
    assert note.metadata["as_of"] == "2026-09-01"
    assert note.metadata["sources"][0] == "nas-backup-status-2026-09-01"
    assert set(note.metadata["source_revisions"]) == set(note.metadata["sources"])
    assert note.observed_at == "2026-09-01" and note.volatility == "state"
    assert "summary" in note.tags
    now = note.body.index("## Now")
    assert now < note.body.index("## History") < note.body.index("## Sources")
    assert "04:00 with 90-day retention. (`nas-backup-status-2026-09-01`)" in note.body


@respx.mock
def test_rerun_is_idempotent_and_reuses_the_pending_proposal(clone):
    route = respx.post(LLM_URL).mock(return_value=_reply())
    first = sm.run(_cfg(clone), today=TODAY)
    second = sm.run(_cfg(clone), today=TODAY)
    assert route.call_count == 1           # unchanged pending proposal: no second model call
    assert second["commit"] == first["commit"]
    assert second["clusters"][0]["reused"] is True
    tree = _git(clone, "ls-tree", "--name-only", sm.DEFAULT_BRANCH).split()
    assert [p for p in tree if p.startswith("current_state")] == ["current_state_nas_backup.md"]


@respx.mock
def test_after_approval_summary_is_current_then_stale_when_a_source_changes(clone):
    route = respx.post(LLM_URL).mock(return_value=_reply())
    sm.run(_cfg(clone), today=TODAY)
    _git(clone, "merge", "-q", "--ff-only", sm.DEFAULT_BRANCH)     # the human approves

    report = sm.run(_cfg(clone), today=TODAY)
    assert route.call_count == 1
    assert report["proposed"] == 0 and report["commit"] is None
    assert [(c["status"], c["summary"]) for c in report["clusters"]] == [
        ("current", "current-state-nas-backup")]

    # a source changes and a new note joins the topic
    p = clone / "nas_backup_status_2026_09_01.md"
    p.write_text(p.read_text().replace("90 days", "120 days"))
    (clone / "nas_backup_status_2026_09_20.md").write_text(_md(
        "NAS backup status 2026-09-20", "NAS backup retention is 120 days; still 04:00.",
        observed="2026-09-20", volatility="state"))
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "update")
    route.mock(return_value=_reply(sources=["nas-backup-status-2026-09-20"],
                                   claim="NAS backup runs at 04:00 with 120-day retention."))

    report = sm.run(_cfg(clone), today=TODAY)
    assert route.call_count == 2
    [c] = report["clusters"]
    assert c["status"] == "stale" and c["summary"] == "current-state-nas-backup"
    assert "source nas-backup-status-2026-09-01 changed" in c["reasons"]
    assert "new note nas-backup-status-2026-09-20" in c["reasons"]
    # updated in place at the approved note's path: no duplicate summary
    changed = _git(clone, "diff", "--name-status", "HEAD", sm.DEFAULT_BRANCH)
    assert changed == "M\tcurrent_state_nas_backup.md"
    text = _git(clone, "show", f"{sm.DEFAULT_BRANCH}:current_state_nas_backup.md")
    assert "120-day retention" in text
    assert parse_text(text, path="x.md").metadata["as_of"] == "2026-09-20"


@respx.mock
def test_summary_whose_cluster_dissolved_is_still_checked(clone):
    respx.post(LLM_URL).mock(return_value=_reply())
    sm.run(_cfg(clone), today=TODAY)
    _git(clone, "merge", "-q", "--ff-only", sm.DEFAULT_BRANCH)
    for name in ("nas_backup_status_2026_07_01.md", "nas_backup_status_2026_08_01.md"):
        (clone / name).unlink()
    _git(clone, "commit", "-qam", "drop")
    report = sm.run(_cfg(clone), today=TODAY, dry_run=True)
    [c] = report["clusters"]
    assert c["status"] == "orphaned" and c["summary"] == "current-state-nas-backup"
    assert "source nas-backup-status-2026-07-01 no longer exists" in c["reasons"]


@respx.mock
@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"choices": [{"message": {"content": "I cannot help with that."}}]}),
    httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
        {"current": [{"claim": "Made up.", "sources": ["not-a-source"]}]})}}]}),
    httpx.Response(200, json={"unexpected": True}),
    httpx.Response(500, text="boom"),
])
def test_bad_model_output_proposes_nothing(clone, response):
    respx.post(LLM_URL).mock(return_value=response)
    report = sm.run(_cfg(clone), today=TODAY)
    assert report["proposed"] == 0 and report["commit"] is None
    assert report["clusters"][0]["error"]
    assert _git(clone, "branch", "--list", sm.DEFAULT_BRANCH) == ""
    assert _status(clone) == ""


@respx.mock
def test_dry_run_writes_nothing(clone):
    respx.post(LLM_URL).mock(return_value=_reply())
    report = sm.run(_cfg(clone), today=TODAY, dry_run=True)
    assert report["proposed"] == 1 and report["commit"] is None
    assert "## Now" in report["_texts"]["current_state_nas_backup.md"]
    assert _git(clone, "branch", "--list", sm.DEFAULT_BRANCH) == ""


def test_max_clusters_defers_the_rest(clone):
    report = sm.run(_cfg(clone, llm_url=None), today=TODAY, dry_run=True, max_clusters=0)
    assert report["clusters"][0]["deferred"] is True
    assert report["clusters"][0]["error"] is None


# --------------------------------------------------------------------------- CLI


@respx.mock
def test_cli_dry_run_prints_the_proposal(clone, monkeypatch, capsys):
    monkeypatch.setenv("MEMD_CLONE", str(clone))
    monkeypatch.setenv("MEMD_DB", str(clone.parent / "memd.db"))
    monkeypatch.setenv("MEMD_LLM_URL", LLM_BASE)
    monkeypatch.setenv("MEMD_LLM_MODEL", "test-model")
    respx.post(LLM_URL).mock(return_value=_reply())
    assert sm.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "new      stem:nas-backup -> current-state-nas-backup" in out
    assert "===== current_state_nas_backup.md =====" in out and "kind: summary" in out
    assert "nothing written" in out
    assert _git(clone, "branch", "--list", sm.DEFAULT_BRANCH) == ""


def test_cli_refuses_to_write_without_a_model(clone, monkeypatch, capsys):
    monkeypatch.setenv("MEMD_CLONE", str(clone))
    monkeypatch.setenv("MEMD_DB", str(clone.parent / "memd.db"))
    assert sm.main([]) == 2
    assert "MEMD_LLM_URL" in capsys.readouterr().err
    assert sm.main(["--dry-run"]) == 0            # plan-only
    assert "stem:nas-backup" in capsys.readouterr().out


def test_console_script_declared():
    import tomllib
    data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["mem-summarize"] == "memd.summarize:main"


def test_recall_path_never_imports_the_chat_client():
    root = Path(sm.__file__).parent
    for name in ("recall.py", "read.py", "save.py", "server.py", "mcp.py", "index.py"):
        assert "memd.llm" not in (root / name).read_text(), name


@respx.mock
def test_push_sends_only_the_review_branch(clone, tmp_path):
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(clone, "remote", "add", "origin", str(bare))
    _git(clone, "push", "-q", "origin", "main")
    respx.post(LLM_URL).mock(return_value=_reply())
    report = sm.run(_cfg(clone), today=TODAY)
    sm.push_branch(clone, report["branch"])
    assert _git(bare, "rev-parse", sm.DEFAULT_BRANCH) == report["commit"]
    assert _git(bare, "rev-parse", "main") == _git(clone, "rev-parse", "HEAD")
