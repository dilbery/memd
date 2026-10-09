import json
import pytest
from memd.owners import directory,backfill_legacy
from tests.test_control import registry


def test_directory_owner_lookup_and_unambiguous_legacy_backfill(registry,tmp_path,monkeypatch):
    path=tmp_path/"users.json"
    path.write_text(json.dumps({"generated":123,"users":{},"directory":[
        {"email":"alice@example.invalid","name":"Alice Example","username":"alice","active":True},
        {"email":"bob@example.invalid","name":"Bob Example","username":"shared","active":True},
        {"email":"charlie@example.invalid","name":"Charlie Example","username":"shared","active":True},
        {"email":"offboarded@example.invalid","username":"gone","active":False}]}))
    monkeypatch.setenv("MEMD_KASM_USER_MAP",str(path))
    assert len(directory()["owners"])==3
    registry.import_legacy({"a"*40:"alice","b"*40:"shared","c"*40:"nightly-job"})
    assert backfill_legacy(registry)==1
    assert backfill_legacy(registry)==0
    records={r["label"]:r for r in registry.list()}
    assert records["alice"]["owner"]=="alice@example.invalid"
    assert records["shared"]["owner"]=="Legacy — review owner"
    assert records["nightly-job"]["owner"]=="Legacy — review owner"
    assert records["alice"]["revision"]==2


def test_legacy_kasm_map_fallback_and_unavailable_file(tmp_path,monkeypatch):
    path=tmp_path/"users.json";monkeypatch.setenv("MEMD_KASM_USER_MAP",str(path))
    assert directory()["owners"]==[]
    path.write_text(json.dumps({"generated":10,"users":{"uuid":"alice@example.invalid"},"active_sessions":["must-not-be-exposed"]}))
    result=directory()
    assert result["owners"][0]["email"]=="alice@example.invalid"
    assert "must-not-be-exposed" not in json.dumps(result)
