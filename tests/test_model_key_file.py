"""MEMD_MODEL_API_KEY_FILE: the model key read from a mounted secret file.

A GitOps sync cannot carry stack env, so the key comes from a Swarm
secret mounted as a file. A misconfigured file must fail loudly: a silent None
means every embedding call 401s behind a green health check.
"""
import importlib.util
from pathlib import Path

import pytest

from memd.config import Config, model_api_key_from, model_headers

KEY = "sk-synthetic-model-key-" + "k" * 24


def _file(tmp_path, text):
    p = tmp_path / "model-api-key"
    p.write_text(text)
    return p


def test_key_read_from_file(tmp_path):
    p = _file(tmp_path, KEY)
    cfg = Config.from_env({"MEMD_MODEL_API_KEY_FILE": str(p)}, env_file=None)
    assert cfg.model_api_key == KEY
    assert model_headers(cfg) == {"Authorization": "Bearer " + KEY}


def test_trailing_newline_is_stripped(tmp_path):
    p = _file(tmp_path, KEY + "\n")
    cfg = Config.from_env({"MEMD_MODEL_API_KEY_FILE": str(p)}, env_file=None)
    assert cfg.model_api_key == KEY


def test_env_value_still_works_without_file():
    cfg = Config.from_env({"MEMD_MODEL_API_KEY": KEY}, env_file=None)
    assert cfg.model_api_key == KEY


def test_empty_file_variable_is_treated_as_unset():
    cfg = Config.from_env({"MEMD_MODEL_API_KEY_FILE": ""}, env_file=None)
    assert cfg.model_api_key is None


def test_both_set_is_refused(tmp_path):
    p = _file(tmp_path, KEY)
    with pytest.raises(RuntimeError, match="both") as exc:
        Config.from_env({"MEMD_MODEL_API_KEY": "sk-other",
                         "MEMD_MODEL_API_KEY_FILE": str(p)}, env_file=None)
    assert KEY not in str(exc.value) and "sk-other" not in str(exc.value)


def test_missing_file_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="MEMD_MODEL_API_KEY_FILE"):
        Config.from_env({"MEMD_MODEL_API_KEY_FILE": str(tmp_path / "absent")}, env_file=None)


def test_blank_file_is_refused(tmp_path):
    p = _file(tmp_path, "  \n")
    with pytest.raises(RuntimeError, match="empty"):
        Config.from_env({"MEMD_MODEL_API_KEY_FILE": str(p)}, env_file=None)


def test_process_environment_path(tmp_path, monkeypatch):
    """Config.from_env() with no mapping is what server._cfg() uses."""
    p = _file(tmp_path, KEY + "\n")
    monkeypatch.delenv("MEMD_MODEL_API_KEY", raising=False)
    monkeypatch.setenv("MEMD_MODEL_API_KEY_FILE", str(p))
    assert Config.from_env(env_file=None).model_api_key == KEY


def test_helper_matches_config(tmp_path):
    p = _file(tmp_path, KEY)
    assert model_api_key_from({"MEMD_MODEL_API_KEY_FILE": str(p)}) == KEY
    assert model_api_key_from({"MEMD_MODEL_API_KEY": KEY}) == KEY
    assert model_api_key_from({}) is None


def test_key_is_not_in_config_repr():
    cfg = Config.from_env({"MEMD_MODEL_API_KEY": KEY}, env_file=None)
    assert cfg.model_api_key == KEY
    assert KEY not in repr(cfg)


@pytest.fixture
def multitenant_startup(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "entrypoint_model_key", Path(__file__).parents[1] / "deploy/entrypoint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("MEMD_ENFORCE_PROFILE", raising=False)
    monkeypatch.delenv("MEMD_MODEL_API_KEY", raising=False)
    monkeypatch.setenv("MEMD_REQUIRE_RECALL_TOKEN", "1")
    monkeypatch.setenv("MEMD_TOKEN", "synthetic-test-token-" + "x" * 32)
    monkeypatch.setenv("MEMD_TOKENS_FILE", str(tmp_path / "absent-tokens"))
    monkeypatch.setenv("MEMD_STORES_ROOT", str(tmp_path / "stores"))
    return module


def test_startup_refuses_missing_key_file(multitenant_startup, tmp_path, monkeypatch):
    """A multi-tenant deployment never builds a Config at startup.
    A bad key file must still stop the task at start, not at the first save."""
    monkeypatch.setenv("MEMD_MODEL_API_KEY_FILE", str(tmp_path / "absent"))
    with pytest.raises(RuntimeError, match="MEMD_MODEL_API_KEY_FILE"):
        multitenant_startup.initialize()
    assert not (tmp_path / "stores").exists()


def test_startup_accepts_readable_key_file(multitenant_startup, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMD_MODEL_API_KEY_FILE", str(_file(tmp_path, KEY)))
    multitenant_startup.initialize()
    assert (tmp_path / "stores").is_dir()
