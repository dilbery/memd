"""MEMD_RECALL_STORES: multi-store recall in the prompt hook, pi and onboarding.

The setting is opt-in, so the first thing pinned here is that a client without
it sends the request it always sent, field for field. With it, the client names
stores (or `scope: "all"`), drops `profile`, and falls back to its own store
with a one-line notice when the server refuses.

Three ways of driving the clients, following the neighbouring tests:

* the hook imported as a module with `urlopen` replaced (as tests/test_render.py
  does), for request shapes and for the retry clock;
* the hook run as the real script against a stub server on 127.0.0.1, for what
  goes on the wire and what Claude Code reads back. The request is made by the
  child process; the test process only listens;
* `clients/pi-memd.ts` under Node with `fetch` replaced, and `onboard.sh` with
  stand-in `curl`/`claude`/`codex` binaries (as tests/test_activity_hook.py does).
"""

import http.server
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import types
import urllib.error

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "clients" / "memd-recall-hook"
PROMPT = "where is the backup window documented"
RID = "0123456789abcdef"
CONTEXT = "## Recalled memory (memd)\n\n### backup-window\nruns weekly"
FEDERATED_CONTEXT = ("## Recalled memory (memd)\n\n### [team] backup-window\nruns weekly\n\n"
                     "[Not searched: cobalt (deadline).]")

# The request the hook sent before the setting existed.
TODAY = {"query": PROMPT, "profile": "amber", "k": 8, "max_chars": 14000, "format": "context"}
# What every multi-store request shares; each test adds `scope` or `stores`.
MULTI = {"query": PROMPT, "k": 8, "max_chars": 14000, "format": "context", "deadline_ms": 3200}
EMPTY = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ""}}

SINGLE_FOOTER = f'\n\n[To open a note from this recall: read(slug, recall_id="{RID}").]'
FEDERATED_FOOTER = (f'\n\n[To open a note from this recall: read(slug, recall_id="{RID}"); '
                    'for a note marked [store], add store="<that store>".]')


def notice(status, shown="amber"):
    return (f"[memd: multi-store recall was refused (HTTP {status}); showing {shown} only. "
            "Check MEMD_RECALL_STORES and the token's additional stores.]\n\n")


# ---- the hook as a module: request shapes and the retry clock ----

