from memd import config as config_mod
from memd.config import (
    Config,
    DEFAULT_EMBED_URL,
    DEFAULT_EMBED_MODEL,
    load_env_file,
)


def test_load_env_file_parses_systemd_format(tmp_path):
    f = tmp_path / "env"
    f.write_text(
        "# a comment\n"
        "\n"
        "   # indented comment\n"
        "MEMD_EMBED_URL=http://x:8000\n"
        "export MEMD_EMBED_MODEL=nomic\n"
        'MEMD_TOKEN="quoted-value"\n'
        "MEMD_SINGLE='single-quoted'\n"
        "MEMD_WITH_EQ=a=b=c\n"
        "  MEMD_SPACED  =  padded  \n"
        "PATH=/usr/bin\n"
        "noequals here\n"
        "=novalue\n",
        encoding="utf-8",
    )
    assert load_env_file(f) == {
        "MEMD_EMBED_URL": "http://x:8000",
        "MEMD_EMBED_MODEL": "nomic",
        "MEMD_TOKEN": "quoted-value",
        "MEMD_SINGLE": "single-quoted",
        "MEMD_WITH_EQ": "a=b=c",
        "MEMD_SPACED": "padded",
    }


def test_load_env_file_missing_returns_empty(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert load_env_file(missing) == {}
    directory = tmp_path / "adir"
    directory.mkdir()
    assert load_env_file(directory) == {}


def test_caller_env_wins_over_the_IMPLICIT_default_file(tmp_path, monkeypatch):
    """Env still overrides the implicit DEFAULT_ENV_FILE, so `VAR=x mem recall` works."""
    f = tmp_path / "env"
    f.write_text("MEMD_EMBED_URL=http://file:1111\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_ENV_FILE", f)
    cfg = Config.from_env({"MEMD_EMBED_URL": "http://env:2222"}, env_file="auto")
    assert cfg.embed_url == "http://env:2222"


def test_explicit_env_file_beats_a_stale_inherited_env(tmp_path):
    """The explicit-file-wins rule, mirroring clients/memd-mcp-bridge.

    A named MEMD_ENV_FILE is a deliberate per-consumer choice and is materialised
    fresh by the secrets manager, whereas the process env is a snapshot a long-lived shell
    took before the token was rotated and revoked. The file must win, or the CLI,
    the auto_recall hook and the in-process MCP server all present a dead token.
    """
    f = tmp_path / "env"
    f.write_text(
        "MEMD_TOKEN=mem_amber_fresh\nMEMD_EMBED_URL=http://file:1111\n",
        encoding="utf-8",
    )
    cfg = Config.from_env(
        {"MEMD_ENV_FILE": str(f),
         "MEMD_TOKEN": "mem_amber_stale_revoked",
         "MEMD_EMBED_URL": "http://env:2222"},
        env_file="auto",
    )
    assert cfg.token == "mem_amber_fresh"
    assert cfg.embed_url == "http://file:1111"


def test_explicit_env_file_kwarg_also_beats_inherited_env(tmp_path):
    """Same rule when the caller passes env_file= directly instead of via the env."""
    f = tmp_path / "env"
    f.write_text("MEMD_TOKEN=mem_amber_fresh\n", encoding="utf-8")
    cfg = Config.from_env({"MEMD_TOKEN": "mem_amber_stale_revoked"}, env_file=f)
    assert cfg.token == "mem_amber_fresh"


def test_env_still_supplies_keys_the_explicit_file_omits(tmp_path):
    """File-above-env must LAYER, not replace: keys only the env defines survive."""
    f = tmp_path / "env"
    f.write_text("MEMD_TOKEN=mem_amber_fresh\n", encoding="utf-8")
    cfg = Config.from_env(
        {"MEMD_ENV_FILE": str(f), "MEMD_PROFILE": "amber",
         "MEMD_EMBED_URL": "http://env:2222"},
        env_file="auto",
    )
    assert cfg.token == "mem_amber_fresh"
    assert cfg.embed_url == "http://env:2222"
    assert cfg.profile == "amber"


def test_file_supplies_key_absent_from_env(tmp_path):
    f = tmp_path / "env"
    f.write_text("MEMD_EMBED_URL=http://file:1111\n", encoding="utf-8")
    cfg = Config.from_env({"MEMD_ENV_FILE": str(f)}, env_file="auto")
    assert cfg.embed_url == "http://file:1111"


def test_file_layer_applies_when_env_passed_explicitly(tmp_path):
    # Regression for the memd/mcp.py::_cfg_for and auto_recall hook call sites,
    # which build an explicit env dict (with MEMD_ENV_FILE pointing at a tmp file)
    # and pass it to Config.from_env — the file layer must still be applied
    # beneath that caller env so embed_url resolves off the file, not the default.
    f = tmp_path / "env"
    f.write_text("MEMD_EMBED_URL=http://file:1111\n", encoding="utf-8")
    env = {"MEMD_PROFILE": "amber", "MEMD_ENV_FILE": str(f)}
    cfg = Config.from_env(env, env_file="auto")
    assert cfg.embed_url == "http://file:1111"
    assert cfg.profile == "amber"


def test_env_file_none_is_hermetic(tmp_path):
    f = tmp_path / "env"
    f.write_text("MEMD_EMBED_URL=http://file:1111\n", encoding="utf-8")
    cfg = Config.from_env({"MEMD_ENV_FILE": str(f)}, env_file=None)
    assert cfg.embed_url == DEFAULT_EMBED_URL


def test_provenance_fields(tmp_path):
    f = tmp_path / "env"
    f.write_text(
        "MEMD_EMBED_URL=http://file:1111\n"
        "MEMD_EMBED_MODEL=nomic\n",
        encoding="utf-8",
    )
    cfg = Config.from_env({"MEMD_ENV_FILE": str(f)}, env_file="auto")
    assert cfg.env_file_path == f
    assert cfg.env_file_keys == ("MEMD_EMBED_MODEL", "MEMD_EMBED_URL")

    cfg_none = Config.from_env({"MEMD_ENV_FILE": str(f)}, env_file=None)
    assert cfg_none.env_file_path is None
    assert cfg_none.env_file_keys == ()


def test_defaults_point_at_lemonade():
    # These previously pointed at a retired embedding backend on another port;
    # a stale default silently degrades recall to lexical-only.
    assert DEFAULT_EMBED_URL == "http://127.0.0.1:8000"
    assert DEFAULT_EMBED_MODEL == "nomic-embed-text-v1-GGUF"
