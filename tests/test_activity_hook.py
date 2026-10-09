"""Activity recall: the PostToolUse hook that recalls on a tool call's target.

Covers both copies -- the package module (memd.hooks.activity_recall) and the
standalone client script (clients/memd-activity-hook) -- through one ``hook``
fixture, plus a guard that the shared block stays byte-identical between them.
Hermetic: recall is a fake callable or a monkeypatched urlopen, the state
directory lives under tmp_path, and no developer env file is read.
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

import pytest

import memd.config as config
import memd.hooks.activity_recall as pkg
import memd.hosts as hosts

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "clients" / "memd-activity-hook"
SHARED_START = "# --- shared: memd.hooks.activity_recall <-> clients/memd-activity-hook"
SHARED_END = "# --- end shared ---"


def _load_client():
    loader = importlib.machinery.SourceFileLoader("memd_activity_hook", str(CLIENT))
    spec = importlib.util.spec_from_loader("memd_activity_hook", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


CLIENT_MOD = _load_client()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMD_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture(params=["package", "client"])
def hook(request):
    return pkg if request.param == "package" else CLIENT_MOD


def _shared(text):
    return text[text.index(SHARED_START):text.index(SHARED_END)]


def test_shared_block_is_identical_in_both_copies():
    assert _shared((ROOT / "memd/hooks/activity_recall.py").read_text()) == _shared(CLIENT.read_text())


def test_client_host_tables_match_the_package():
    assert CLIENT_MOD.HOST_ROLES == config.HOST_ROLES
    assert CLIENT_MOD._ALIASES == hosts._ALIASES


def test_client_is_stdlib_only():
    imports = [line for line in CLIENT.read_text().splitlines()
               if line.startswith(("import ", "from "))]
    assert not [line for line in imports if "memd" in line or "httpx" in line]


# ---- signal extraction ----

BASH_CASES = [
    ("ssh vmhost", [("host", "vmhost")]),
    ("ssh -p 2200 -i ~/.ssh/key admin@vmhost uptime", [("host", "vmhost")]),
    ("ssh -l admin -oBatchMode=yes gpuhost", [("host", "gpuhost")]),
    ("ssh ssh://admin@vmhost:2200", [("host", "vmhost")]),
    ("ssh vmhost 'sudo systemctl restart nginx.service'",
     [("host", "vmhost"), ("service", "nginx")]),
    ("mosh gpuhost", [("host", "gpuhost")]),
    ("scp -P 22 ./x.txt admin@10.10.1.20:/tmp/", [("host", "10.10.1.20")]),
    ("scp vmhost:/etc/hosts gpuhost:/tmp/", [("host", "vmhost"), ("host", "gpuhost")]),
    ("rsync -avz -e 'ssh -p 22' ./ gpuhost:/srv/app/", [("host", "gpuhost")]),
    ("rsync -a rsync://vmhost/module/ ./out", [("host", "vmhost")]),
    ("sftp admin@vmhost", [("host", "vmhost")]),
    ("ssh apphost.lan", [("host", "apphost")]),
    ("ssh 10.10.1.10", [("host", "apphost")]),
    ("ssh hass", [("host", "homeassistant")]),
    ("ssh vmhost.example.com", [("host", "vmhost")]),
    ("docker logs -f --tail 100 grafana 2>&1 | tail -n 5", [("service", "grafana")]),
    ("docker restart grafana prometheus", [("service", "grafana"), ("service", "prometheus")]),
    ("docker exec -it -u 0 memd sh -c 'ls /data'", [("service", "memd")]),
    ("docker container inspect -f '{{.State}}' caddy", [("service", "caddy")]),
    ("docker cp memd:/data/x ./x", [("service", "memd")]),
    ("docker run --rm --name scratchpad alpine true", [("service", "scratchpad")]),
    ("docker -H ssh://vmhost ps", [("host", "vmhost")]),
    ("docker compose -f ~/docker/memd/compose.yaml logs --tail 50 memd", [("service", "memd")]),
    ("docker compose -p paperless restart", [("service", "paperless")]),
    ("docker-compose up -d web db", [("service", "web"), ("service", "db")]),
    ("sudo -u root systemctl status caddy", [("service", "caddy")]),
    ("systemctl --user restart pipewire wireplumber",
     [("service", "pipewire"), ("service", "wireplumber")]),
    ("systemctl -H vmhost is-active memd.timer", [("host", "vmhost"), ("service", "memd")]),
    ("systemctl list-units --failed", []),
    ("systemctl restart 'memd-*'", []),
    ("journalctl -u postgresql -n 50 --no-pager", [("service", "postgresql")]),
    ("journalctl --unit=memd.service -f", [("service", "memd")]),
    ("sudo service nginx reload", [("service", "nginx")]),
    ("kubectl -n prod logs deploy/api", [("service", "api")]),
    ("kubectl logs web-5d8f7c9b6d-x7k2p -c app", [("service", "web")]),
    ("kubectl rollout restart deployment/web", [("service", "web")]),
    ("kubectl get pods", []),
    ("kubectl describe pod cache-0", [("service", "cache-0")]),
    ("curl -fsS https://api.example.com/v1/items", [("host", "api.example.com")]),
    ("curl http://localhost:8077/health", []),
    ("wget -q http://127.0.0.1/", []),
    ("cd /srv && bash -c 'ssh vmhost ls'", [("host", "vmhost")]),
    ("echo $(ssh gpuhost hostname)", [("host", "gpuhost")]),
    ("FOO=1 timeout 10 ssh vmhost true", [("host", "vmhost")]),
    ("sudo -i systemctl restart caddy", [("service", "caddy")]),
    ("timeout -s KILL 5 ssh vmhost true", [("host", "vmhost")]),
    ("env -u HOME ssh gpuhost", [("host", "gpuhost")]),
    ("ssh $HOST ls", []),
    ("git status && ls -la", []),
    ("ls # ssh vmhost", []),
    ("echo 'unterminated", []),
    ("", []),
]


@pytest.mark.parametrize("command, expected", BASH_CASES, ids=[c or "empty" for c, _ in BASH_CASES])
def test_bash_signals(hook, command, expected):
    assert hook.extract_signals("Bash", {"command": command}) == expected


def test_host_names_map_aliases_to_deployment_names(hook, monkeypatch):
    monkeypatch.setenv("MEMD_HOST_NAMES", "vmhost=buildbox,apphost=nas")
    assert hook.extract_signals("Bash", {"command": "ssh vmhost"}) == [("host", "buildbox")]
    assert hook.extract_signals("Bash", {"command": "ssh 10.10.1.10"}) == [("host", "nas")]
    assert hook.host_filter([("host", "buildbox")]) == "buildbox"


def test_host_filter_only_for_one_known_host(hook):
    assert hook.host_filter([("host", "vmhost"), ("service", "nginx")]) == "vmhost"
    assert hook.host_filter([("host", "api.example.com")]) is None
    assert hook.host_filter([("host", "vmhost"), ("host", "gpuhost")]) is None
    assert hook.host_filter([("service", "nginx")]) is None


def test_file_signals_find_repo(hook, tmp_path):
    repo = tmp_path / "widgets"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    target = repo / "src" / "render.py"
    assert hook.extract_signals("Edit", {"file_path": str(target), "old_string": "a"}) == [
        ("file", str(target)), ("repo", "widgets")]
    # Relative paths resolve against the event's cwd; a worktree's .git file counts.
    wt = tmp_path / "wt-copy"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: elsewhere\n")
    assert hook.extract_signals("Write", {"file_path": "notes.txt"}, cwd=str(wt)) == [
        ("file", str(wt / "notes.txt")), ("repo", "wt-copy")]
    assert hook.extract_signals("Read", {"file_path": str(tmp_path / "loose.txt")}) == [
        ("file", str(tmp_path / "loose.txt"))]
    assert hook.extract_signals("NotebookEdit", {"notebook_path": str(target)})[0] == ("file", str(target))


@pytest.mark.parametrize("tool, tool_input", [
    ("Glob", {"pattern": "*.py"}),
    ("Edit", {"file_path": ""}),
    ("Edit", "not a dict"),
    ("Bash", {"command": 42}),
    (None, None),
])
def test_no_signals_for_other_input(hook, tool, tool_input):
    assert hook.extract_signals(tool, tool_input) == []


def test_query_terms_skip_generic_basenames(hook):
    signals = [("file", "/w/widgets/README.md"), ("repo", "widgets"), ("host", "vmhost")]
    assert hook.query_terms(signals) == ["vmhost", "widgets"]
    assert hook.query_terms([("file", "/w/x/compose.yaml")]) == ["compose.yaml"]


def test_signal_count_is_capped(hook):
    command = "docker restart " + " ".join(f"svc{i}" for i in range(20))
    assert len(hook.extract_signals("Bash", {"command": command})) == hook.MAX_SIGNALS


# ---- per-session state ----

def test_claim_dedupes_per_session(hook, tmp_path):
    d = tmp_path / "state"
    now = 1_000_000.0
    assert hook.claim(["host:vmhost", "service:nginx"], "s1", now, str(d)) == [
        "host:vmhost", "service:nginx"]
    assert hook.claim(["host:vmhost"], "s1", now + 5, str(d)) == []
    assert hook.claim(["host:vmhost", "repo:widgets"], "s1", now + 6, str(d)) == ["repo:widgets"]
    assert hook.claim(["host:vmhost"], "s2", now + 7, str(d)) == ["host:vmhost"]
    # Entries past the TTL are forgotten.
    later = now + hook.STATE_TTL_SECONDS + 10
    assert hook.claim(["host:vmhost"], "s1", later, str(d)) == ["host:vmhost"]
    names = sorted(p.name for p in d.iterdir() if p.suffix == ".json")   # activity.lock aside
    assert len(names) == 2 and all(n.startswith("activity-") for n in names)
    assert not any("s1" in n for n in names)          # session ids are hashed


def test_state_files_are_pruned_by_age(hook, tmp_path):
    d = tmp_path / "state"
    now = time.time()
    hook.claim(["host:vmhost"], "old", now, str(d))
    hook.claim(["host:vmhost"], "recent", now, str(d))
    old_file = Path(hook._state_path(str(d), "old"))
    stale = now - hook.STATE_TTL_SECONDS - 60
    os.utime(old_file, (stale, stale))
    unrelated = d / "keep-me.txt"
    unrelated.write_text("x")
    os.utime(unrelated, (stale, stale))
    hook.claim(["host:vmhost"], "new-session", now, str(d))
    assert not old_file.exists()
    assert Path(hook._state_path(str(d), "recent")).exists()
    assert unrelated.exists()


def test_state_entries_are_capped(hook, tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STATE_MAX_ENTRIES", 5)
    d = str(tmp_path / "state")
    for i in range(12):
        hook.claim([f"file:/f{i}"], "s", 1000.0 + i, d)
    data = json.loads(Path(hook._state_path(d, "s")).read_text())
    assert set(data["seen"]) == {f"file:/f{i}" for i in range(7, 12)}


def test_corrupt_state_is_reset(hook, tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    Path(hook._state_path(str(d), "s")).write_text("{not json")
    assert hook.claim(["host:vmhost"], "s", 1000.0, str(d)) == ["host:vmhost"]


def test_default_state_dir_is_under_xdg_cache(hook, tmp_path):
    assert hook.state_dir() == str(tmp_path / "cache" / "memd" / "hook-state")


# ---- handle(): once per target, bar, caps, deadline, silence ----

def _event(command="ssh vmhost 'systemctl restart nginx'", session="sess-1", tool="Bash"):
    return {"session_id": session, "hook_event_name": "PostToolUse", "tool_name": tool,
            "tool_input": {"command": command}, "cwd": "/tmp"}


class FakeRecall:
    def __init__(self, notes=None, recall_id="r-1", delay=0.0, error=None):
        self.notes = notes if notes is not None else [
            {"slug": "vmhost-nginx", "host": "vmhost", "matched": True,
             "body": "nginx on vmhost proxies the dashboard; reload after cert renewals."},
        ]
        self.recall_id, self.delay, self.error, self.calls = recall_id, delay, error, []

    def __call__(self, query, host, k, timeout):
        self.calls.append({"query": query, "host": host, "k": k, "timeout": timeout})
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return list(self.notes), self.recall_id


def test_output_shape_and_content(hook):
    fake = FakeRecall()
    out = hook.handle(_event(), fake)
    assert set(out) == {"hookSpecificOutput"}
    assert set(out["hookSpecificOutput"]) == {"hookEventName", "additionalContext"}
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "### vmhost-nginx" in text and "reload after cert renewals" in text
    assert 'recall_id="r-1"' in text
    assert fake.calls == [{"query": "vmhost nginx", "host": "vmhost",
                           "k": hook.DEFAULT_TOP_N + 2, "timeout": 1.5}]


def test_each_target_recalls_once_per_session(hook):
    fake = FakeRecall()
    assert hook.handle(_event(), fake)
    assert hook.handle(_event(), fake) is None
    assert hook.handle(_event("ssh vmhost uptime"), fake) is None
    assert len(fake.calls) == 1
    # A new target in the same session recalls again, but an injected note does not repeat.
    assert hook.handle(_event("ssh vmhost 'journalctl -u caddy'"), fake) is None
    assert len(fake.calls) == 2
    # A different session starts afresh.
    assert hook.handle(_event(session="sess-2"), fake)


def test_minimal_relevance_bar(hook):
    notes = [
        {"slug": "unrelated", "host": "gpuhost", "matched": True, "body": "GPU tuning notes."},
        {"slug": "core-stub", "host": "any", "matched": False, "body": "nginx everywhere"},
        {"slug": "empty", "host": "any", "matched": True, "body": ""},
    ]
    assert hook.handle(_event("docker restart nginx"), FakeRecall(notes)) is None
    notes.append({"slug": "proxy", "host": "any", "matched": True, "tags": ["nginx"],
                  "body": "The reverse proxy config lives in the stack repo."})
    out = hook.handle(_event("docker logs nginx", session="sess-2"), FakeRecall(notes))
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "### proxy" in text and "unrelated" not in text and "core-stub" not in text


def test_file_stem_counts_as_a_mention(hook, tmp_path):
    notes = [{"slug": "renderer", "matched": True, "body": "The render_pipeline module caches."}]
    event = {"session_id": "s", "tool_name": "Edit",
             "tool_input": {"file_path": str(tmp_path / "render_pipeline.py")}}
    assert hook.handle(event, FakeRecall(notes))


def test_injection_is_hard_capped(hook, monkeypatch):
    notes = [{"slug": f"vmhost-{i}", "host": "vmhost", "matched": True,
              "body": "nginx " + "x" * 20000} for i in range(8)]
    out = hook.handle(_event(), FakeRecall(notes))
    text = out["hookSpecificOutput"]["additionalContext"]
    assert len(text) <= hook.DEFAULT_MAX_CHARS
    assert text.count("### ") <= hook.DEFAULT_TOP_N
    monkeypatch.setenv("MEMD_ACTIVITY_MAX_CHARS", "999999")
    out = hook.handle(_event(session="other"), FakeRecall(notes))
    assert len(out["hookSpecificOutput"]["additionalContext"]) <= hook.MAX_CHARS_CEILING


def test_deadline_bounds_a_slow_recall(hook, monkeypatch):
    monkeypatch.setenv("MEMD_ACTIVITY_DEADLINE_MS", "150")
    started = time.monotonic()
    assert hook.handle(_event(), FakeRecall(delay=3.0)) is None
    assert time.monotonic() - started < 1.5


def test_recall_error_is_silent(hook):
    assert hook.handle(_event(), FakeRecall(error=OSError("refused"))) is None
    assert hook.handle(_event(session="s9"), lambda *a: "garbage", now=time.time() + 120) is None


def test_failure_backs_off_further_recalls(hook):
    now = time.time()
    assert hook.handle(_event(), FakeRecall(error=OSError("refused")), now=now) is None
    fake = FakeRecall()
    assert hook.handle(_event(session="s2"), fake, now=now + 10) is None
    assert fake.calls == []
    assert hook.handle(_event(session="s3"), fake, now=now + hook.BACKOFF_SECONDS + 1)
    assert len(fake.calls) == 1


def test_disabled_by_env(hook, monkeypatch):
    monkeypatch.setenv("MEMD_ACTIVITY_HOOK", "0")
    fake = FakeRecall()
    assert hook.handle(_event(), fake) is None
    assert fake.calls == []


def test_unwritable_state_injects_nothing(hook, tmp_path):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("")
    fake = FakeRecall()
    assert hook.handle(_event(), fake, directory=str(blocker / "sub")) is None
    assert fake.calls == []


def test_no_signal_means_no_recall(hook):
    fake = FakeRecall()
    assert hook.handle(_event("ls -la"), fake) is None
    assert hook.handle({"tool_name": "Glob", "tool_input": {}}, fake) is None
    assert hook.handle("not a dict", fake) is None
    assert fake.calls == []


@pytest.mark.parametrize("stdin", ["", "{not json", "[]", json.dumps({"tool_name": "Bash"})])
def test_run_is_silent_on_bad_input(hook, stdin):
    out, err = io.StringIO(), io.StringIO()
    old = sys.stderr
    sys.stderr = err
    try:
        assert hook.run(io.StringIO(stdin), out, FakeRecall()) == 0
    finally:
        sys.stderr = old
    assert out.getvalue() == ""


def test_run_writes_one_json_object(hook):
    out = io.StringIO()
    assert hook.run(io.StringIO(json.dumps(_event())), out, FakeRecall()) == 0
    data = json.loads(out.getvalue())
    assert data["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


def test_run_survives_a_crashing_fetch_path(hook, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(hook, "extract_signals", boom)
    out = io.StringIO()
    assert hook.run(io.StringIO(json.dumps(_event())), out, FakeRecall()) == 0
    assert out.getvalue() == ""


# ---- remote recall request ----

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_remote_fetch_request(hook, monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"], seen["timeout"] = req.full_url, timeout
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data)
        return _Resp(json.dumps({"notes": [{"slug": "a", "body": "b"}, "junk"],
                                 "recall_id": "rid"}).encode())

    monkeypatch.setattr(hook.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("MEMD_REMOTE", "https://memd.example.com/")
    monkeypatch.setenv("MEMD_TOKEN", "tok-123")
    monkeypatch.setenv("MEMD_PROFILE", "amber")
    notes, rid = hook.remote_fetch("vmhost nginx", "vmhost", 5, 1.5)
    assert notes == [{"slug": "a", "body": "b"}] and rid == "rid"
    assert seen["url"] == "https://memd.example.com/recall"
    assert seen["timeout"] == 1.5
    assert seen["headers"]["authorization"] == "Bearer tok-123"
    assert seen["body"] == {"query": "vmhost nginx", "k": 5, "include_core": False,
                            "max_chars": 4000, "profile": "amber", "host": "vmhost"}
    # No host filter -> no host key.
    hook.remote_fetch("widgets", None, 5, 1.5)
    assert "host" not in seen["body"]


def test_remote_settings_come_from_env_file(hook, monkeypatch, tmp_path):
    env_file = tmp_path / "client.env"
    env_file.write_text("export MEMD_REMOTE='https://memd.example.com'\n"
                        "export MEMD_TOKEN=file-token\n")
    monkeypatch.setenv("MEMD_ENV_FILE", str(env_file))
    monkeypatch.setenv("MEMD_TOKEN", "stale-shell-token")
    seen = {}

    def fake_urlopen(req, timeout):
        seen["auth"] = dict(req.header_items()).get("Authorization")
        return _Resp(b'{"notes": []}')

    monkeypatch.setattr(hook.urllib.request, "urlopen", fake_urlopen)
    assert hook.remote_fetch("q", None, 3, 1.0) == ([], None)
    assert seen["auth"] == "Bearer file-token"


def test_remote_fetch_without_remote_raises(hook):
    with pytest.raises(RuntimeError):
        hook.remote_fetch("q", None, 3, 1.0)


def test_package_uses_in_process_recall_without_remote(monkeypatch):
    seen = {}

    class _N:
        def to_dict(self):
            return {"slug": "vmhost-nginx", "host": "vmhost", "matched": True,
                    "body": "nginx on vmhost"}

    def fake_recall(query, profile="amber", k=8, *, cfg=None, include_core=True, host=None):
        seen.update(query=query, k=k, include_core=include_core, host=host)
        return [_N()]

    import memd.recall
    monkeypatch.setattr(memd.recall, "recall", fake_recall)
    out = pkg.handle(_event(), pkg.fetch)
    assert "vmhost-nginx" in out["hookSpecificOutput"]["additionalContext"]
    assert seen == {"query": "vmhost nginx", "k": pkg.DEFAULT_TOP_N + 2,
                    "include_core": False, "host": "vmhost"}


def test_client_script_runs_standalone_and_silently(tmp_path):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
           "XDG_CACHE_HOME": str(tmp_path / "cache")}
    proc = subprocess.run([sys.executable, str(CLIENT)], input=json.dumps(_event()),
                          capture_output=True, text=True, env=env, timeout=20)
    assert proc.returncode == 0 and proc.stdout == ""


# ---- onboarding: installed only on request, merged safely ----

def _executable(path, code):
    path.write_text("#!" + sys.executable + "\n" + code)
    path.chmod(0o700)


def _onboard(tmp_path, *args, settings=None, extra_env=None):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    if settings is not None:
        (home / ".claude" / "settings.json").write_text(settings)
    bins = tmp_path / "bin"
    bins.mkdir(exist_ok=True)
    _executable(bins / "curl", '''
import os
import sys
from pathlib import Path
args = sys.argv[1:]
if os.environ.get("CURL_LOG"):
    with open(os.environ["CURL_LOG"], "a") as log:
        log.write("ARGV: " + " ".join(args) + "\\n")
        if "-K" in args:
            log.write("CONFIG: " + sys.stdin.read() + "\\n")
if "-d" in args:
    print('{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"recall"},{"name":"save"}]}}\\n200')
elif any("/clients/" in a for a in args):
    Path(args[args.index("-o") + 1]).write_text("#!/bin/sh\\nexit 0\\n")
''')
    _executable(bins / "claude", "pass\n")
    _executable(bins / "codex", "pass\n")
    env = dict(os.environ, HOME=str(home), PATH=str(bins) + os.pathsep + os.environ["PATH"])
    env.pop("CODEX_HOME", None)
    env.update(extra_env or {})
    run = subprocess.run(["bash", str(ROOT / "clients/onboard.sh"), *args], env=env,
                         capture_output=True, text=True, timeout=30)
    return run, home


def _activity_entries(settings):
    return [(group, h) for group in settings.get("hooks", {}).get("PostToolUse", [])
            for h in group.get("hooks", []) if "memd-activity-hook" in h.get("command", "")]


def test_onboarding_installs_activity_hook_on_flag(tmp_path):
    existing = {"hooks": {"PostToolUse": [
        {"matcher": "Write", "hooks": [{"type": "command", "command": "fmt-on-save"}]}]},
        "theme": "dark"}
    run, home = _onboard(tmp_path, "--activity-hook", "mem_amber_test-token",
                         "https://memd.example.com", settings=json.dumps(existing))
    assert run.returncode == 0, run.stdout + run.stderr
    hook_file = home / ".claude/hooks/memd-activity-hook"
    assert hook_file.exists() and os.access(hook_file, os.X_OK)
    settings = json.loads((home / ".claude/settings.json").read_text())
    assert settings["theme"] == "dark"
    assert settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "fmt-on-save"
    [(group, entry)] = _activity_entries(settings)
    assert group["matcher"] == "Bash|Edit|Write|MultiEdit|Read|NotebookEdit"
    assert entry["type"] == "command" and entry["timeout"] == 5
    assert "MEMD_REMOTE=https://memd.example.com" in entry["command"]
    assert "MEMD_ENV_FILE=" in entry["command"] and "test-token" not in entry["command"]
    assert entry["command"].endswith("/.claude/hooks/memd-activity-hook")
    assert settings["hooks"]["UserPromptSubmit"]
    backups = list((home / ".claude").glob("settings.json.memd-backup.*"))
    assert backups and json.loads(backups[0].read_text()) == existing
    # Idempotent: a second run replaces the entry instead of adding another.
    run, home = _onboard(tmp_path, "mem_amber_test-token", "https://memd.example.com",
                         "--activity-hook")
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(_activity_entries(json.loads((home / ".claude/settings.json").read_text()))) == 1


def test_onboarding_leaves_activity_hook_off_by_default(tmp_path):
    run, home = _onboard(tmp_path, "mem_amber_test-token", "https://memd.example.com")
    assert run.returncode == 0, run.stdout + run.stderr
    settings = json.loads((home / ".claude/settings.json").read_text())
    assert "PostToolUse" not in settings["hooks"]
    assert not (home / ".claude/hooks/memd-activity-hook").exists()
    assert "--activity-hook" in run.stdout


def test_onboarding_activity_hook_from_env(tmp_path):
    run, home = _onboard(tmp_path, "mem_amber_test-token", "https://memd.example.com",
                         extra_env={"MEMD_ACTIVITY_HOOK": "1"})
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(_activity_entries(json.loads((home / ".claude/settings.json").read_text()))) == 1


def test_onboarding_restores_settings_when_merge_fails(tmp_path):
    original = '{"hooks": {"PostToolUse": "not-a-list"}}'
    run, home = _onboard(tmp_path, "--activity-hook", "mem_amber_test-token",
                         "https://memd.example.com", settings=original)
    assert run.returncode != 0
    assert "Onboarding complete." not in run.stdout
    assert (home / ".claude/settings.json").read_text() == original
    assert "Restored Claude settings" in run.stdout


def test_onboarding_usage_mentions_flag(tmp_path):
    run = subprocess.run(["bash", str(ROOT / "clients/onboard.sh"), "--activity-hook"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20)
    assert run.returncode != 0 and "--activity-hook" in run.stderr and "Usage:" in run.stderr


def test_server_serves_the_activity_hook():
    from fastapi.testclient import TestClient
    import memd.server as server_mod
    client = TestClient(server_mod.create_token_app())   # no lifespan: nothing starts
    response = client.get("/clients/memd-activity-hook")
    assert response.status_code == 200
    assert response.text == CLIENT.read_text()


def test_recalls_per_session_are_capped(hook, tmp_path, monkeypatch):
    d = str(tmp_path / "state")
    calls = []

    def fetch(query, host, k, timeout):
        calls.append(query)
        return [], None
    monkeypatch.setenv("MEMD_ACTIVITY_MAX_RECALLS", "3")
    now = 1_000_000.0
    for i in range(6):
        event = {"session_id": "s", "tool_name": "Read", "tool_input": {"file_path": f"/srv/app/module{i}.py"}}
        hook.handle(event, fetch, now=now + i, directory=d)
    assert len(calls) == 3
    other = {"session_id": "t", "tool_name": "Read", "tool_input": {"file_path": "/srv/app/other.py"}}
    hook.handle(other, fetch, now=now + 10, directory=d)
    assert len(calls) == 4                      # the cap is per session


def test_parallel_claims_in_one_session_inject_once(hook, tmp_path):
    import threading
    d = str(tmp_path / "state")
    results, barrier = [], threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(hook.claim(["note:shared"], "s", 1_000_000.0, d))
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sum(1 for r in results if r == ["note:shared"]) == 1


# ---- failed commands: "seen this error before?" ----

NORMALISE_CASES = [
    ("\x1b[31mError:\x1b[0m cannot find module 'express'\r", "Error: cannot find module 'express'"),
    ('  File "/home/u/app/main.py", line 12, in <module>', "File, in <module>"),
    ("bash: /etc/nginx/nginx.conf: Permission denied", "bash: Permission denied"),
    ("Sep 28 10:00:01 somehost nginx[12345]: nginx: [emerg] bind() to 0.0.0.0:80 failed "
     "(98: Address already in use)",
     "nginx: nginx: [emerg] bind to failed (98: Address already in use)"),
    ("2026-09-28T10:00:00.123Z ERROR worker pid=4242 crashed", "ERROR worker crashed"),
    ("docker: Error response from daemon: Conflict. The container name is already in use "
     "by container 3f2a9b8c7d6e5f4a3b2c1d0e.",
     "docker: Error response from daemon: Conflict. The container name is already in use "
     "by container ."),
    ("request 123e4567-e89b-12d3-a456-426614174000 failed at 0x7ffd1234", "request failed at"),
    ("curl: (7) Failed to connect to 10.10.1.5 port 8080 after 3 ms: Connection refused",
     "curl: (7) Failed to connect to port after 3 ms: Connection refused"),
    ("error[E0425]: cannot find value `x` in this scope --> src/main.rs:4:5",
     "error[E0425]: cannot find value `x` in this scope -->"),
    ("fatal: unable to access 'https://git.example.com/repo.git/': Could not resolve host",
     "fatal: unable to access: Could not resolve host"),
    ("", ""),
    (None, ""),
]


@pytest.mark.parametrize("line, expected", NORMALISE_CASES)
def test_normalise_error_line(hook, line, expected):
    assert hook.normalise_error_line(line) == expected


ERROR_CASES = [
    ("python-traceback",
     'Traceback (most recent call last):\n  File "/home/u/app/main.py", line 12, in <module>\n'
     "    import yaml\nModuleNotFoundError: No module named 'yaml'\n",
     ["ModuleNotFoundError: No module named 'yaml'"],
     ["ModuleNotFoundError", "no module named", "yaml"]),
    ("systemd",
     "Job for nginx.service failed because the control process exited with error code.\n"
     'See "systemctl status nginx.service" and "journalctl -xeu nginx.service" for details.\n',
     ["Job for nginx.service failed because the control process exited with error code."],
     ["nginx"]),
    ("journal",
     "Sep 28 10:00:01 somehost memd[812]: sqlite3.OperationalError: database is locked\n"
     "Sep 28 10:00:01 somehost systemd[1]: memd.service: Main process exited, code=exited, "
     "status=1/FAILURE\n",
     ["memd: sqlite3.OperationalError: database is locked",
      "systemd: memd.service: Main process exited, code=exited, status=1/FAILURE"],
     ["OperationalError", "memd"]),
    ("docker",
     "docker: Error response from daemon: driver failed programming external connectivity on "
     "endpoint web (3f2a9b8c7d6e5f4a3b2c1d0e): Bind for 0.0.0.0:8080 failed: port is already "
     "allocated.\n",
     ["docker: Error response from daemon: driver failed programming external connectivity on "
      "endpoint web: Bind for failed: port is already allocated."],
     ["port is already allocated"]),
    ("npm",
     "npm ERR! code ERESOLVE\nnpm ERR! ERESOLVE unable to resolve dependency tree\nnpm ERR!\n"
     "npm ERR! A complete log of this run can be found in:\n"
     "npm ERR!     /home/u/.npm/_logs/2026-09-28T10_00_00_000Z-debug-0.log\n",
     ["npm ERR! code ERESOLVE", "npm ERR! ERESOLVE unable to resolve dependency tree"],
     ["ERESOLVE", "unable to resolve dependency tree"]),
    ("pip",
     "Collecting foo==1.2.3\n"
     "ERROR: Could not find a version that satisfies the requirement foo==1.2.3 (from versions: none)\n"
     "ERROR: No matching distribution found for foo==1.2.3\n",
     ["ERROR: Could not find a version that satisfies the requirement foo==1.2.3 (from versions: none)",
      "ERROR: No matching distribution found for foo==1.2.3"],
     ["foo", "no matching distribution found"]),
    ("permission-denied", "cp: cannot create regular file '/srv/app/x': Permission denied\n",
     ["cp: cannot create regular file: Permission denied"], ["permission denied"]),
    ("connection-refused",
     "ssh: connect to host vmhost port 22: Connection refused\r\n",
     ["ssh: connect to host vmhost port 22: Connection refused"], ["connection refused"]),
    ("crlf-ansi-noise",
     "\x1b[32m> build\x1b[0m\r\n\x1b[1m\x1b[31merror\x1b[39m TS2345:\x1b[0m Argument of type "
     "'string' is not assignable\r\n\r\n",
     ["error TS2345: Argument of type 'string' is not assignable"], ["TS2345", "string"]),
    ("no-output", "Exit code 1\n", [], []),
    ("only-progress", "Downloading...\n50%\n100%\n", [], []),
]


@pytest.mark.parametrize("name, text, lines, keywords", ERROR_CASES, ids=[c[0] for c in ERROR_CASES])
def test_error_lines_and_keywords(hook, name, text, lines, keywords):
    assert hook.error_lines(text) == lines
    assert hook.error_keywords(lines) == keywords


def test_error_lines_are_bounded(hook):
    text = "\n".join(f"ValueError: bad value {i} {'x' * 400}" for i in range(5000))
    lines = hook.error_lines(text)
    assert len(lines) == hook.MAX_ERROR_LINES
    assert all(len(line) <= hook.MAX_ERROR_LINE_CHARS for line in lines)


def test_error_query_and_signature(hook):
    lines = ["ModuleNotFoundError: No module named 'yaml'"]
    query = hook.error_query("cd /srv/app && sudo python3 main.py | tail -n 5", lines, ["vmhost"])
    assert query == "python3 ModuleNotFoundError: No module named yaml vmhost"
    long = hook.error_query("make", ["ValueError: " + "word " * 200], [])
    assert len(long) <= hook.MAX_ERROR_QUERY_CHARS
    # Volatile detail is gone before hashing: the same error elsewhere is the same signature.
    a = hook.error_lines("Sep 28 10:00:01 h1 app[12]: KeyError: 'token' at /srv/a.py:12\n")
    b = hook.error_lines("Sep 29 11:30:07 h2 app[99]: KeyError: 'token' at /opt/b.py:40\n")
    assert hook.error_signature("python", a) == hook.error_signature("python", b)
    assert hook.error_signature("python", a) != hook.error_signature("node", a)
    assert hook.failed_command_name("ls; echo hi") == "ls"
    assert hook.failed_command_name("cd /srv && echo hi | tail") == ""


def _bash_event(response=None, command="docker restart grafana", session="err-1", event_name="PostToolUse",
                **extra):
    event = {"session_id": session, "hook_event_name": event_name, "tool_name": "Bash",
             "tool_input": {"command": command}, "cwd": "/tmp"}
    if response is not None:
        event["tool_response"] = response
    event.update(extra)
    return event


DOCKER_ERR = "Error response from daemon: No such container: grafana"


@pytest.mark.parametrize("event, failed", [
    (_bash_event(event_name="PostToolUseFailure", error="Exit code 1\n" + DOCKER_ERR), True),
    (_bash_event(event_name="PostToolUseFailure", error="Exit code 1\n" + DOCKER_ERR,
                 is_interrupt=True), False),
    (_bash_event(event_name="PostToolUseFailure"), False),
    (_bash_event(event_name="PostToolUseFailure", error=""), False),
    (_bash_event({"stdout": "", "stderr": DOCKER_ERR, "interrupted": False, "exit_code": 1}), True),
    (_bash_event({"stdout": "", "stderr": DOCKER_ERR, "exitCode": "125"}), True),
    (_bash_event({"stdout": "", "stderr": "Exit code 125\n" + DOCKER_ERR}), True),
    (_bash_event({"stdout": "", "stderr": DOCKER_ERR + "\nexit status 1"}), True),
    (_bash_event("Exit code 2\n" + DOCKER_ERR), True),
    (_bash_event({"stdout": "", "stderr": DOCKER_ERR, "interrupted": False}), False),
    (_bash_event({"stdout": "", "stderr": DOCKER_ERR, "exit_code": 0}), False),
    (_bash_event({"stdout": "", "stderr": DOCKER_ERR, "exit_code": True}), False),
    (_bash_event({"stdout": "", "stderr": "Exit code 0"}), False),
    (_bash_event({"stdout": "", "stderr": DOCKER_ERR, "exit_code": 1, "interrupted": True}), False),
    (_bash_event({"stdout": "grep found: exit status 1 in the log", "stderr": ""}), False),
    (_bash_event(), False),
    (_bash_event(["not", "a", "dict"]), False),
    ({**_bash_event({"stderr": DOCKER_ERR, "exit_code": 1}), "tool_name": "Edit"}, False),
    ({"hook_event_name": "PostToolUseFailure", "tool_name": "Read", "error": "EACCES"}, False),
    ("not a dict", False),
])
def test_failure_detection(hook, event, failed):
    assert (hook.bash_failure(event) is not None) is failed


def _error_note(slug="grafana-missing", title=None, tags=None, body=None):
    return {"slug": slug, "title": title or slug, "matched": True, "host": "any", "tags": tags or [],
            "body": body or "No such container: grafana happens after a compose down; run the stack's up."}


def test_error_recall_output_shape(hook):
    fake = FakeRecall([_error_note()], recall_id="r-err")
    out = hook.handle(_bash_event(event_name="PostToolUseFailure", error="Exit code 1\n" + DOCKER_ERR), fake)
    assert set(out) == {"hookSpecificOutput"}
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    text = out["hookSpecificOutput"]["additionalContext"]
    assert text.startswith("## memd: seen this error before?\n\nError: Error response from daemon")
    assert "### grafana-missing" in text and 'recall_id="r-err"' in text
    assert len(text) <= hook.DEFAULT_MAX_CHARS
    assert fake.calls == [{"query": "docker Error response from daemon: No such container: grafana grafana",
                           "host": None, "k": hook.DEFAULT_TOP_N + 2, "timeout": 1.5}]
    # The same shape from a PostToolUse response with an exit code.
    out = hook.handle(_bash_event({"stdout": "", "stderr": DOCKER_ERR, "exit_code": 1}, session="e2"),
                      FakeRecall([_error_note()]))
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "seen this error before?" in out["hookSpecificOutput"]["additionalContext"]


def test_error_recall_relevance_bar(hook):
    unrelated = {"slug": "gpu", "matched": True, "host": "gpuhost", "body": "GPU tuning."}
    fake = FakeRecall([unrelated])
    assert hook.handle(_bash_event(event_name="PostToolUseFailure", error=DOCKER_ERR,
                                   command="make build"), fake) is None
    assert len(fake.calls) == 1


def test_error_recall_prefers_troubleshooting_notes(hook, monkeypatch):
    monkeypatch.setenv("MEMD_ACTIVITY_TOP_N", "2")
    notes = [_error_note("grafana-dashboards", body="grafana dashboards: No such container at first boot."),
             _error_note("grafana-alerts", body="grafana alert rules; No such container is harmless."),
             _error_note("grafana-fix", tags=["troubleshooting"]),
             _error_note("stack-notes", title="Incident: grafana gone")]
    out = hook.handle(_bash_event(event_name="PostToolUseFailure", error=DOCKER_ERR), FakeRecall(notes))
    text = out["hookSpecificOutput"]["additionalContext"]
    assert text.index("### grafana-fix") < text.index("### stack-notes")
    assert "### grafana-dashboards" not in text and "### grafana-alerts" not in text
    assert hook.prefer_troubleshooting(notes[:2], 2) == notes[:2]      # fits: order untouched
    assert hook.troubleshooting({"slug": "runbook-caddy"})
    assert not hook.troubleshooting({"slug": "prefix-notes", "tags": ["fixture"]})


def test_same_error_injects_once_per_session(hook):
    fake = FakeRecall([_error_note()])
    first = _bash_event(event_name="PostToolUseFailure", error="Exit code 1\n" + DOCKER_ERR)
    assert "seen this error" in hook.handle(first, fake)["hookSpecificOutput"]["additionalContext"]
    # The same error again (different volatile detail) does not recall as an error;
    # the call's target was never claimed, so the target recall runs instead.
    again = _bash_event(event_name="PostToolUseFailure",
                        error="\x1b[31mError response from daemon: No such container: grafana\x1b[0m\r\n")
    out = hook.handle(again, fake)
    assert len(fake.calls) == 2 and fake.calls[1]["query"] == "grafana"
    assert out is None                                           # the note was already shown
    assert hook.handle(again, fake) is None and len(fake.calls) == 2
    # Another session sees it afresh.
    assert hook.handle({**first, "session_id": "other"}, fake)


def test_error_recalls_count_against_the_cap(hook, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMD_ACTIVITY_MAX_RECALLS", "2")
    d = str(tmp_path / "state")
    fake = FakeRecall([])
    for i in range(4):
        hook.handle(_bash_event(event_name="PostToolUseFailure", command="make",
                                error=f"ValueError: bad input number{chr(97 + i)}"), fake,
                    now=1_000_000.0 + i, directory=d)
    assert len(fake.calls) == 2
    hook.handle(_event("ssh gpuhost", session="err-1"), fake, now=1_000_010.0, directory=d)
    assert len(fake.calls) == 2


def test_errors_switch_turns_off_only_error_recall(hook, monkeypatch):
    monkeypatch.setenv("MEMD_ACTIVITY_ERRORS", "0")
    fake = FakeRecall([_error_note()])
    out = hook.handle(_bash_event({"stdout": "", "stderr": DOCKER_ERR, "exit_code": 1}), fake)
    assert fake.calls[0]["query"] == "grafana"                  # plain target recall
    assert "memd: memory for grafana" in out["hookSpecificOutput"]["additionalContext"]
    assert hook.handle(_bash_event(event_name="PostToolUseFailure", error=DOCKER_ERR, command="make",
                                   session="e3"), fake) is None
    assert len(fake.calls) == 1


def test_errors_switch_from_env_file(hook, monkeypatch, tmp_path):
    env_file = tmp_path / "client.env"
    env_file.write_text("export MEMD_ACTIVITY_ERRORS=off\n")
    monkeypatch.setenv("MEMD_ENV_FILE", str(env_file))
    assert hook.errors_disabled()
    monkeypatch.setenv("MEMD_ENV_FILE", str(tmp_path / "absent.env"))
    assert not hook.errors_disabled()


def test_hook_off_turns_off_error_recall_too(hook, monkeypatch):
    monkeypatch.setenv("MEMD_ACTIVITY_HOOK", "0")
    fake = FakeRecall([_error_note()])
    assert hook.handle(_bash_event(event_name="PostToolUseFailure", error=DOCKER_ERR), fake) is None
    assert fake.calls == []


def test_run_handles_a_failure_event(hook):
    out = io.StringIO()
    event = _bash_event(event_name="PostToolUseFailure", error="Exit code 1\n" + DOCKER_ERR)
    assert hook.run(io.StringIO(json.dumps(event)), out, FakeRecall([_error_note()])) == 0
    data = json.loads(out.getvalue())
    assert data["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"


def test_onboarding_registers_the_failure_hook(tmp_path):
    run, home = _onboard(tmp_path, "--activity-hook", "mem_amber_test-token", "https://memd.example.com")
    assert run.returncode == 0, run.stdout + run.stderr
    settings = json.loads((home / ".claude/settings.json").read_text())
    [group] = settings["hooks"]["PostToolUseFailure"]
    assert group["matcher"] == "Bash"
    assert group["hooks"][0]["command"].endswith("/.claude/hooks/memd-activity-hook")
    run, home = _onboard(tmp_path, "--activity-hook", "mem_amber_test-token", "https://memd.example.com")
    assert len(json.loads((home / ".claude/settings.json").read_text())["hooks"]["PostToolUseFailure"]) == 1


# ---- credentials never reach the error query ----

SECRET_SAMPLES = [
    "-----BEGIN OPENSSH PRIVATE KEY-----b3BlbnNzaC1rZXktdjEAAAA",
    "ghp_" + "A1" * 18,
    "github_pat_" + "B2" * 15,
    "sk-proj-" + "c3" * 10,
    "xoxb-1234-1234-1234-abcdef",
    "AKIA" + "ABCDEFGHIJKLMNOP",            # split so secret scanners skip the fixture
    "glpat-" + "x9" * 11,
    "hf_" + "Q7" * 12,
    "tskey-auth-" + "k8" * 8,
    "mem_" + "Zq3-" * 8,
    "memd_tok1." + "Vw4_" * 8,
    ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0In0", "c2lnbmF0dXJlLXZhbHVl"]),
    "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5",
]


@pytest.mark.parametrize("secret", SECRET_SAMPLES)
def test_scrub_secrets_removes_token_shapes(hook, secret):
    assert secret not in hook.scrub_secrets(f"error: rejected {secret} by server")


@pytest.mark.parametrize("text, secret", [
    ("password=hunter2 rejected", "hunter2"),
    ('DB_PASSWORD: "S3cret!" is wrong', "S3cret!"),
    ("export API_KEY=Zx9fQ2mL1k", "Zx9fQ2mL1k"),
    ("Authorization: Bearer abcDEF123456789xyz", "abcDEF123456789xyz"),
    ("auth failed for Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
])
def test_scrub_secrets_removes_credential_values(hook, text, secret):
    assert secret not in hook.scrub_secrets(text)


@pytest.mark.parametrize("text", [
    "token: invalid",
    "auth: failed",
    "KeyError: 'token'",
    "psql: error: FATAL: password authentication failed for user alice",
    "basic authentication required",
    "Invalid token provided",
])
def test_scrub_secrets_keeps_plain_error_text(hook, text):
    assert hook.scrub_secrets(text) == text


def test_error_lines_never_carry_credentials(hook):
    lines = hook.error_lines("Error: 401 Unauthorized, password=hunter2 rejected by db\n")
    assert lines and not any("hunter2" in line for line in lines)


# ---- onboarding keeps the token out of argv, history and clear text ----

def _onboard_logged(tmp_path, *args, extra_env=None):
    """``_onboard`` with a curl that also logs its argv and any -K config it reads."""
    log = tmp_path / "curl.log"
    run, home = _onboard(tmp_path, *args, extra_env={"CURL_LOG": str(log), **(extra_env or {})})
    return run, home, (log.read_text() if log.exists() else "")


def test_onboarding_keeps_the_token_out_of_curl_argv(tmp_path):
    run, _, log = _onboard_logged(tmp_path, "mem_amber_test-token", "https://memd.example.com")
    assert run.returncode == 0, run.stdout + run.stderr
    argv_lines = [line for line in log.splitlines() if line.startswith("ARGV:")]
    assert argv_lines and not any("mem_amber_test-token" in line for line in argv_lines)
    assert 'header = "Authorization: Bearer mem_amber_test-token"' in log


def test_onboarding_warns_about_a_token_on_the_command_line(tmp_path):
    run, _, _ = _onboard_logged(tmp_path, "mem_amber_test-token", "https://memd.example.com")
    assert "WARNING: a token on the command line" in run.stderr


def test_onboarding_takes_the_token_from_memd_token(tmp_path):
    run, home, log = _onboard_logged(tmp_path, "https://memd.example.com",
                                     extra_env={"MEMD_TOKEN": "mem_amber_env-token"})
    assert run.returncode == 0, run.stdout + run.stderr
    assert "WARNING" not in run.stderr
    assert "Bearer mem_amber_env-token" in log


def test_onboarding_takes_the_token_from_a_file(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("mem_amber_file-token\r\n")
    run, _, log = _onboard_logged(tmp_path, "--token-file", str(token_file), "https://memd.example.com")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "Bearer mem_amber_file-token\"" in log


@pytest.mark.parametrize("server, allowed", [
    ("http://memd.example.com", False),
    ("http://user@memd.example.com:8077/", False),
    ("http://127.0.0.1:8077", True),
    ("http://localhost:8077", True),
    ("http://[::1]:8077", True),
])
def test_onboarding_refuses_plain_http_off_this_machine(tmp_path, server, allowed):
    run, _, log = _onboard_logged(tmp_path, "mem_amber_test-token", server)
    if allowed:
        assert run.returncode == 0, run.stdout + run.stderr
    else:
        assert run.returncode == 1
        assert "refusing to send the token over plain HTTP" in run.stderr
        assert "Bearer" not in log


def test_onboarding_allows_plain_http_when_told_to(tmp_path):
    run, _, _ = _onboard_logged(tmp_path, "mem_amber_test-token", "http://memd.example.com",
                                extra_env={"MEMD_ALLOW_INSECURE_HTTP": "1"})
    assert run.returncode == 0, run.stdout + run.stderr
