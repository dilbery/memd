"""_forgejo_token: env FORGEJO_TOKEN wins, else fall back to ~/.config/forgejo/token."""
import pytest

from memd.reflect import _forgejo_token, _pr_url_for_branch


def test_env_token_wins(monkeypatch):
    monkeypatch.setenv("FORGEJO_TOKEN", "envtok")
    assert _forgejo_token() == "envtok"


def test_falls_back_to_file(monkeypatch, tmp_path):
    monkeypatch.delenv("FORGEJO_TOKEN", raising=False)
    cfg = tmp_path / ".config" / "forgejo"
    cfg.mkdir(parents=True)
    (cfg / "token").write_text("filetok\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _forgejo_token() == "filetok"


def test_empty_env_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGEJO_TOKEN", "   ")
    cfg = tmp_path / ".config" / "forgejo"
    cfg.mkdir(parents=True)
    (cfg / "token").write_text("filetok\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _forgejo_token() == "filetok"


def test_missing_both_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("FORGEJO_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no token file here
    with pytest.raises(RuntimeError):
        _forgejo_token()


def test_pr_url_for_branch_found():
    pulls = [
        {"head": {"ref": "reflect/2026-07-01"}, "html_url": "http://x/pulls/1"},
        {"head": {"ref": "reflect/2026-07-08"}, "html_url": "http://x/pulls/9"},
    ]
    assert _pr_url_for_branch(pulls, "reflect/2026-07-08") == "http://x/pulls/9"


def test_pr_url_for_branch_missing():
    assert _pr_url_for_branch([], "reflect/2026-07-08") is None
    assert _pr_url_for_branch([{"head": {}}], "reflect/2026-07-08") is None
