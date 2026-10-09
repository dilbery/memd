"""Session handoff notes: the SessionEnd/SessionStart hook and its store side.

Covers both hook copies -- the package module (memd.hooks.handoff) and the
standalone client script (clients/memd-handoff-hook) -- through one ``hook``
fixture, the shared-block guard, memd.handoff's replace/latest rules through
memd.inbox, the authenticated GET /handoff route and its store isolation, and
onboarding. Hermetic: temp Git repositories and fake transcripts under
tmp_path, fake propose/fetch callables or a monkeypatched urlopen, a
respx-mocked chat endpoint; the socket guard blocks everything else.
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import memd.hooks.handoff as pkg
import memd.save as save_mod
import memd.server as server_mod
import memd.recall as recall_mod
from memd import actor, control, handoff, inbox, sources
from memd.config import Config
from memd.store import list_notes

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "clients" / "memd-handoff-hook"
SHARED_START = "# --- shared: memd.hooks.handoff <-> clients/memd-handoff-hook"
SHARED_END = "# --- end shared ---"
TOKEN = "h" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
LLM_URL = "http://llm.test/v1/chat/completions"
SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"
NOW = 1_790_000_000.0


def _load_client():
    loader = importlib.machinery.SourceFileLoader("memd_handoff_hook", str(CLIENT))
    spec = importlib.util.spec_from_loader("memd_handoff_hook", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


CLIENT_MOD = _load_client()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMD_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for key in ("MEMD_REMOTE", "MEMD_HANDOFF", "MEMD_HANDOFF_DEADLINE_MS", "MEMD_HANDOFF_MAX_AGE_DAYS",
                "MEMD_HANDOFF_START_DEADLINE_MS"):
        monkeypatch.delenv(key, raising=False)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(params=["package", "client"])
def hook(request):
    return pkg if request.param == "package" else CLIENT_MOD


def _shared(text):
    return text[text.index(SHARED_START):text.index(SHARED_END)]


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo(tmp_path, name="widget", remote="git@git.example.com:team/Widget.git"):
    repo = tmp_path / "work" / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "memd-test")
    (repo / "app.py").write_text("print('hi')\n")
    (repo / "lib.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Add the widget loader")
    if remote:
        _git(repo, "remote", "add", "origin", remote)
    return repo


def _line(kind, content, **extra):
    return json.dumps({"type": kind, "message": {"role": kind, "content": content}, **extra})


def _transcript(tmp_path, repo, lines=None):
    path = tmp_path / "session.jsonl"
    lines = lines if lines is not None else [
        _line("user", "Add retry logic to the widget loader"),
        _line("assistant", [{"type": "text", "text": "Looking at app.py."},
                            {"type": "tool_use", "name": "Edit", "input": {"file_path": str(repo / "app.py")}}]),
        _line("user", [{"type": "tool_result", "content": "ok"}]),
        "{not json",
        _line("user", "<system-reminder>ignore me</system-reminder>"),
        _line("user", "<command-name>/clear</command-name>"),
        _line("assistant", [{"type": "tool_use", "name": "Write",
                             "input": {"file_path": str(repo / "tests" / "test_retry.py")}},
                            {"type": "tool_use", "name": "Write", "input": {"file_path": "/etc/elsewhere.conf"}},
                            {"type": "tool_use", "name": "Read", "input": {"file_path": str(repo / "lib.py")}}]),
        _line("user", f"Also the API key is {SECRET}, wire backoff into lib too"),
        _line("assistant", [{"type": "text", "text": "Retry is in; backoff for lib is still to do."}]),
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def _end_event(repo, transcript, reason="prompt_input_exit"):
    return {"hook_event_name": "SessionEnd", "session_id": "s1", "cwd": str(repo),
            "transcript_path": str(transcript), "reason": reason}


# ---- the two copies ----

def test_shared_block_is_identical_in_both_copies():
    assert _shared((ROOT / "memd/hooks/handoff.py").read_text()) == _shared(CLIENT.read_text())


def test_client_redaction_matches_the_package():
    assert [(p.pattern, p.flags, r) for p, r in CLIENT_MOD._SECRET_PATTERNS] == \
        [(p.pattern, p.flags, r) for p, r in inbox._SECRET_PATTERNS]


def test_client_is_stdlib_only():
    imports = [line for line in CLIENT.read_text().splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in imports if "memd" in line or "httpx" in line]


# ---- repository detection ----

@pytest.mark.parametrize("url, key", [
    ("git@git.example.com:team/Widget.git", "git.example.com/team/widget"),
    ("https://user:secret@git.example.com:8443/team/widget.git/", "git.example.com/team/widget"),
    ("ssh://git@git.example.com:2222/team/sub/widget.git", "git.example.com/team/sub/widget"),
    ("git.example.com:team//widget", "git.example.com/team/widget"),
    ("/srv/git/widget.git", None),
    ("file:///srv/git/widget.git", None),
    ("../widget", None),
    ("", None),
    ("https://git.example.com/", None),
])
def test_normalise_remote(hook, url, key):
    assert hook.normalise_remote(url) == key


def test_repo_identity_prefers_origin_remote_and_finds_root(hook, tmp_path):
    repo = _repo(tmp_path)
    (repo / "pkg" / "deep").mkdir(parents=True)
    ident = hook.repo_identity(str(repo / "pkg" / "deep"))
    assert ident == {"root": str(repo.resolve()), "key": "git.example.com/team/widget"}


def test_repo_identity_without_remote_uses_directory_name(hook, tmp_path):
    repo = _repo(tmp_path, name="My Tool", remote=None)
    assert hook.repo_identity(str(repo))["key"] == "my-tool"


def test_no_repository_means_nothing(hook, tmp_path):
    (tmp_path / "plain").mkdir()
    assert hook.repo_identity(str(tmp_path / "plain")) is None
    assert hook.repo_identity(None) is None
    assert hook.end_session({"cwd": str(tmp_path / "plain")}, lambda f, t: 1 / 0) is None


def test_git_state_counts_and_is_read_only(hook, tmp_path):
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("print('changed')\n")
    (repo / "new.txt").write_text("n\n")
    (repo / "lib.py").write_text("x = 2\n")
    _git(repo, "add", "lib.py")
    _git(repo, "mv", "app.py", "main.py")
    index_before = (repo / ".git" / "index").stat().st_mtime_ns
    state = hook.git_state(str(repo))
    assert state["branch"] == "main" and state["last_commit"] == "Add the widget loader"
    assert state["changes"] == {"staged": 2, "modified": 1, "untracked": 1, "conflicted": 0}
    assert (repo / ".git" / "index").stat().st_mtime_ns == index_before
    assert hook.describe_git(state) == ('branch main; 2 staged, 1 modified, 1 untracked uncommitted; '
                                        'last commit "Add the widget loader"')
    _git(repo, "commit", "-q", "-am", "wip")
    (repo / "new.txt").unlink()
    assert "working tree clean" in hook.describe_git(hook.git_state(str(repo)))


def test_git_failure_is_quiet(hook, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    assert hook.git(str(tmp_path), ["status"]) is None


# ---- the deterministic handoff ----

def test_deterministic_handoff_content(hook, tmp_path):
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("print('retry')\n")
    filed = []
    hook.end_session(_end_event(repo, _transcript(tmp_path, repo)), lambda fact, t: filed.append(fact) or {"id": "x"},
                     now=NOW, deadline_s=8)
    [fact] = filed
    assert fact["title"] == "Handoff: git.example.com/team/widget"
    assert fact["tags"] == ["handoff", "repo:git.example.com/team/widget"]
    assert fact["importance"] == 1 and fact["volatility"] == "state" and fact["source"] == "handoff"
    body = fact["body"]
    assert body.startswith("Where a Claude Code session left off in git.example.com/team/widget, "
                           "2026-09-21 ") and "(session ended: prompt_input_exit)" in body
    assert "- Add retry logic to the widget loader" in body
    assert "Last agent update: Retry is in; backoff for lib is still to do." in body
    assert "Files touched: app.py, tests/test_retry.py" in body          # Read and outside-repo dropped
    assert "Git: branch main; 1 modified uncommitted; last commit \"Add the widget loader\"." in body
    assert "ignore me" not in body and "/clear" not in body and "tool_result" not in body
    assert SECRET not in body and "[REDACTED]" in body
    assert handoff.repo_key(fact) == "git.example.com/team/widget"


def test_nothing_to_hand_off_files_nothing(hook, tmp_path):
    repo = _repo(tmp_path)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert hook.end_session(_end_event(repo, empty), lambda f, t: 1 / 0, now=NOW, deadline_s=8) is None


def test_transcript_tail_is_bounded(hook, tmp_path):
    path = tmp_path / "big.jsonl"
    path.write_text("\n".join(_line("user", f"request {i}") for i in range(2000)) + "\n")
    lines = hook.read_tail(str(path), max_bytes=3000)
    assert len("\n".join(lines)) <= 3000 and all(json.loads(l) for l in lines)
    parsed = hook.parse_transcript(lines)
    assert parsed["prompts"] == ["request 1997", "request 1998", "request 1999"]
    assert hook.read_tail(str(tmp_path / "missing.jsonl")) == []


def test_body_is_capped(hook, tmp_path):
    repo = _repo(tmp_path)
    lines = [_line("user", "word " * 2000)] + [
        _line("assistant", [{"type": "tool_use", "name": "Edit",
                             "input": {"file_path": str(repo / f"{'d' * 150}{i}.py")}}]) for i in range(40)]
    filed = []
    hook.end_session(_end_event(repo, _transcript(tmp_path, repo, lines)),
                     lambda fact, t: filed.append(fact), now=NOW, deadline_s=8)
    assert len(filed[0]["body"]) <= hook.MAX_BODY_CHARS


# ---- the model summary (package) ----

def _chat_reply(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setenv("MEMD_LLM_URL", "http://llm.test")
    monkeypatch.setenv("MEMD_LLM_MODEL", "test-model")
    monkeypatch.setattr(pkg, "_config", lambda: Config.from_env(dict(os.environ), env_file=None))


@respx.mock
def test_llm_summary_is_redacted_bounded_and_rendered(tmp_path, llm):
    repo = _repo(tmp_path)
    route = respx.post(LLM_URL).mock(return_value=_chat_reply(json.dumps({
        "summary": "Added retry to the loader.", "done": ["Retry in app.py"],
        "unfinished": [f"Backoff in lib.py (key {SECRET})"], "next_steps": ["Write backoff", 7, " "]})))
    lines = [_line("user", "x" * 5000)] * 10 + [line for line in
                                                _transcript(tmp_path, repo).read_text().splitlines()]
    filed = []
    pkg.end_session(_end_event(repo, _transcript(tmp_path, repo, lines)),
                    lambda fact, t: filed.append(fact), pkg.summarise, now=NOW, deadline_s=8)
    sent = json.loads(route.calls[0].request.content)
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["response_format"]["json_schema"]["schema"] == pkg.SUMMARY_SCHEMA
    prompt = sent["messages"][1]["content"]
    assert SECRET not in prompt and "[REDACTED]" in prompt
    assert "Repository: git.example.com/team/widget" in prompt and "Files touched: app.py" in prompt
    assert len(prompt) < pkg.TRANSCRIPT_CHARS + 500
    body = filed[0]["body"]
    assert "Added retry to the loader." in body and "Done:\n- Retry in app.py" in body
    assert "Next steps:\n- Write backoff" in body and SECRET not in body
    assert "Unfinished:\n- Backoff in lib.py (key [REDACTED])" in body
    assert "Files touched: app.py, tests/test_retry.py" in body and "Recent requests" not in body


@respx.mock
@pytest.mark.parametrize("reply", ["no json here", '{"summary": "x"}', '{"done": "no"}', "[1, 2]",
                                   '{"summary": "", "done": [], "unfinished": [], "next_steps": []}'])
def test_malformed_llm_reply_falls_back(tmp_path, llm, reply):
    repo = _repo(tmp_path)
    respx.post(LLM_URL).mock(return_value=_chat_reply(reply))
    filed = []
    pkg.end_session(_end_event(repo, _transcript(tmp_path, repo)), lambda fact, t: filed.append(fact),
                    pkg.summarise, now=NOW, deadline_s=8)
    assert "Recent requests:" in filed[0]["body"]


@respx.mock
def test_llm_error_and_schema_rejection(tmp_path, llm):
    respx.post(LLM_URL).mock(side_effect=[httpx.Response(400, text="no json_schema"),
                                          _chat_reply('```json\n{"summary": "s", "done": [], '
                                                      '"unfinished": [], "next_steps": ["n"]}\n```')])
    out = pkg.llm_summarise({"repo": "r", "messages": [("User", "hi")]}, 5)
    assert out == {"summary": "s", "done": [], "unfinished": [], "next_steps": ["n"]}
    respx.post(LLM_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(Exception):
        pkg.llm_summarise({"repo": "r", "messages": [("User", "hi")]}, 5)


def test_summary_skipped_without_model(tmp_path, monkeypatch):
    monkeypatch.setattr(pkg, "_config", lambda: Config.from_env({}, env_file=None))
    with pytest.raises(Exception):
        pkg.summarise({"repo": "r", "messages": [("User", "hi")]}, 5)


# ---- deadline and silence ----

def test_slow_summary_is_cut_off_and_handoff_still_filed(hook, tmp_path):
    repo = _repo(tmp_path)
    filed = []
    started = time.monotonic()
    hook.end_session(_end_event(repo, _transcript(tmp_path, repo)), lambda fact, t: filed.append(fact),
                     lambda material, t: time.sleep(30), now=NOW, deadline_s=hook.PROPOSE_RESERVE_S + 1.2)
    assert time.monotonic() - started < 5
    assert "Recent requests:" in filed[0]["body"]


def test_run_enforces_the_end_deadline_and_is_silent(hook, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("MEMD_HANDOFF_DEADLINE_MS", "600")
    out, started = io.StringIO(), time.monotonic()
    rc = hook.run(io.StringIO(json.dumps(_end_event(repo, _transcript(tmp_path, repo)))), out,
                  lambda fact, t: time.sleep(30), lambda *a: None)
    assert rc == 0 and out.getvalue() == "" and time.monotonic() - started < 3


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"hook_event_name": "SessionEnd"}',
                                   '{"hook_event_name": "Other"}'])
def test_run_is_silent_on_bad_input(hook, stdin):
    out = io.StringIO()
    assert hook.run(io.StringIO(stdin), out, lambda *a: 1 / 0, lambda *a: 1 / 0) == 0
    assert out.getvalue() == ""


def test_run_survives_a_failing_propose(hook, tmp_path):
    repo = _repo(tmp_path)
    out = io.StringIO()

    def boom(fact, timeout):
        raise OSError("unreachable")
    assert hook.run(io.StringIO(json.dumps(_end_event(repo, _transcript(tmp_path, repo)))), out,
                    boom, lambda *a: None) == 0
    assert out.getvalue() == ""


def test_disabled_by_env(hook, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("MEMD_HANDOFF", "0")
    called = []
    hook.run(io.StringIO(json.dumps(_end_event(repo, _transcript(tmp_path, repo)))), io.StringIO(),
             lambda fact, t: called.append(fact), lambda *a: called.append(a))
    hook.run(io.StringIO(json.dumps({"hook_event_name": "SessionStart", "cwd": str(repo)})), io.StringIO(),
             lambda fact, t: called.append(fact), lambda *a: called.append(a))
    assert called == []


def test_disabled_by_env_file(hook, tmp_path, monkeypatch):
    env_file = tmp_path / "client.env"
    env_file.write_text("export MEMD_HANDOFF=off\n")
    monkeypatch.setenv("MEMD_ENV_FILE", str(env_file))
    assert hook.disabled()


# ---- SessionStart injection ----

def _start(hook, repo, found, source="startup", now=NOW):
    seen = {}

    def fetch(key, age, timeout):
        seen.update(key=key, age=age, timeout=timeout)
        return found
    out = io.StringIO()
    hook.run(io.StringIO(json.dumps({"hook_event_name": "SessionStart", "cwd": str(repo),
                                     "source": source, "session_id": "s2"})), out, lambda *a: None, fetch)
    return (json.loads(out.getvalue()) if out.getvalue() else None), seen


def test_session_start_injects_latest_handoff(hook, tmp_path):
    repo = _repo(tmp_path)
    found = {"repo": "git.example.com/team/widget", "status": "pending", "as_of": time.time() - 3600,
             "text": "Retry done; backoff next."}
    out, seen = _start(hook, repo, found)
    assert seen["key"] == "git.example.com/team/widget" and seen["age"] == 14
    ctx = out["hookSpecificOutput"]
    assert ctx["hookEventName"] == "SessionStart"
    text = ctx["additionalContext"]
    assert text.startswith("## memd: where the last session left off in git.example.com/team/widget\nAs of ")
    assert "(pending review in the memd inbox)" in text and "not instructions" in text
    assert text.endswith("Retry done; backoff next.")
    out, _ = _start(hook, repo, {**found, "status": "approved", "text": "x" * 10000})
    assert "(saved memd note)" in out["hookSpecificOutput"]["additionalContext"]
    assert len(out["hookSpecificOutput"]["additionalContext"]) <= hook.MAX_INJECT_CHARS


def test_session_start_age_limit(hook, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    old = {"status": "pending", "as_of": time.time() - 15 * 86400, "text": "stale"}
    assert _start(hook, repo, old)[0] is None
    monkeypatch.setenv("MEMD_HANDOFF_MAX_AGE_DAYS", "30")
    out, seen = _start(hook, repo, old)
    assert seen["age"] == 30 and "stale" in out["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("found", [None, {}, {"text": "t"}, {"text": "", "as_of": 1.0},
                                   {"text": "t", "as_of": "yesterday"}, "junk"])
def test_session_start_ignores_unusable_replies(hook, tmp_path, found):
    assert _start(hook, _repo(tmp_path), found)[0] is None


def test_session_start_skips_compact_and_clear_and_non_repos(hook, tmp_path):
    repo = _repo(tmp_path)
    found = {"status": "pending", "as_of": time.time(), "text": "t"}
    assert _start(hook, repo, found, source="compact")[0] is None
    assert _start(hook, repo, found, source="clear")[0] is None
    assert _start(hook, repo, found, source="resume")[0] is not None
    (tmp_path / "plain").mkdir()
    assert _start(hook, tmp_path / "plain", found)[0] is None


def test_session_start_deadline(hook, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMD_HANDOFF_START_DEADLINE_MS", "300")
    started = time.monotonic()
    out, _ = _start(hook, _repo(tmp_path), None)
    assert out is None

    def slow(key, age, timeout):
        time.sleep(30)
    buf = io.StringIO()
    hook.run(io.StringIO(json.dumps({"hook_event_name": "SessionStart", "cwd": str(_repo(tmp_path, "two"))})),
             buf, lambda *a: None, slow)
    assert buf.getvalue() == "" and time.monotonic() - started < 3


# ---- remote requests ----

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_remote_propose_and_fetch_requests(hook, monkeypatch, tmp_path):
    env_file = tmp_path / "client.env"
    env_file.write_text("export MEMD_REMOTE='https://memd.example.com/'\nexport MEMD_TOKEN=file-token\n"
                        "export MEMD_PROFILE=amber\n")
    monkeypatch.setenv("MEMD_ENV_FILE", str(env_file))
    seen = []

    def fake_urlopen(req, timeout):
        seen.append({"url": req.full_url, "method": req.get_method(), "timeout": timeout,
                     "auth": dict(req.header_items()).get("Authorization"),
                     "body": json.loads(req.data) if req.data else None})
        if req.get_method() == "POST":
            return _Resp(b'{"ok": true, "id": "0123456789abcdef"}')
        return _Resp(b'{"ok": true, "handoff": {"text": "t", "as_of": 1.0}}')

    monkeypatch.setattr(hook.urllib.request, "urlopen", fake_urlopen)
    assert hook.remote_propose({"title": "Handoff: r", "body": "b"}, 3.0)["id"] == "0123456789abcdef"
    assert hook.remote_fetch("git.example.com/team/widget", 14, 2.0) == {"text": "t", "as_of": 1.0}
    post, get = seen
    assert post["url"] == "https://memd.example.com/propose" and post["timeout"] == 3.0
    assert post["auth"] == "Bearer file-token" and post["body"]["profile"] == "amber"
    assert get["url"] == ("https://memd.example.com/handoff?repo=git.example.com%2Fteam%2Fwidget"
                          "&max_age_days=14&profile=amber")
    assert get["auth"] == "Bearer file-token"


def test_remote_without_server_raises(hook):
    with pytest.raises(RuntimeError):
        hook.remote_propose({}, 1.0)


def test_client_script_runs_standalone_and_silently(tmp_path):
    repo = _repo(tmp_path)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home"),
           "MEMD_ENV_FILE": str(tmp_path / "absent.env")}
    for event in (_end_event(repo, _transcript(tmp_path, repo)),
                  {"hook_event_name": "SessionStart", "cwd": str(repo), "source": "startup"}):
        proc = subprocess.run([sys.executable, str(CLIENT)], input=json.dumps(event), capture_output=True,
                              text=True, env=env, timeout=30)
        assert proc.returncode == 0 and proc.stdout == ""


# ---- store side: replace, approve, latest ----

@pytest.fixture
def store(git_clone, tmp_path, monkeypatch):
    from memd.refresh import ensure_lexical
    db = tmp_path / "memd.db"
    for key, value in {"MEMD_CLONE": str(git_clone), "MEMD_DB": str(db), "MEMD_PROFILE": "amber",
                       "MEMD_AMBER_CLONE": str(git_clone), "MEMD_AMBER_DB": str(db),
                       "MEMD_TOKEN": TOKEN, "MEMD_LOCAL_HOST": "any",
                       "MEMD_EMBED_URL": "http://127.0.0.1:9",
                       "MEMD_RERANK_URL": "http://127.0.0.1:9"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(recall_mod, "embed_with_deadline", lambda *a, **k: None)
    monkeypatch.setattr(recall_mod, "rerank", lambda *a, **k: None)
    monkeypatch.setattr(save_mod, "_vector_near_matches", lambda cfg, body: [])
    monkeypatch.setattr(save_mod, "_pull_rebase_push", lambda clone: None)
    actor.set_actor("")
    cfg = Config.from_env(env_file=None)
    ensure_lexical(cfg)
    return cfg


def _fact(repo="git.example.com/team/widget", body="Retry done; backoff next.", **extra):
    return {**pkg.handoff_fact({"key": repo}, body, NOW), **extra}


def test_repo_key_needs_source_tag_and_one_repo():
    assert handoff.repo_key(_fact()) == "git.example.com/team/widget"
    assert handoff.repo_key({**_fact(), "source": "agent"}) is None
    assert handoff.repo_key({**_fact(), "tags": ["repo:x"]}) is None
    assert handoff.repo_key({**_fact(), "tags": ["handoff", "repo:a", "repo:b"]}) is None
    assert handoff.repo_key({**_fact(), "tags": ["handoff", "repo:../etc"]}) is None


def test_new_handoff_replaces_previous_pending_for_same_repo(store):
    first = inbox.propose(_fact(body="First state of play."), "amber", cfg=store, proposer="laptop")
    other_repo = inbox.propose(_fact(repo="widget-docs", body="Docs state."), "amber", cfg=store,
                               proposer="laptop")
    other_proposer = inbox.propose(_fact(body="Desktop state."), "amber", cfg=store, proposer="desktop")
    plain = inbox.propose({"title": "Handoff: git.example.com/team/widget", "body": "A plain proposal.",
                           "tags": ["handoff", "repo:git.example.com/team/widget"]}, "amber", cfg=store,
                          proposer="laptop")
    second = inbox.propose(_fact(body=f"Second state of play, key {SECRET}."), "amber", cfg=store,
                           proposer="laptop")
    ids = {i["id"]: i for i in inbox.list_candidates(store, "amber")["items"]}
    assert first["id"] not in ids
    assert {other_repo["id"], other_proposer["id"], plain["id"], second["id"]} <= set(ids)
    assert ids[second["id"]]["source"] == "handoff" and ids[plain["id"]]["source"] == "agent"
    item = inbox.get(store, "amber", second["id"])
    assert SECRET not in item["body"] and "[REDACTED]" in item["body"]


def test_approved_handoff_is_a_low_importance_state_note_updated_in_place(store):
    cid = inbox.propose(_fact(body="First state of play."), "amber", cfg=store, proposer="laptop")["id"]
    receipt = inbox.approve(store, "amber", cid, reviewer="owner")["receipt"]
    assert receipt["saved"] is True
    notes = [n for n in list_notes(store.clone) if "handoff" in (n.tags or [])]
    [note] = notes
    assert note.importance == 1 and note.volatility == "state"
    assert "repo:git.example.com/team/widget" in note.tags
    cid = inbox.propose(_fact(body="Second state of play."), "amber", cfg=store, proposer="laptop")["id"]
    inbox.approve(store, "amber", cid, reviewer="owner")
    notes = [n for n in list_notes(store.clone) if "handoff" in (n.tags or [])]
    assert len(notes) == 1 and "Second state" in notes[0].body


def test_latest_prefers_newest_and_limits_pending_to_the_proposer(store):
    now = time.time()
    inbox.propose(_fact(body="Old approved state."), "amber", cfg=store, proposer="laptop", now=now - 7200)
    [old] = inbox.list_candidates(store, "amber")["items"]
    inbox.approve(store, "amber", old["id"], reviewer="owner")
    inbox.propose(_fact(body="Newer pending state."), "amber", cfg=store, proposer="laptop", now=now - 60)
    key = "git.example.com/team/widget"
    found = handoff.latest(store, "amber", key, proposer="laptop", now=now)
    assert found["status"] == "pending" and found["text"] == "Newer pending state."
    assert set(found) == {"repo", "status", "as_of", "text"}
    # Another credential never sees the pending one; it gets the approved note.
    found = handoff.latest(store, "amber", key, proposer="desktop", now=now)
    assert found["status"] == "approved" and "Old approved state." in found["text"]
    # Age limit.
    assert handoff.latest(store, "amber", key, proposer="desktop", now=now + 20 * 86400) is None
    assert handoff.latest(store, "amber", key, proposer="desktop", max_age=30, now=now + 20 * 86400)
    assert handoff.latest(store, "amber", "unknown-repo", proposer="laptop", now=now) is None
    with pytest.raises(ValueError):
        handoff.latest(store, "amber", "../x", proposer="laptop")


def test_package_hook_round_trip_in_process(store, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(pkg, "_config", lambda: Config.from_env(dict(os.environ), env_file=None))
    pkg.run(io.StringIO(json.dumps(_end_event(repo, _transcript(tmp_path, repo)))), io.StringIO(),
            pkg.propose, pkg.fetch)
    [item] = inbox.list_candidates(store, "amber")["items"]
    assert item["source"] == "handoff" and item["proposer"] == pkg.LOCAL_PROPOSER
    out = io.StringIO()
    pkg.run(io.StringIO(json.dumps({"hook_event_name": "SessionStart", "cwd": str(repo)})), out,
            pkg.propose, pkg.fetch)
    text = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert "Add retry logic to the widget loader" in text


# ---- HTTP route ----

def test_handoff_route_auth_and_own_proposals(store):
    with TestClient(server_mod.create_token_app()) as client:
        assert client.get("/handoff", params={"repo": "git.example.com/team/widget"}).status_code == 401
        r = client.post("/propose", headers=AUTH, json=_fact())
        assert r.status_code == 200, r.text
        r = client.get("/handoff", headers=AUTH, params={"repo": "Git.Example.com/team/widget"})
        assert r.status_code == 200, r.text
        found = r.json()["handoff"]
        assert found["status"] == "pending" and found["text"] == "Retry done; backoff next."
        assert set(found) == {"repo", "status", "as_of", "text"}
        assert client.get("/handoff", headers=AUTH, params={"repo": "../etc"}).status_code == 400
        assert client.get("/handoff", headers=AUTH, params={"repo": "other"}).json()["handoff"] is None
    # Filed by the legacy token under its label: another label cannot read it.
    assert handoff.latest(store, "amber", "git.example.com/team/widget", proposer="someone-else") is None


@pytest.fixture
def stores(config, tmp_path, monkeypatch):
    """Admin control with stores `amber` and `other`, and tokens for each."""
    monkeypatch.setenv("MEMD_ADMIN_DB", str(tmp_path / "control" / "admin.db"))
    monkeypatch.setenv("MEMD_ENFORCE_PROFILE", "1")
    monkeypatch.setenv("MEMD_LOCAL_HOST", "any")
    monkeypatch.setattr(sources._jobs, "submit", lambda fn, *args: fn(*args))
    monkeypatch.setattr(recall_mod, "embed_with_deadline", lambda *a, **k: None)
    monkeypatch.setattr(recall_mod, "rerank", lambda *a, **k: None)
    monkeypatch.setattr(save_mod, "_vector_near_matches", lambda cfg, body: [])
    monkeypatch.setattr(save_mod, "_pull_rebase_push", lambda clone: None)
    from memd.refresh import ensure_lexical
    control.initialize()
    control.register_existing()
    control.create_user("owner", "test-password-long-enough", "admin")
    ensure_lexical(config)
    app = server_mod.create_token_app()
    with TestClient(app) as admin:
        login = admin.post("/ui/login", json={"username": "owner", "password": "test-password-long-enough"})
        assert login.status_code == 200
        admin.headers["X-CSRF-Token"] = login.json()["csrf"]
        r = admin.post("/admin/stores", json={"id": "other", "name": "other", "kind": "local"})
        assert r.status_code == 200, r.text
        user = admin.post("/admin/users", json={"username": "member"}).json()
        admin.put(f"/admin/users/{user['id']}/grants", json={"grants": {"amber": "write", "other": "write"}})

        def issue(label, store, scope="write"):
            r = admin.post("/admin/tokens", json={"label": label, "user_id": user["id"], "store_id": store,
                                                  "scope": scope})
            assert r.status_code == 200, r.text
            return {"Authorization": "Bearer " + r.json()["token"]}
        yield TestClient(app), issue


def test_handoff_route_is_per_store_and_per_credential(stores):
    api, issue = stores
    laptop, desktop, other, reader = (issue("laptop", "amber"), issue("desktop", "amber"),
                                      issue("elsewhere", "other"), issue("reader", "amber", "read"))
    key = "git.example.com/team/widget"
    assert api.post("/propose", headers=laptop, json=_fact()).status_code == 200
    assert api.get("/handoff", headers=laptop, params={"repo": key}).json()["handoff"]["status"] == "pending"
    # Same store, another credential: the pending text stays private.
    assert api.get("/handoff", headers=desktop, params={"repo": key}).json()["handoff"] is None
    # Another store: nothing, and no way to name the first store.
    assert api.get("/handoff", headers=other, params={"repo": key}).json()["handoff"] is None
    r = api.get("/handoff", headers=other, params={"repo": key, "profile": "amber"})
    assert r.status_code == 403
    # A read-only token may not propose, so it may not read pending handoffs either.
    assert api.get("/handoff", headers=reader, params={"repo": key}).status_code == 403
    # The other store's own handoff is its own.
    assert api.post("/propose", headers=other, json=_fact(body="Other store state.")).status_code == 200
    assert api.get("/handoff", headers=other, params={"repo": key}).json()["handoff"]["text"] == \
        "Other store state."
    assert api.get("/handoff", headers=laptop, params={"repo": key}).json()["handoff"]["text"] == \
        "Retry done; backoff next."


def test_server_serves_the_handoff_hook():
    client = TestClient(server_mod.create_token_app())
    response = client.get("/clients/memd-handoff-hook")
    assert response.status_code == 200 and response.text == CLIENT.read_text()


# ---- onboarding: installed only on request, merged safely ----

def _executable(path, code):
    path.write_text("#!" + sys.executable + "\n" + code)
    path.chmod(0o700)


def _onboard(tmp_path, *args, settings=None, extra_env=None):
    home = tmp_path / "onboard-home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    if settings is not None:
        (home / ".claude" / "settings.json").write_text(settings)
    bins = tmp_path / "bin"
    bins.mkdir(exist_ok=True)
    _executable(bins / "curl", '''
import sys
from pathlib import Path
args = sys.argv[1:]
if "-d" in args:
    print('{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"recall"},{"name":"save"}]}}\\n200')
elif any("/clients/" in a for a in args):
    Path(args[args.index("-o") + 1]).write_text("#!/bin/sh\\nexit 0\\n")
''')
    _executable(bins / "claude", "pass\n")
    _executable(bins / "codex", "pass\n")
    env = dict(os.environ, HOME=str(home), PATH=str(bins) + os.pathsep + os.environ["PATH"])
    env.pop("CODEX_HOME", None)
    env.pop("MEMD_HANDOFF", None)
    env.pop("MEMD_ACTIVITY_HOOK", None)
    env.update(extra_env or {})
    run = subprocess.run(["bash", str(ROOT / "clients/onboard.sh"), *args], env=env,
                         capture_output=True, text=True, timeout=30)
    return run, home


def _handoff_entries(settings, event):
    return [(group, h) for group in settings.get("hooks", {}).get(event, [])
            for h in group.get("hooks", []) if "memd-handoff-hook" in h.get("command", "")]


def test_onboarding_installs_handoff_hook_on_flag_idempotently(tmp_path):
    existing = {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
        {"type": "command", "command": "show-todo"}]}]}, "theme": "dark"}
    run, home = _onboard(tmp_path, "--handoff", "mem_amber_test-token", "https://memd.example.com",
                         settings=json.dumps(existing))
    assert run.returncode == 0, run.stdout + run.stderr
    hook_file = home / ".claude/hooks/memd-handoff-hook"
    assert hook_file.exists() and os.access(hook_file, os.X_OK)
    settings = json.loads((home / ".claude/settings.json").read_text())
    assert settings["theme"] == "dark"
    assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "show-todo"
    [(end_group, end)] = _handoff_entries(settings, "SessionEnd")
    [(start_group, start)] = _handoff_entries(settings, "SessionStart")
    assert "matcher" not in end_group and start_group["matcher"] == "startup|resume"
    assert end["timeout"] == 15 and start["timeout"] == 10
    for entry in (end, start):
        assert "MEMD_REMOTE=https://memd.example.com" in entry["command"]
        assert "MEMD_ENV_FILE=" in entry["command"] and "test-token" not in entry["command"]
        assert entry["command"].endswith("/.claude/hooks/memd-handoff-hook")
    assert "PostToolUse" not in settings["hooks"]
    backups = list((home / ".claude").glob("settings.json.memd-backup.*"))
    assert backups and json.loads(backups[0].read_text()) == existing
    run, home = _onboard(tmp_path, "mem_amber_test-token", "https://memd.example.com", "--handoff")
    assert run.returncode == 0, run.stdout + run.stderr
    settings = json.loads((home / ".claude/settings.json").read_text())
    assert len(_handoff_entries(settings, "SessionEnd")) == 1
    assert len(_handoff_entries(settings, "SessionStart")) == 1
    assert len(settings["hooks"]["SessionStart"]) == 2


def test_onboarding_leaves_handoff_hook_off_by_default(tmp_path):
    run, home = _onboard(tmp_path, "mem_amber_test-token", "https://memd.example.com")
    assert run.returncode == 0, run.stdout + run.stderr
    settings = json.loads((home / ".claude/settings.json").read_text())
    assert "SessionEnd" not in settings["hooks"] and "SessionStart" not in settings["hooks"]
    assert not (home / ".claude/hooks/memd-handoff-hook").exists()
    assert "--handoff" in run.stdout


def test_onboarding_handoff_hook_from_env(tmp_path):
    run, home = _onboard(tmp_path, "mem_amber_test-token", "https://memd.example.com",
                         extra_env={"MEMD_HANDOFF": "1"})
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(_handoff_entries(json.loads((home / ".claude/settings.json").read_text()), "SessionEnd")) == 1


def test_onboarding_restores_settings_when_handoff_merge_fails(tmp_path):
    original = '{"hooks": {"SessionEnd": "not-a-list"}}'
    run, home = _onboard(tmp_path, "--handoff", "mem_amber_test-token", "https://memd.example.com",
                         settings=original)
    assert run.returncode != 0 and "Onboarding complete." not in run.stdout
    assert (home / ".claude/settings.json").read_text() == original
    assert "Restored Claude settings" in run.stdout


def test_onboarding_usage_mentions_handoff_flag():
    run = subprocess.run(["bash", str(ROOT / "clients/onboard.sh"), "--handoff"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20)
    assert run.returncode != 0 and "--handoff" in run.stderr and "Usage:" in run.stderr


# ---- untrusted repository config never runs a command ----

def _marker_script(tmp_path, marker):
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexec cat\n")
    script.chmod(0o755)
    return script


def test_git_state_ignores_repository_fsmonitor(hook, tmp_path):
    repo = _repo(tmp_path)
    marker = tmp_path / "fsmonitor-ran"
    _git(repo, "config", "core.fsmonitor", str(_marker_script(tmp_path, marker)))
    (repo / "app.py").write_text("print('changed')\n")
    state = hook.git_state(str(repo))
    assert not marker.exists()
    assert state["changes"]["modified"] == 1


def test_git_state_skips_status_when_repository_defines_filters(hook, tmp_path):
    repo = _repo(tmp_path)
    marker = tmp_path / "filter-ran"
    script = _marker_script(tmp_path, marker)
    (repo / ".gitattributes").write_text("* filter=evil\n")
    _git(repo, "config", "filter.evil.clean", str(script))
    _git(repo, "config", "filter.evil.smudge", str(script))
    (repo / "app.py").write_text("print('changed')\n")
    state = hook.git_state(str(repo))
    assert not marker.exists()
    assert "changes" not in state
    assert state["branch"] == "main"


def test_repo_defines_filters_is_false_for_a_plain_repository(hook, tmp_path):
    assert hook.repo_defines_filters(str(_repo(tmp_path))) is False
