"""memd-mcp-bridge resilience: survive a token rotation and a network blip.

Two latent faults, both of which present to the model as "memd is down":

1. The bridge resolved its token ONCE at startup. The secrets manager rotates
   /run/secrets/env.d/*.env and revokes the old value, so any agent session
   that outlives a rotation handed the dead token to every later call and went
   permanently memory-less. It now re-reads the file on a 401 and threads the
   refreshed token forward, so only the one straddling call pays a retry.

2. A single dropped connection returned "network error" straight to the model,
   which reads as an empty memory and sends the agent off guessing. Requests
   that get NO reply are now retried; requests that get an HTTP status are not,
   because a status is a real answer and a retried save could double-write.

Hermetic: `post_once` is always monkeypatched, so nothing here touches the
network or the developer's real secrets-manager files.
"""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "clients" / "memd-mcp-bridge"


@pytest.fixture(scope="module")
def bridge():
    spec = importlib.util.spec_from_loader(
        "memd_mcp_bridge_res",
        importlib.machinery.SourceFileLoader("memd_mcp_bridge_res", str(BRIDGE)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _no_sleeping(bridge, monkeypatch):
    """Retries must not actually wait during tests."""
    monkeypatch.setattr(bridge.time, "sleep", lambda _s: None)


def write_env(tmp_path, token, name="memd.env"):
    p = tmp_path / name
    p.write_text('MEMD_TOKEN="{}"\n'.format(token))
    return p


REQUEST = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


# --------------------------------------------------------------------------
# Network retry
# --------------------------------------------------------------------------

def test_transient_network_failure_is_retried_then_succeeds(bridge, monkeypatch):
    calls = []

    def fake_once(endpoint, raw, token, timeout):
        calls.append(token)
        return (-1, b"") if len(calls) < 3 else (200, b'{"ok":true}')

    monkeypatch.setattr(bridge, "post_once", fake_once)
    status, body = bridge.post_raw("http://x/mcp/", REQUEST, "tok", 5.0)
    assert (status, body) == (200, b'{"ok":true}')
    assert len(calls) == 3


def test_retries_are_bounded(bridge, monkeypatch):
    calls = []

    def fake_once(endpoint, raw, token, timeout):
        calls.append(1)
        return -1, b""

    monkeypatch.setattr(bridge, "post_once", fake_once)
    status, _ = bridge.post_raw("http://x/mcp/", REQUEST, "tok", 5.0)
    assert status == -1
    assert len(calls) == bridge.NETWORK_ATTEMPTS


def test_http_status_is_never_retried(bridge, monkeypatch):
    """An HTTP status is a real answer -- retrying a save could double-write."""
    for code in (401, 429, 500):
        calls = []

        def fake_once(endpoint, raw, token, timeout, _c=code):
            calls.append(1)
            return _c, b"nope"

        monkeypatch.setattr(bridge, "post_once", fake_once)
        status, _ = bridge.post_raw("http://x/mcp/", REQUEST, "tok", 5.0)
        assert status == code
        assert len(calls) == 1, code


# --------------------------------------------------------------------------
# Token rotation
# --------------------------------------------------------------------------

def test_401_reresolves_the_token_and_retries(bridge, tmp_path, monkeypatch, capsys):
    env = write_env(tmp_path, "NEW-TOKEN")
    monkeypatch.setenv("MEMD_ENV_FILE", str(env))
    monkeypatch.delenv("MEMD_TOKEN", raising=False)

    seen = []

    def fake_once(endpoint, raw, token, timeout):
        seen.append(token)
        if token == "STALE-TOKEN":
            return 401, b'{"error":"invalid or missing token"}'
        return 200, b'{"jsonrpc":"2.0","id":1,"result":{}}'

    monkeypatch.setattr(bridge, "post_once", fake_once)
    out = bridge.handle_line(REQUEST, "http://x/mcp/", "STALE-TOKEN", 5.0)

    assert seen == ["STALE-TOKEN", "NEW-TOKEN"]
    # The refreshed token is handed back so the rest of the session uses it.
    assert out == "NEW-TOKEN"
    assert "result" in capsys.readouterr().out


def test_401_with_an_unchanged_token_does_not_loop(bridge, tmp_path, monkeypatch):
    """A genuinely revoked token must surface the 401, not retry forever."""
    env = write_env(tmp_path, "SAME-TOKEN")
    monkeypatch.setenv("MEMD_ENV_FILE", str(env))
    monkeypatch.delenv("MEMD_TOKEN", raising=False)

    calls = []

    def fake_once(endpoint, raw, token, timeout):
        calls.append(token)
        return 401, b'{"error":"invalid or missing token"}'

    monkeypatch.setattr(bridge, "post_once", fake_once)
    out = bridge.handle_line(REQUEST, "http://x/mcp/", "SAME-TOKEN", 5.0)
    assert calls == ["SAME-TOKEN"]
    assert out == "SAME-TOKEN"


def test_every_handle_line_exit_returns_a_token(bridge, monkeypatch, capsys):
    """main() assigns the return value; a bare `return` would blank the token."""
    monkeypatch.setattr(bridge, "post_once", lambda *a, **k: (200, b'{"result":1}'))

    # invalid JSON
    assert bridge.handle_line("{not json", "http://x/", "T", 5.0) == "T"
    # 200 with a body
    assert bridge.handle_line(REQUEST, "http://x/", "T", 5.0) == "T"
    # notification (no id) -> 202
    monkeypatch.setattr(bridge, "post_once", lambda *a, **k: (202, b""))
    note = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert bridge.handle_line(note, "http://x/", "T", 5.0) == "T"
    # failed request -> synthesized error
    monkeypatch.setattr(bridge, "post_once", lambda *a, **k: (500, b"boom"))
    assert bridge.handle_line(REQUEST, "http://x/", "T", 5.0) == "T"
    capsys.readouterr()


def test_notification_still_gets_no_reply_on_stdout(bridge, monkeypatch, capsys):
    """Regression guard: stdout is the protocol channel and must stay clean."""
    monkeypatch.setattr(bridge, "post_once", lambda *a, **k: (202, b""))
    note = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    bridge.handle_line(note, "http://x/", "T", 5.0)
    assert capsys.readouterr().out == ""
