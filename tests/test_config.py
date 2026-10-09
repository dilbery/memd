from pathlib import Path

import pytest

import memd.config as config
from memd.config import Config, MEMORY_BUDGET_BYTES, VALID_HOSTS, profile_config


def test_budget_is_24985_bytes():
    # 24.4 KB loader budget; one byte over re-introduces the truncation bug
    assert MEMORY_BUDGET_BYTES == 24985


def test_valid_hosts_match_frontmatter_enum():
    assert VALID_HOSTS == ("gpuhost", "lapbox", "vmhost", "lxc", "remote", "any")


def test_defaults_from_empty_env():
    # env_file=None: from_env() otherwise layers the real ~/.config/memd/env
    # underneath, which would mask the module defaults this test pins.
    cfg = Config.from_env({}, env_file=None)
    assert cfg.embed_url == "http://127.0.0.1:8000"
    assert cfg.embed_model == "nomic-embed-text-v1-GGUF"
    assert cfg.rerank_url == "http://127.0.0.1:8000"
    assert cfg.rerank_model == "bge-reranker-v2-m3-GGUF"
    assert cfg.profile == "amber"


def test_env_overrides():
    cfg = Config.from_env({"MEMD_PROFILE": "cobalt", "MEMD_EMBED_URL": "http://x:9"})
    assert cfg.profile == "cobalt"
    assert cfg.embed_url == "http://x:9"


def test_local_host_maps_short_hostname(monkeypatch):
    monkeypatch.setattr(config.socket, "gethostname", lambda: "gpuhost.lan")
    assert config.local_host() == "gpuhost"


def test_local_host_unknown_falls_back_to_any(monkeypatch):
    monkeypatch.setattr(config.socket, "gethostname", lambda: "randombox")
    assert config.local_host() == "any"


# --- Task 2: per-profile isolation + Path/None env contract ---


def test_defaults(monkeypatch):
    for var in (
        "MEMD_EMBED_URL", "MEMD_EMBED_MODEL", "MEMD_RERANK_URL",
        "MEMD_RERANK_MODEL", "MEMD_CLONE", "MEMD_DB", "MEMD_PROFILE", "MEMD_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = Config.from_env(env_file=None)
    assert cfg.embed_url == "http://127.0.0.1:8000"
    assert cfg.embed_model == "nomic-embed-text-v1-GGUF"
    assert cfg.rerank_url == "http://127.0.0.1:8000"
    assert cfg.rerank_model == "bge-reranker-v2-m3-GGUF"
    assert cfg.profile == "amber"
    assert cfg.token is None


def test_env_override(monkeypatch, tmp_path):
    clone = tmp_path / "clone"
    db = tmp_path / "memd.db"
    monkeypatch.setenv("MEMD_CLONE", str(clone))
    monkeypatch.setenv("MEMD_DB", str(db))
    monkeypatch.setenv("MEMD_TOKEN", "s3cr3t")
    monkeypatch.setenv("MEMD_PROFILE", "amber")
    cfg = Config.from_env()
    assert cfg.clone == clone
    assert cfg.db == db
    assert cfg.token == "s3cr3t"


def test_profile_config_isolation():
    amber = profile_config("amber")
    cobalt = profile_config("cobalt")
    # Hard isolation: separate repo / clone / db / credential per profile.
    assert amber["clone_path"] != cobalt["clone_path"]
    assert amber["db_path"] != cobalt["db_path"]
    assert amber["repo_ssh"] != cobalt["repo_ssh"]
    assert amber["credential"] != cobalt["credential"]


# --- FIX GROUP 2 (c): Config.from_env gates MEMD_CLONE/MEMD_DB overrides so a
# profile-mismatched override raises CrossProfileViolation, not silently wins. ---


def _register_two(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMD_AMBER_CLONE", str(tmp_path / "amber" / "clone"))
    monkeypatch.setenv("MEMD_AMBER_DB", str(tmp_path / "amber" / "memd.db"))
    monkeypatch.setenv("MEMD_AMBER_REPO", "r1")
    monkeypatch.setenv("MEMD_AMBER_CRED", "c1")
    monkeypatch.setenv("MEMD_COBALT_CLONE", str(tmp_path / "cobalt" / "clone"))
    monkeypatch.setenv("MEMD_COBALT_DB", str(tmp_path / "cobalt" / "memd.db"))
    monkeypatch.setenv("MEMD_COBALT_REPO", "r2")
    monkeypatch.setenv("MEMD_COBALT_CRED", "c2")


def test_clone_override_into_other_profile_raises(tmp_path, monkeypatch):
    from memd.profiles import CrossProfileViolation

    _register_two(tmp_path, monkeypatch)
    cobalt_clone = tmp_path / "cobalt" / "clone"
    # profile=amber but MEMD_CLONE points into cobalt's registered tree -> refuse.
    with pytest.raises(CrossProfileViolation):
        Config.from_env({
            "MEMD_PROFILE": "amber",
            "MEMD_CLONE": str(cobalt_clone),
        })


def test_db_override_into_other_profile_raises(tmp_path, monkeypatch):
    from memd.profiles import CrossProfileViolation

    _register_two(tmp_path, monkeypatch)
    cobalt_db = tmp_path / "cobalt" / "memd.db"
    with pytest.raises(CrossProfileViolation):
        Config.from_env({
            "MEMD_PROFILE": "amber",
            "MEMD_DB": str(cobalt_db),
        })


def test_own_profile_override_is_allowed(tmp_path, monkeypatch):
    _register_two(tmp_path, monkeypatch)
    amber_clone = tmp_path / "amber" / "clone"
    amber_db = tmp_path / "amber" / "memd.db"
    cfg = Config.from_env({
        "MEMD_PROFILE": "amber",
        "MEMD_CLONE": str(amber_clone),
        "MEMD_DB": str(amber_db),
    })
    assert cfg.clone == amber_clone and cfg.db == amber_db


def test_neutral_override_outside_all_roots_is_allowed(tmp_path, monkeypatch):
    """A MEMD_CLONE override that belongs to NO registered profile (e.g. a test
    tmp dir, or the legacy single-clone systemd path) must still be honored by
    config — the gate only rejects a CROSS-profile override, not a neutral one
    (the recall/save guard treats it as the profile's effective root)."""
    _register_two(tmp_path, monkeypatch)
    neutral = tmp_path / "scratch" / "clone"
    cfg = Config.from_env({
        "MEMD_PROFILE": "amber",
        "MEMD_CLONE": str(neutral),
        "MEMD_DB": str(tmp_path / "scratch" / "memd.db"),
    })
    assert cfg.clone == neutral


def test_local_host_env_override_wins(monkeypatch):
    """The hub's guests are not in VALID_HOSTS; MEMD_LOCAL_HOST=any stops a note being
    scoped to whichever container happened to save it."""
    monkeypatch.setattr(config.socket, "gethostname", lambda: "gpuhost")
    monkeypatch.setenv("MEMD_LOCAL_HOST", "any")
    assert config.local_host() == "any"


def test_local_host_env_override_is_validated(monkeypatch):
    monkeypatch.setattr(config.socket, "gethostname", lambda: "randombox")
    monkeypatch.setenv("MEMD_LOCAL_HOST", "not-a-host")
    assert config.local_host() == "any"


def test_local_host_without_override_still_maps_the_hostname(monkeypatch):
    monkeypatch.delenv("MEMD_LOCAL_HOST", raising=False)
    monkeypatch.setattr(config.socket, "gethostname", lambda: "vmhost.lan")
    assert config.local_host() == "vmhost"