def load_hook(monkeypatch, tmp_path, **env):
    """Import the hook afresh: it reads its settings once, at import."""
    monkeypatch.setenv("HOME", str(tmp_path))       # never the developer's client.env
    monkeypatch.setenv("MEMD_PROFILE", "amber")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    loader = importlib.machinery.SourceFileLoader("memd_recall_hook_stores", str(HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Transport:
    """Stands in for urlopen and the hook's clock.

    Replays `replies` in order (an int is that HTTP error status, an exception is
    raised); each request takes `seconds` on the clock the hook measures its
    timeout with.
    """

    def __init__(self, hook, monkeypatch, *replies, seconds=0.0):
        self.calls, self.replies, self.now, self.seconds = [], list(replies), 100.0, seconds
        monkeypatch.setattr(hook.urllib.request, "urlopen", self.urlopen)
        monkeypatch.setattr(hook, "time", types.SimpleNamespace(monotonic=lambda: self.now))

    def urlopen(self, request, timeout=None):
        self.calls.append({"body": json.loads(request.data), "timeout": timeout})
        self.now += self.seconds
        reply = self.replies.pop(0)
        if isinstance(reply, int):
            raise urllib.error.HTTPError(request.full_url, reply, "refused", {}, None)
        if isinstance(reply, BaseException):
            raise reply
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps({"context": CONTEXT, "recall_id": RID}).encode()

    @property
    def bodies(self):
        return [call["body"] for call in self.calls]


@pytest.mark.parametrize("value", [None, "", "   ", ",", " , ,"])
def test_unset_or_blank_sends_todays_request(monkeypatch, tmp_path, value):
    env = {} if value is None else {"MEMD_RECALL_STORES": value}
    hook = load_hook(monkeypatch, tmp_path, **env)
    transport = Transport(hook, monkeypatch, "ok")
    assert hook.fetch_recall(PROMPT)["context"] == CONTEXT
    assert transport.calls == [{"body": TODAY, "timeout": 4.0}]


@pytest.mark.parametrize("value", ["all", "ALL", " All "])
def test_all_sends_scope_and_no_profile(monkeypatch, tmp_path, value):
    hook = load_hook(monkeypatch, tmp_path, MEMD_RECALL_STORES=value)
    transport = Transport(hook, monkeypatch, "ok")
    hook.fetch_recall(PROMPT)
    assert transport.calls == [{"body": {**MULTI, "scope": "all"}, "timeout": 4.0}]


def test_list_is_trimmed_deduplicated_and_kept_in_order(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path, MEMD_RECALL_STORES=" team , amber,,team, ,cobalt,amber ")
    transport = Transport(hook, monkeypatch, "ok")
    hook.fetch_recall(PROMPT)
    assert transport.bodies == [{**MULTI, "stores": ["team", "amber", "cobalt"]}]


@pytest.mark.parametrize("timeout, expected", [
    (None, 3200), ("10", 9200), ("2.5", 1700), ("1.3", 500), ("0.5", 500)])
def test_deadline_is_the_timeout_less_a_margin(monkeypatch, tmp_path, timeout, expected):
    env = {"MEMD_RECALL_STORES": "all", **({} if timeout is None else {"MEMD_TIMEOUT": timeout})}
    hook = load_hook(monkeypatch, tmp_path, **env)
    transport = Transport(hook, monkeypatch, "ok")
    hook.fetch_recall(PROMPT)
    deadline = transport.bodies[0]["deadline_ms"]
    assert deadline == expected and type(deadline) is int


def test_single_store_request_carries_no_deadline(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path, MEMD_TIMEOUT="10")
    transport = Transport(hook, monkeypatch, "ok")
    hook.fetch_recall(PROMPT)
    assert transport.bodies == [TODAY]


@pytest.mark.parametrize("status", [403, 400])
def test_refusal_retries_once_as_todays_request_with_a_notice(monkeypatch, tmp_path, status):
    hook = load_hook(monkeypatch, tmp_path, MEMD_RECALL_STORES="amber,team")
    transport = Transport(hook, monkeypatch, status, "ok", seconds=1.5)
    data = hook.fetch_recall(PROMPT)
    assert transport.bodies == [{**MULTI, "stores": ["amber", "team"]}, TODAY]
    # The retry gets what is left of the one timeout, not a fresh one.
    assert transport.calls[1]["timeout"] == pytest.approx(2.5)
    assert data["context"] == notice(status) + CONTEXT


def test_retry_runs_with_exactly_a_second_left(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path, MEMD_RECALL_STORES="all")
    transport = Transport(hook, monkeypatch, 403, "ok", seconds=3.0)
    assert hook.fetch_recall(PROMPT)["context"].startswith(notice(403))
    assert transport.calls[1]["timeout"] == pytest.approx(1.0)


def test_no_retry_with_less_than_a_second_left(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path, MEMD_RECALL_STORES="all")
    transport = Transport(hook, monkeypatch, 403, "ok", seconds=3.1)
    assert hook.fetch_recall(PROMPT) == []
    assert len(transport.calls) == 1


def test_a_refused_retry_is_not_retried_again(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path, MEMD_RECALL_STORES="all")
    transport = Transport(hook, monkeypatch, 403, 403, "ok")
    assert hook.fetch_recall(PROMPT) == []
    assert len(transport.calls) == 2


@pytest.mark.parametrize("status", [401, 404, 429, 500, 503])
def test_other_statuses_are_not_retried(monkeypatch, tmp_path, status):
    hook = load_hook(monkeypatch, tmp_path, MEMD_RECALL_STORES="all")
    transport = Transport(hook, monkeypatch, status, "ok")
    assert hook.fetch_recall(PROMPT) == []
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [403, 400])
def test_single_store_refusal_is_not_retried(monkeypatch, tmp_path, status):
    hook = load_hook(monkeypatch, tmp_path)
    transport = Transport(hook, monkeypatch, status, "ok")
    assert hook.fetch_recall(PROMPT) == []
    assert len(transport.calls) == 1


def test_notice_is_not_shown_alone(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path, MEMD_RECALL_STORES="all")
    transport = Transport(hook, monkeypatch, 403, "ok")
    transport.read = lambda: b'{"context": ""}'
    assert hook.fetch_recall(PROMPT)["context"] == ""


@pytest.mark.parametrize("failure", [
    TimeoutError("timed out"), urllib.error.URLError("unreachable"), ConnectionResetError(),
    ValueError("unknown url type")])
def test_a_retry_that_fails_gives_no_context(monkeypatch, tmp_path, failure):
    hook = load_hook(monkeypatch, tmp_path, MEMD_RECALL_STORES="all")
    transport = Transport(hook, monkeypatch, 403, failure)
    assert hook.fetch_recall(PROMPT) == []
    assert len(transport.calls) == 2


UNUSABLE = [b"not json", b"", b"[1, 2]", b'"text"', b"null", b"\xff\xfe"]


@pytest.mark.parametrize("raw", UNUSABLE)
@pytest.mark.parametrize("replies", [("ok",), (403, "ok")])
def test_an_unusable_reply_gives_no_context(monkeypatch, tmp_path, raw, replies):
    hook = load_hook(monkeypatch, tmp_path, MEMD_RECALL_STORES="amber,team")
    transport = Transport(hook, monkeypatch, *replies)
    transport.read = lambda: raw
    assert hook.fetch_recall(PROMPT) == []
    assert len(transport.calls) == len(replies)


def test_notes_only_reply_to_a_retry_is_still_rendered(monkeypatch, tmp_path, capsys):
    # An older server answers the retry with notes and no context: the hook
    # renders them itself, as it always has.
    hook = load_hook(monkeypatch, tmp_path, MEMD_RECALL_STORES="all")
    transport = Transport(hook, monkeypatch, 403, "ok")
    transport.read = lambda: json.dumps(
        {"notes": [{"slug": "backup-window", "importance": 2, "body": "runs weekly"}]}).encode()
    monkeypatch.setattr(hook.sys, "stdin", types.SimpleNamespace(read=lambda: json.dumps({"prompt": PROMPT})))
    assert hook.main() == 0
    shown = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "### backup-window\nruns weekly" in shown


@pytest.fixture
def hook(monkeypatch, tmp_path):
    return load_hook(monkeypatch, tmp_path)


def test_federated_reply_gets_the_store_read_hint(hook):
    assert (hook.with_recall_id(FEDERATED_CONTEXT, RID, max_chars=1000, stores=["amber", "team"])
            == FEDERATED_CONTEXT + FEDERATED_FOOTER)


@pytest.mark.parametrize("stores", [None, [], "amber,team", {"team": "read"}])
def test_single_store_footer_is_unchanged(hook, stores):
    assert hook.with_recall_id(CONTEXT, RID, max_chars=1000) == CONTEXT + SINGLE_FOOTER
    assert hook.with_recall_id(CONTEXT, RID, max_chars=1000, stores=stores) == CONTEXT + SINGLE_FOOTER


def test_federated_footer_keeps_the_budget_rule(hook):
    fits = "x" * (1000 - len(FEDERATED_FOOTER))
    assert hook.with_recall_id(fits, RID, max_chars=1000, stores=["team"]) == fits + FEDERATED_FOOTER
    # One character over: no footer at all, as for the single-store footer.
    assert hook.with_recall_id(fits + "x", RID, max_chars=1000, stores=["team"]) == fits + "x"
    assert hook.with_recall_id("", RID, max_chars=1000, stores=["team"]) == ""
    assert hook.with_recall_id(CONTEXT, "not-an-id", max_chars=1000, stores=["team"]) == CONTEXT


# ---- the hook as the real script, against a stub server ----

class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind would resolve the host name; keep to the bind.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        pass    # a client that gave up on a delayed reply is the point of the test


class Stub:
    """memd's /recall on an ephemeral 127.0.0.1 port.

    Replays `(status, body)` or `(status, body, delay_seconds)` replies in order
    and records each request. A `bytes` body is sent as it is.
    """

    def __init__(self, replies):
        self.requests = []
        self.closing = threading.Event()
        replies = list(replies)
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append({"path": self.path, "body": body,
                                      "auth": self.headers.get("Authorization")})
                status, reply, *delay = replies.pop(0) if replies else (500, {})
                if delay:
                    stub.closing.wait(delay[0])
                data = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def bodies(self):
        return [request["body"] for request in self.requests]

    def close(self):
        self.closing.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def stub():
    started = []

    def start(*replies):
        started.append(Stub(replies))
        return started[-1]

    yield start
    for server in started:
        server.close()


def run_hook_process(url, tmp_path, **env):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "MEMD_REMOTE": url,
           "MEMD_TOKEN": "test-token", "MEMD_PROFILE": "amber", **env}
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"prompt": PROMPT}),
                          capture_output=True, text=True, env=env, timeout=30)


