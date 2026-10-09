"""memd-mcp-bridge token precedence: the live secret file must beat a stale env var.

Regression guard for a past outage. The secrets manager materialises per-consumer
tokens into /run/secrets/env.d/*.env and ROTATES them, revoking the old value.
A shell startup hook loads memd.env at shell startup, so a long-lived tmux
session keeps a pre-rotation token and exports it to every child forever. The bridge
resolved `$MEMD_TOKEN` first, so that dead snapshot silently beat the live file and
every MCP call returned 401 with nothing naming which source had won.

Hermetic: no network, no reads of the developer's real secret files - every
case writes its own env file into tmp_path and sets MEMD_ENV_FILE at it.
"""
import importlib.util
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "clients" / "memd-mcp-bridge"


@pytest.fixture(scope="module")
def bridge():
    """Import the bridge script (no .py suffix, so load it by explicit path)."""
    spec = importlib.util.spec_from_loader(
        "memd_mcp_bridge",
        importlib.machinery.SourceFileLoader("memd_mcp_bridge", str(BRIDGE)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_env(tmp_path, token, name="memd.env"):
    p = tmp_path / name
    # Real secrets-manager files quote the token and carry unrelated keys around it.
    p.write_text(
        "MEMD_URL=https://memd.example.com\n"
        "MEMD_PROFILE=amber\n"
        'MEMD_TOKEN="{}"\n'.format(token)
    )
    return p


def test_configured_file_beats_stale_env_token(bridge, tmp_path, monkeypatch):
    """The exact regression: a stale export must not shadow the configured file."""
    env_file = write_env(tmp_path, "mem_amber_fresh")
    monkeypatch.setenv("MEMD_TOKEN", "mem_amber_stale_revoked")
    monkeypatch.setenv("MEMD_ENV_FILE", str(env_file))

    token, source = bridge.resolve_token()

    assert token == "mem_amber_fresh"
    assert str(env_file) in source


def test_env_token_used_when_no_file_is_configured(bridge, tmp_path, monkeypatch):
    """Manual override still works: no MEMD_ENV_FILE means the export wins."""
    monkeypatch.setenv("MEMD_TOKEN", "mem_amber_manual")
    monkeypatch.delenv("MEMD_ENV_FILE", raising=False)
    # Point the default at a path that cannot exist so the real file is never read.
    monkeypatch.setattr(bridge, "DEFAULT_ENV_FILE", str(tmp_path / "absent.env"))

    token, source = bridge.resolve_token()

    assert token == "mem_amber_manual"
    assert source == "$MEMD_TOKEN"


def test_configured_file_missing_falls_back_to_env(bridge, tmp_path, monkeypatch):
    """A configured-but-absent file must not strand a usable env token."""
    monkeypatch.setenv("MEMD_TOKEN", "mem_amber_manual")
    monkeypatch.setenv("MEMD_ENV_FILE", str(tmp_path / "absent.env"))

    token, source = bridge.resolve_token()

    assert token == "mem_amber_manual"
    assert source == "$MEMD_TOKEN"


def test_no_token_anywhere_reports_none(bridge, tmp_path, monkeypatch):
    monkeypatch.delenv("MEMD_TOKEN", raising=False)
    monkeypatch.setenv("MEMD_ENV_FILE", str(tmp_path / "absent.env"))

    token, source = bridge.resolve_token()

    assert token is None
    assert "no token" in source


def test_quotes_and_whitespace_are_stripped(bridge, tmp_path, monkeypatch):
    p = tmp_path / "memd.env"
    p.write_text("MEMD_TOKEN=  'mem_amber_quoted'  \n")
    monkeypatch.delenv("MEMD_TOKEN", raising=False)
    monkeypatch.setenv("MEMD_ENV_FILE", str(p))

    token, _ = bridge.resolve_token()

    assert token == "mem_amber_quoted"


def test_endpoint_comes_from_remote_env_when_args_are_empty(bridge, monkeypatch):
    """The mount fix: the URL moved out of argv into MEMD_REMOTE.

    A harness that sees a bare https URL in a stdio server's args may treat the
    entry as a remote HTTP MCP server, skip the bridge and never read
    MEMD_ENV_FILE - omp v17.3.4 does exactly that, and every call 401s. Both
    ~/.claude.json and ~/.codex/config.toml now pass args: [] instead.
    """
    monkeypatch.setenv("MEMD_REMOTE", "https://memd.example.com")

    assert bridge.resolve_endpoint(["memd-mcp-bridge"]) == "https://memd.example.com/mcp/"


def test_explicit_arg_still_wins_over_remote_env(bridge, monkeypatch):
    monkeypatch.setenv("MEMD_REMOTE", "https://ignored.example")

    resolved = bridge.resolve_endpoint(["memd-mcp-bridge", "https://memd.example.com"])

    assert resolved == "https://memd.example.com/mcp/"


def test_flags_are_not_mistaken_for_the_endpoint(bridge, monkeypatch):
    monkeypatch.setenv("MEMD_REMOTE", "https://memd.example.com")

    assert bridge.resolve_endpoint(["memd-mcp-bridge", "--selftest"]) == (
        "https://memd.example.com/mcp/"
    )
