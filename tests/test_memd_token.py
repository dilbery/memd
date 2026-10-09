"""deploy/memd-token authors the file memd.mcp_http.load_tokens reads."""
import os
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).parents[1] / "deploy" / "memd-token"


def run(*args, env=None):
    return subprocess.run([sys.executable, str(TOOL), *args],
                          capture_output=True, text=True, env=env)


def test_issue_creates_file_and_prints_token_once(tmp_path):
    f = tmp_path / "tokens"
    result = run("--file", str(f), "issue", "pilot")
    assert result.returncode == 0, result.stderr
    token = result.stdout.strip()
    assert token.startswith("mem_") and len(token) >= 40
    assert result.stdout.count("\n") == 1
    assert f.read_text().splitlines() == [f"pilot {token}"]
    assert oct(f.stat().st_mode & 0o777) == "0o600"


def test_issue_appends_and_list_shows_labels_only(tmp_path):
    f = tmp_path / "tokens"
    first = run("--file", str(f), "issue", "a").stdout.strip()
    second = run("--file", str(f), "issue", "b").stdout.strip()
    listing = run("--file", str(f), "list").stdout
    assert listing.split() == ["a", "b"]
    assert first not in listing and second not in listing


def test_revoke_removes_only_that_label(tmp_path):
    f = tmp_path / "tokens"
    run("--file", str(f), "issue", "keep")
    run("--file", str(f), "issue", "drop")
    assert run("--file", str(f), "revoke", "drop").returncode == 0
    assert [line.split()[0] for line in f.read_text().splitlines()] == ["keep"]


def test_revoke_unknown_label_is_an_error(tmp_path):
    f = tmp_path / "tokens"
    f.write_text("x tok\n")
    assert run("--file", str(f), "revoke", "nope").returncode == 2
    assert f.read_text() == "x tok\n"


def test_issue_duplicate_label_is_refused(tmp_path):
    f = tmp_path / "tokens"
    run("--file", str(f), "issue", "same")
    before = f.read_text()
    result = run("--file", str(f), "issue", "same")
    assert result.returncode == 2 and "exists" in result.stderr
    assert f.read_text() == before


def test_comments_and_blank_lines_are_preserved(tmp_path):
    f = tmp_path / "tokens"
    f.write_text("# a comment\n\nkeep tok1\ndrop tok2\n")
    run("--file", str(f), "revoke", "drop")
    assert f.read_text() == "# a comment\n\nkeep tok1\n"


def test_env_default_path(tmp_path):
    f = tmp_path / "t"
    env = {**os.environ, "MEMD_TOKENS_FILE": str(f)}
    assert run("issue", "e", env=env).returncode == 0
    assert f.exists()


def test_mode_is_preserved_across_a_revoke(tmp_path):
    f = tmp_path / "tokens"
    run("--file", str(f), "issue", "a")
    run("--file", str(f), "issue", "b")
    os.chmod(f, 0o640)
    run("--file", str(f), "revoke", "a")
    assert oct(f.stat().st_mode & 0o777) == "0o640"


def test_server_loader_accepts_the_file(tmp_path, monkeypatch):
    """The whole point: what this writes is what memd reads."""
    from memd.mcp_http import load_tokens
    f = tmp_path / "tokens"
    token = run("--file", str(f), "issue", "svc").stdout.strip()
    monkeypatch.setenv("MEMD_TOKENS_FILE", str(f))
    monkeypatch.delenv("MEMD_TOKEN", raising=False)
    assert load_tokens() == {token: "svc"}


def test_revoked_token_stops_authenticating(tmp_path, monkeypatch):
    from memd.mcp_http import check_bearer
    f = tmp_path / "tokens"
    token = run("--file", str(f), "issue", "gone").stdout.strip()
    monkeypatch.setenv("MEMD_TOKENS_FILE", str(f))
    monkeypatch.delenv("MEMD_TOKEN", raising=False)
    assert check_bearer(f"Bearer {token}") == "gone"
    run("--file", str(f), "revoke", "gone")
    assert check_bearer(f"Bearer {token}") is None