def run_hook(url, tmp_path, **env):
    """Run the hook as Claude Code does; it must always exit 0 with one JSON object."""
    proc = run_hook_process(url, tmp_path, **env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def injected(output):
    assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    return output["hookSpecificOutput"]["additionalContext"]


def test_script_unset_puts_todays_request_on_the_wire(stub, tmp_path):
    server = stub((200, {"ok": True, "profile": "amber", "context": CONTEXT, "recall_id": RID}))
    output = run_hook(server.url, tmp_path)
    assert server.requests == [{"path": "/recall", "auth": "Bearer test-token", "body": TODAY}]
    assert injected(output) == CONTEXT + SINGLE_FOOTER


def test_script_prints_a_federated_reply_with_the_store_hint(stub, tmp_path):
    server = stub((200, {"ok": True, "profile": "amber", "stores": ["amber", "team"],
                         "stores_skipped": [{"store": "cobalt", "reason": "deadline"}],
                         "context": FEDERATED_CONTEXT, "recall_id": RID}))
    output = run_hook(server.url, tmp_path, MEMD_RECALL_STORES="all")
    assert server.bodies == [{**MULTI, "scope": "all"}]
    assert injected(output) == FEDERATED_CONTEXT + FEDERATED_FOOTER


@pytest.mark.parametrize("status", [403, 400])
def test_script_falls_back_to_its_own_store_when_refused(stub, tmp_path, status):
    server = stub((status, {"detail": "stores[1] is not a store this caller may use"}),
                  (200, {"ok": True, "profile": "amber", "context": CONTEXT, "recall_id": RID}))
    output = run_hook(server.url, tmp_path, MEMD_RECALL_STORES="amber, team")
    assert server.bodies == [{**MULTI, "stores": ["amber", "team"]}, TODAY]
    assert injected(output) == notice(status) + CONTEXT + SINGLE_FOOTER


def test_script_does_not_retry_with_too_little_time_left(stub, tmp_path):
    server = stub((403, {"detail": "refused"}, 0.8), (200, {"context": CONTEXT}))
    output = run_hook(server.url, tmp_path, MEMD_RECALL_STORES="all", MEMD_TIMEOUT="1.6")
    assert len(server.requests) == 1
    assert output == EMPTY


@pytest.mark.parametrize("stores", [None, "all"])
def test_script_server_error_gives_empty_context(stub, tmp_path, stores):
    server = stub((500, {"detail": "boom"}), (200, {"context": CONTEXT}))
    env = {} if stores is None else {"MEMD_RECALL_STORES": stores}
    assert run_hook(server.url, tmp_path, **env) == EMPTY
    assert len(server.requests) == 1


def test_script_failed_retry_gives_empty_context(stub, tmp_path):
    server = stub((403, {"detail": "refused"}), (500, {"detail": "boom"}), (200, {"context": CONTEXT}))
    assert run_hook(server.url, tmp_path, MEMD_RECALL_STORES="all") == EMPTY
    assert len(server.requests) == 2


@pytest.mark.parametrize("reply", [b"not json", b"[1, 2]", b"\xff\xfe"])
@pytest.mark.parametrize("refused_first", [False, True])
def test_script_unusable_reply_gives_empty_context(stub, tmp_path, reply, refused_first):
    server = stub(*([(403, {"detail": "refused"})] if refused_first else []), (200, reply))
    assert run_hook(server.url, tmp_path, MEMD_RECALL_STORES="amber,team") == EMPTY
    assert len(server.requests) == 1 + refused_first


def test_script_gives_up_on_a_retry_that_does_not_answer_in_time(stub, tmp_path):
    server = stub((403, {"detail": "refused"}), (200, {"context": CONTEXT}, 20))
    began = time.monotonic()
    output = run_hook(server.url, tmp_path, MEMD_RECALL_STORES="all", MEMD_TIMEOUT="1.5")
    assert output == EMPTY and len(server.requests) == 2
    # Long before the stub would have answered: the retry ran inside the 1.5 s.
    assert time.monotonic() - began < 10


@pytest.mark.parametrize("replies", [
    [(403, {"detail": "refused"}), (200, {"context": CONTEXT, "recall_id": RID})],
    [(403, {"detail": "refused"}), (500, {"detail": "boom"})],
    [(403, {"detail": "refused"}), (403, {"detail": "refused"})],
    [(400, {"detail": "refused"}, 0.7)],
])
def test_script_fallback_names_neither_the_token_nor_the_server(stub, tmp_path, replies):
    server = stub(*replies)
    proc = run_hook_process(server.url, tmp_path, MEMD_RECALL_STORES="amber,team",
                            MEMD_TIMEOUT="1.5")
    assert proc.returncode == 0
    said = proc.stdout + proc.stderr
    assert "test-token" not in said and server.url not in said and "127.0.0.1" not in said


@pytest.mark.parametrize("stores", [None, "amber,team"])
def test_script_connection_refused_gives_empty_context(tmp_path, stores):
    with socket.socket() as probe:              # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {} if stores is None else {"MEMD_RECALL_STORES": stores}
    assert run_hook(f"http://127.0.0.1:{port}", tmp_path, **env) == EMPTY


# ---- the pi extension: the same setting, shapes, fallback and notice ----

PI_PROBE = '''
const {default: extension} = await import(process.env.PI_EXTENSION);
let recall;
extension({on(name, fn) {if (name === "before_agent_start") recall = fn;}, registerTool() {}});
const replies = JSON.parse(process.env.REPLIES), requests = [], timeouts = [];
let now = 0;
performance.now = () => now;
const realTimeout = AbortSignal.timeout.bind(AbortSignal);
AbortSignal.timeout = (ms) => {timeouts.push(ms); return realTimeout(ms);};
globalThis.fetch = async (url, request) => {
    requests.push(JSON.parse(request.body));
    now += Number(process.env.ELAPSED_MS);
    const [status, body] = replies.shift();
    if (status === "abort") throw new DOMException("The operation was aborted due to timeout", "TimeoutError");
    return {ok: status >= 200 && status < 300, status,
            json: async () => body === "unparseable" ? JSON.parse("not json") : body};
};
const result = await recall({prompt: process.env.PROMPT});
console.log(JSON.stringify({requests, timeouts, content: result?.message?.content ?? null}));
'''
PI_TODAY = {"query": PROMPT, "profile": "amber", "k": 8, "format": "context", "max_chars": 14000}
PI_MULTI = {"query": PROMPT, "k": 8, "format": "context", "max_chars": 14000, "deadline_ms": 4200}
PI_OK = (200, {"ok": True, "profile": "amber", "context": CONTEXT})


def run_pi(tmp_path, replies, stores=None, profile="amber", elapsed_ms=0):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the pi extension")
    major = int(subprocess.check_output([node, "--version"], text=True).lstrip("v").split(".")[0])
    if major < 22:
        pytest.skip("Node 22+ is required to execute TypeScript without dependencies")
    script = tmp_path / "probe.mjs"
    script.write_text(PI_PROBE)
    env = dict(os.environ, MEMD_TOKEN="test-token", PROMPT=PROMPT, REPLIES=json.dumps(replies),
               ELAPSED_MS=str(elapsed_ms), PI_EXTENSION=(ROOT / "clients/pi-memd.ts").as_uri())
    for key, value in (("MEMD_RECALL_STORES", stores), ("MEMD_PROFILE", profile)):
        env.pop(key, None)
        if value is not None:
            env[key] = value
    run = subprocess.run([node, "--experimental-strip-types", str(script)], env=env,
                         capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.parametrize("stores", [None, "", "  ", ","])
def test_pi_unset_or_blank_sends_todays_request(tmp_path, stores):
    result = run_pi(tmp_path, [PI_OK], stores=stores)
    assert result == {"requests": [PI_TODAY], "timeouts": [5000], "content": CONTEXT}


@pytest.mark.parametrize("stores, fields", [
    ("all", {"scope": "all"}),
    (" ALL ", {"scope": "all"}),
    (" team , amber,,team, ,cobalt,amber ", {"stores": ["team", "amber", "cobalt"]}),
])
def test_pi_multi_store_request_has_no_profile_and_a_deadline(tmp_path, stores, fields):
    result = run_pi(tmp_path, [PI_OK], stores=stores)
    assert result == {"requests": [{**PI_MULTI, **fields}], "timeouts": [5000], "content": CONTEXT}


@pytest.mark.parametrize("status", [403, 400])
def test_pi_refusal_retries_once_as_todays_request_with_a_notice(tmp_path, status):
    result = run_pi(tmp_path, [(status, {"detail": "refused"}), PI_OK], stores="amber,team",
                    elapsed_ms=1500)
    assert result["requests"] == [{**PI_MULTI, "stores": ["amber", "team"]}, PI_TODAY]
    assert result["timeouts"] == [5000, 3500]
    assert result["content"] == notice(status) + CONTEXT


@pytest.mark.parametrize("reply_profile, shown", [("cobalt", "cobalt"), (None, "your own store")])
def test_pi_notice_names_the_store_shown_without_a_configured_profile(tmp_path, reply_profile, shown):
    reply = {"ok": True, "context": CONTEXT}
    if reply_profile:
        reply["profile"] = reply_profile
    result = run_pi(tmp_path, [(403, {}), (200, reply)], stores="all", profile=None)
    assert "profile" not in result["requests"][1]
    assert result["content"] == notice(403, shown) + CONTEXT


@pytest.mark.parametrize("replies, elapsed_ms, requests", [
    ([(403, {}), PI_OK], 4100, 1),         # under a second of the 5 s left
    ([(403, {}), (403, {}), PI_OK], 0, 2),  # the retry is refused too
    ([(403, {}), (500, {}), PI_OK], 0, 2),
    ([(500, {}), PI_OK], 0, 1),
    ([(401, {}), PI_OK], 0, 1),
    ([(403, {}), ("abort", None), PI_OK], 0, 2),   # the retry ran out of time
    ([(403, {}), (200, "unparseable")], 0, 2),
    ([(403, {}), (200, [1, 2])], 0, 2),
    ([("abort", None), PI_OK], 0, 1),
    ([(200, "unparseable"), PI_OK], 0, 1),
])
def test_pi_other_failures_inject_nothing(tmp_path, replies, elapsed_ms, requests):
    result = run_pi(tmp_path, replies, stores="all", elapsed_ms=elapsed_ms)
    assert result["content"] is None and len(result["requests"]) == requests


def test_pi_single_store_refusal_is_not_retried(tmp_path):
    result = run_pi(tmp_path, [(403, {}), PI_OK])
    assert result["content"] is None and result["requests"] == [PI_TODAY]


# ---- onboarding: --stores=... written for the recall hook and pi only ----

def _executable(path, code):
    path.write_text("#!" + sys.executable + "\n" + code)
    path.chmod(0o700)


def onboard(tmp_path, *args, shell="bash"):
    home = tmp_path / "home"
    for directory in (".claude", ".pi/agent"):
        (home / directory).mkdir(parents=True, exist_ok=True)
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
    # Record how the MCP registrations are invoked: they must not carry the setting.
    record = ('import json, os, sys\n'
              'with open(os.environ["CLI_LOG"], "a") as log:\n'
              '    log.write(json.dumps(sys.argv) + "\\n")\n')
    _executable(bins / "claude", record)
    _executable(bins / "codex", record)
    log = tmp_path / "cli.jsonl"
    # The test servers speak plain HTTP, which onboarding may refuse to send a token over.
    env = dict(os.environ, HOME=str(home), PATH=str(bins) + os.pathsep + os.environ["PATH"],
               CLI_LOG=str(log), MEMD_ALLOW_INSECURE_HTTP="1")
    env.pop("CODEX_HOME", None)
    run = subprocess.run([shell, str(ROOT / "clients/onboard.sh"), *args], env=env,
                         capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
    return run, home, log


def hook_commands(home, name):
    settings = json.loads((home / ".claude/settings.json").read_text())
    return [h["command"] for groups in settings["hooks"].values() for group in groups
            for h in group.get("hooks", []) if h.get("command", "").endswith("/" + name)]


@pytest.mark.parametrize("value", ["all", "amber,team"])
def test_onboarding_writes_the_setting_for_the_recall_hook_and_pi(tmp_path, value):
    run, home, log = onboard(tmp_path, "--activity-hook", "--handoff", f"--stores={value}",
                             "mem_amber_test-token", "http://invalid.test")
    assert run.returncode == 0, run.stdout + run.stderr
    [recall] = hook_commands(home, "memd-recall-hook")
    words = shlex.split(recall)
    assert f"MEMD_RECALL_STORES={value}" in words and "MEMD_PROFILE=amber" in words
    assert words[-1] == str(home / ".claude/hooks/memd-recall-hook")
    # pi reads the env file the user sources before starting it.
    env_file = home / ".config/memd/client.env"
    assert f"export MEMD_RECALL_STORES={value}\n" in env_file.read_text()
    assert env_file.stat().st_mode & 0o777 == 0o600
    # The activity and handoff hooks and the MCP registrations stay single-store.
    others = hook_commands(home, "memd-activity-hook") + hook_commands(home, "memd-handoff-hook")
    assert len(others) == 4 and not any("MEMD_RECALL_STORES" in command for command in others)
    registrations = log.read_text()
    assert "mcp" in registrations and "MEMD_RECALL_STORES" not in registrations


def test_onboarding_without_the_flag_writes_no_setting_and_removes_an_earlier_one(tmp_path):
    run, home, _ = onboard(tmp_path, "mem_amber_test-token", "http://invalid.test", "--stores=all")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "MEMD_RECALL_STORES=all" in hook_commands(home, "memd-recall-hook")[0]
    run, home, _ = onboard(tmp_path, "mem_amber_test-token", "http://invalid.test")
    assert run.returncode == 0, run.stdout + run.stderr
    [recall] = hook_commands(home, "memd-recall-hook")
    assert "MEMD_RECALL_STORES" not in recall
    assert "MEMD_RECALL_STORES" not in (home / ".config/memd/client.env").read_text()


def test_onboarded_hook_command_puts_the_stores_on_the_wire(stub, tmp_path):
    server = stub((200, {"ok": True, "profile": "amber", "stores": ["amber", "team"],
                         "context": FEDERATED_CONTEXT, "recall_id": RID}))
    run, home, _ = onboard(tmp_path, "--stores=amber,team", "mem_amber_test-token", server.url)
    assert run.returncode == 0, run.stdout + run.stderr
    [command] = hook_commands(home, "memd-recall-hook")
    # The stand-in curl installed a placeholder; run the command with the real hook.
    shutil.copy(HOOK, home / ".claude/hooks/memd-recall-hook")
    proc = subprocess.run(["sh", "-c", command], input=json.dumps({"prompt": PROMPT}),
                          capture_output=True, text=True, timeout=30,
                          env={"PATH": os.environ.get("PATH", ""), "HOME": str(home)})
    assert proc.returncode == 0, proc.stderr
    assert server.requests == [{"path": "/recall", "auth": "Bearer mem_amber_test-token",
                                "body": {**MULTI, "stores": ["amber", "team"]}}]
    assert injected(json.loads(proc.stdout)) == FEDERATED_CONTEXT + FEDERATED_FOOTER


def _started_as_sh(tmp_path):
    """Shells to start the script with as `sh onboard.sh`: bash in POSIX mode, and
    any real POSIX shell that is installed (those re-exec bash)."""
    link = tmp_path / "posix" / "sh"
    link.parent.mkdir()
    link.symlink_to(shutil.which("bash"))
    return [str(link)] + [exe for exe in map(shutil.which, ("dash", "posh")) if exe]


def test_onboarding_started_as_sh_takes_the_flag(tmp_path):
    for n, shell in enumerate(_started_as_sh(tmp_path)):
        run, home, _ = onboard(tmp_path / str(n), "--stores=amber,team", "mem_amber_test-token",
                               "http://invalid.test", shell=shell)
        assert run.returncode == 0, shell + "\n" + run.stdout + run.stderr
        assert "MEMD_RECALL_STORES=amber,team" in shlex.split(hook_commands(home, "memd-recall-hook")[0])
        run, home, _ = onboard(tmp_path / (str(n) + "-bad"), "--stores=amber,Team",
                               "mem_amber_test-token", "http://invalid.test", shell=shell)
        assert run.returncode != 0 and "Usage:" in run.stderr, shell
        assert not (home / ".config/memd/client.env").exists()


@pytest.mark.parametrize("flag", [
    "--stores=", "--stores", "--stores=Team", "--stores=1team", "--stores=amber,,team",
    "--stores=amber,team,", "--stores=amber, team", "--stores=amber;team", "--stores=$(id)",
    "--stores=amber_team", "--stores=amber\nteam", "--stores=team\n", "--stores=amber'team",
    "--stores=-team", "--stores=,team",
    # `all` is every store, not a store name, so it cannot sit inside a list.
    "--stores=all,team", "--stores=amber,all", "--stores=amber,all,team",
])
def test_onboarding_rejects_a_malformed_stores_value(tmp_path, flag):
    run, home, log = onboard(tmp_path, flag, "mem_amber_test-token", "http://invalid.test")
    assert run.returncode != 0
    assert "Usage:" in run.stderr and "--stores=<list>" in run.stderr
    assert "MEMD_RECALL_STORES" in run.stderr
    assert "Onboarding complete." not in run.stdout
    # Rejected before anything is written or registered.
    assert not (home / ".config/memd/client.env").exists()
    assert not (home / ".claude/settings.json").exists() and not log.exists()
