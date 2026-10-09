"""Deployment-specific names come from the environment, not the source.

The repository ships placeholder profile and host names; a deployment maps them
to its own with MEMD_LEGACY_PROFILES and MEMD_HOST_NAMES.
"""
import json
from types import SimpleNamespace

import httpx
import respx

from memd import config, profiles
from memd.config import Config
from memd.embed import CANARY_STRING, startup_canary
from memd.ground import ground
from memd.hosts import canonical_host
from memd.infer_host import infer_host


def test_placeholder_profiles_by_default(monkeypatch):
    monkeypatch.delenv("MEMD_LEGACY_PROFILES", raising=False)
    assert config.legacy_profiles() == ("amber", "cobalt")
    assert config.default_profile() == "amber"


def test_legacy_profiles_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMD_LEGACY_PROFILES", "first, second-one")
    monkeypatch.setenv("MEMD_SECOND_ONE_CLONE", str(tmp_path / "c"))
    monkeypatch.setenv("MEMD_SECOND_ONE_DB", str(tmp_path / "m.db"))
    monkeypatch.delenv("MEMD_PROFILE", raising=False)
    assert config.default_profile() == "first"
    assert "amber" not in profiles.registry()
    got = profiles.resolve("second-one")
    assert got["clone_path"] == (tmp_path / "c").resolve()
    assert got["credential"] == "MEMD_TOKEN_SECOND_ONE"
    assert profiles.resolve_profile() == "first"


def test_host_names_map_roles(monkeypatch):
    monkeypatch.setenv("MEMD_HOST_NAMES", "gpuhost=ws1, apphost=nas")
    assert config.host_name("gpuhost") == "ws1"
    assert config.host_name("vmhost") == "vmhost"
    assert "ws1" in config.valid_hosts() and "gpuhost" not in config.valid_hosts()
    monkeypatch.setenv("MEMD_LOCAL_HOST", "ws1")
    assert config.local_host() == "ws1"
    assert canonical_host("10.10.1.10") == canonical_host("NAS.local") == "nas"


def test_inference_and_grounding_use_mapped_names(monkeypatch):
    monkeypatch.setenv("MEMD_HOST_NAMES", "gpuhost=ws1,vmhost=buildbox")
    note = SimpleNamespace(host="any", title="t", body="ssh buildbox and restart")
    assert infer_host(note) == "buildbox"
    monkeypatch.setenv("MEMD_HOST_SIGNALS", '{"gpuhost": ["iGPU"], "vmhost": "not-a-list"}')
    note = SimpleNamespace(host="any", title="t", body="inference server on the igpu")
    assert infer_host(note) == "ws1"
    monkeypatch.setenv("MEMD_HOST_SIGNALS", "{broken")
    assert infer_host(note) == "any"
    assert ground(SimpleNamespace(host="ws1", body="plain"), host_checker=lambda *_: True) == "ok"
    assert ground(SimpleNamespace(host="gpuhost", body="x")) == "unverified-remote"


@respx.mock
def test_canary_refingerprints_a_legacy_file(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMD_EMBED_URL", "http://127.0.0.1:8081")
    cfg = Config.from_env(env_file=None)
    respx.post("http://127.0.0.1:8081/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"embedding": [0.5] * 768}]}))
    fp = tmp_path / "fp.json"
    fp.write_text(json.dumps([-0.5] * 768))  # v1: bare vector of another canary text
    startup_canary(cfg, fingerprint_path=fp)  # no drift error
    assert json.loads(fp.read_text())["canary"] == CANARY_STRING


def test_deployment_names_are_read_from_the_env_file(monkeypatch, tmp_path):
    """Regression: set only in ~/.config/memd/env, these left every recall/save UnknownProfile."""
    env_file = tmp_path / "env"
    env_file.write_text("MEMD_LEGACY_PROFILES=first,second\nMEMD_HOST_NAMES=gpuhost=ws1,apphost=nas\n"
                        f"MEMD_SECOND_CLONE={tmp_path / 'c'}\nMEMD_SECOND_DB={tmp_path / 'm.db'}\n")
    monkeypatch.setattr(config, "DEFAULT_ENV_FILE", env_file)
    for key in ("MEMD_LEGACY_PROFILES", "MEMD_HOST_NAMES", "MEMD_ENV_FILE", "MEMD_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    assert config.legacy_profiles() == ("first", "second")
    assert "first" in profiles.registry() and "amber" not in profiles.registry()
    assert profiles.resolve("second")["clone_path"] == (tmp_path / "c").resolve()
    assert profiles.resolve_profile() == "first"
    assert config.host_name("gpuhost") == "ws1" and canonical_host("10.10.1.10") == "nas"
    # The cache follows the file.
    env_file.write_text("MEMD_LEGACY_PROFILES=third\n")
    import os
    os.utime(env_file, ns=(1, 1))
    assert config.legacy_profiles() == ("third",)


def test_a_note_without_a_profile_line_reads_as_the_configured_default(monkeypatch, tmp_path):
    from memd.store import Note, parse_note
    note_file = tmp_path / "n.md"
    note_file.write_text("---\ntitle: T\nslug: t\n---\nbody\n")
    monkeypatch.delenv("MEMD_LEGACY_PROFILES", raising=False)
    assert parse_note(note_file).profile == "amber"
    monkeypatch.setenv("MEMD_LEGACY_PROFILES", "first,second")
    assert parse_note(note_file).profile == "first"
    assert Note(title="t", slug="t", path="t.md", body="b").profile == "first"
