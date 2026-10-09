import json
from pathlib import Path
import subprocess

import pytest

from memd import user_memory
from memd.identity import bind_identity, clear_identity
from memd.config import Config
from memd.store import list_notes, read_note, RETRACTED
from memd.save import RevisionConflict


@pytest.fixture
def stores(tmp_path,monkeypatch):
    monkeypatch.setenv("MEMD_STORES_ROOT",str(tmp_path/"stores"))
    monkeypatch.setenv("MEMD_LOCAL_HOST","any")
    monkeypatch.setattr("memd.save._pull_rebase_push",lambda clone:None)
    configs={}
    for user in ("alice@example.invalid","bob@example.invalid"):
        bind_identity(user);cfg=user_memory.owner_config();cfg.clone.mkdir(parents=True)
        for args in (("init","-q"),("config","user.name","Synthetic test"),("config","user.email",user)):
            subprocess.run(["git","-C",str(cfg.clone),*args],check=True,capture_output=True)
        (cfg.clone/"legacy_filename.md").write_text("---\ntitle: Personal widget\nslug: widget\nhost: any\nimportance: 4\nsource: original-source\nobserved_at: 2026-09-01\ncustom: preserve-me\n---\nPrivate widget for "+user+"\n")
        (cfg.clone/("bob-only.md" if user.startswith("bob") else "alice-only.md")).write_text("---\ntitle: Private second note\nslug: "+("bob-only" if user.startswith("bob") else "alice-only")+"\n---\nSecond secret for "+user+"\n")
        subprocess.run(["git","-C",str(cfg.clone),"add","."],check=True,capture_output=True)
        subprocess.run(["git","-C",str(cfg.clone),"commit","-qm","seed"],check=True,capture_output=True)
        configs[user]=cfg
    bind_identity("alice@example.invalid")
    yield configs
    clear_identity()


def test_owner_browse_and_edit_keep_identity_metadata_and_history(stores):
    before=user_memory.detail("widget")
    assert "alice@" in before["body"]
    assert "bob@" not in json.dumps(user_memory.browse())
    with pytest.raises(FileNotFoundError):user_memory.detail("bob-only")
    result=user_memory.change("edit",{"slug":"widget","revision":before["revision"],"title":"Corrected widget","body":"Updated owner fact","tags":["reviewed"],"description":"Current"},subject="alice-sub")
    assert result["saved"] and result["synced"]
    after=user_memory.detail("widget")
    assert after["revision"]!=before["revision"] and after["title"]=="Corrected widget"
    cfg=stores["alice@example.invalid"];note=read_note(cfg.clone,"widget")
    assert Path(note.path).name=="legacy_filename.md"
    assert note.source=="original-source" and note.observed_at=="2026-09-01"
    assert note.metadata["custom"]=="preserve-me"
    assert note.metadata["user_changed_by"]=="alice-sub"
    history=subprocess.check_output(["git","-C",str(cfg.clone),"show","HEAD~1:legacy_filename.md"],text=True)
    assert "Private widget for alice" in history
    bind_identity("bob@example.invalid")
    assert user_memory.detail("widget")["title"]=="Personal widget"


def test_stale_edit_retract_and_injected_fields_are_refused(stores):
    old=user_memory.detail("widget")
    result=user_memory.change("edit",{"slug":"widget","revision":old["revision"],"title":"new","body":"new"},subject="a")
    with pytest.raises(RevisionConflict):user_memory.change("retract",{"slug":"widget","revision":old["revision"]},subject="a")
    with pytest.raises(ValueError):user_memory.change("edit",{"slug":"widget","revision":result["revision"],"title":"x","body":"x","profile":"bob@example.invalid"},subject="a")
    with pytest.raises(ValueError):user_memory.change("retract",{"slug":"widget"},subject="a")
    with pytest.raises(FileNotFoundError):user_memory.detail("../bob@example.invalid/clone/widget")


def test_retraction_survives_failed_search_refresh_and_blocks_agent_read_save(stores,monkeypatch):
    from memd.index import open_db,refresh_lexical
    from memd.read import read
    from memd.save import save
    cfg=stores["alice@example.invalid"]
    db=open_db(cfg.db,dim=cfg.embed_dim);refresh_lexical(db,cfg);db.close()
    original=user_memory.detail("widget")
    def failed(*a,**k):raise OSError("simulated indexing outage")
    monkeypatch.setattr("memd.index.refresh_lexical",failed)
    result=user_memory.change("retract",{"slug":"widget","revision":original["revision"]},subject="alice-sub")
    assert result["saved"] and not result["search_updated"]
    assert read_note(cfg.clone,"widget").superseded_by==RETRACTED
    db=open_db(cfg.db,dim=cfg.embed_dim)
    for table in ("notes","fts_notes","vec_notes"):
        assert not db.execute(f"SELECT 1 FROM {table} WHERE slug='widget'").fetchone()
    db.close()
    from memd.recall import recall
    monkeypatch.setattr("memd.recall.embed_with_deadline", lambda *a, **k: None)
    monkeypatch.setattr("memd.recall.rerank", lambda *a, **k: None)
    assert "widget" not in [n.slug for n in recall("widget", profile=cfg.profile, cfg=cfg)]
    assert "widget" not in [n.slug for n in recall("", profile=cfg.profile, cfg=cfg)]
    with pytest.raises(FileNotFoundError):read("widget",profile=cfg.profile,cfg=cfg)
    with pytest.raises(ValueError):save({"slug":"widget","title":"Personal widget","body":"revival"},profile=cfg.profile,cfg=cfg)
    assert user_memory.detail("widget")["status"]=="retracted"
    assert user_memory.browse(state="retracted")["total"]==1
    assert "widget" not in [n["slug"] for n in user_memory.browse()["items"]]


def test_failed_commit_preserves_original_file(stores,monkeypatch):
    import memd.save as module
    cfg=stores["alice@example.invalid"];before=(cfg.clone/"legacy_filename.md").read_bytes()
    note=user_memory.detail("widget")
    def fail(*args,**kwargs):raise RuntimeError("simulated commit failure")
    monkeypatch.setattr(module,"_commit",fail)
    with pytest.raises(RuntimeError):user_memory.change("retract",{"slug":"widget","revision":note["revision"]},subject="a")
    assert (cfg.clone/"legacy_filename.md").read_bytes()==before


def test_no_bound_identity_and_no_store_are_not_shared_defaults(stores):
    clear_identity()
    with pytest.raises(PermissionError):user_memory.browse()
    bind_identity("new@example.invalid")
    cfg=user_memory.owner_config()
    assert user_memory.browse()["items"]==[]
    assert not cfg.clone.exists()


def test_note_symlink_cannot_read_other_owner(stores):
    a=stores["alice@example.invalid"];b=stores["bob@example.invalid"]
    (a.clone/"linked.md").symlink_to(b.clone/"bob-only.md")
    with pytest.raises(FileNotFoundError):user_memory.detail("bob-only")
