import json
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from memd import control, server, sources
from memd.refresh import ensure_lexical

PASSWORD = "test-password-long-enough"


@pytest.fixture
def admin(config,tmp_path,monkeypatch):
    monkeypatch.setenv("MEMD_ADMIN_DB",str(tmp_path/"control"/"admin.db"))
    monkeypatch.setenv("MEMD_ENFORCE_PROFILE","1")
    monkeypatch.setenv("MEMD_VAULT_ROOTS",str(tmp_path/"vaults"))
    monkeypatch.setattr(sources._jobs,"submit",lambda fn,*args: fn(*args))
    control.initialize()
    control.register_existing()
    uid=control.create_user("owner",PASSWORD,"admin")
    ensure_lexical(config)
    app=server.create_token_app()
    with TestClient(app) as client:
        login=client.post("/ui/login",json={"username":"owner","password":PASSWORD})
        assert login.status_code==200
        client.headers["X-CSRF-Token"]=login.json()["csrf"]
        yield client,app,tmp_path,uid


def member(client,store="amber",permission="read"):
    created=client.post("/admin/users",json={"username":"member"}).json()
    assert client.put(f'/admin/users/{created["id"]}/grants',json={"grants":{store:permission}}).status_code==200
    return created


def token(client,uid,store="amber",scope="read"):
    r=client.post("/admin/tokens",json={"label":"test-agent","user_id":uid,"store_id":store,"scope":scope})
    assert r.status_code==200,r.text
    return r.json()


def mcp(client,value,name,args):
    response=client.post("/mcp/",headers={"Authorization":"Bearer "+value,"Accept":"application/json, text/event-stream"},
                         json={"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":name,"arguments":args}})
    assert response.status_code==200,response.text
    return response.json()["result"]


def test_admin_boundary_sessions_csrf_and_password(admin):
    client,app,_,uid=admin
    guest=TestClient(app)
    assert guest.get("/admin/overview").status_code==401
    assert guest.get("/ui/notes").status_code==401
    assert guest.post("/admin/users",headers={"Authorization":"Bearer test-token"},json={"username":"rogue"}).status_code==403
    client.headers.pop("X-CSRF-Token")
    assert client.post("/admin/users",json={"username":"rogue"}).status_code==403
    client.headers["X-CSRF-Token"]=client.get("/ui/me").json()["csrf"]
    assert client.patch(f"/admin/users/{uid}",json={"disabled":True}).status_code==400
    assert client.post("/ui/password",json={"current":"incorrect","password":PASSWORD}).status_code==400
    assert client.post("/ui/password",json={"current":PASSWORD,"password":PASSWORD+"-new"}).status_code==200
    assert client.get("/admin/overview").status_code==401
    assert guest.post("/ui/login",headers={"Origin":"https://evil.invalid"},json={"username":"owner","password":PASSWORD+"-new"}).status_code==403
    with control.db() as c:
        stored=c.execute("SELECT password FROM users WHERE id=?",(uid,)).fetchone()[0]
        assert PASSWORD not in stored


def test_token_listing_and_immediate_rest_mcp_revocation(admin):
    client,app,_,uid=admin
    user=member(client)
    issued=token(client,user["id"])
    api=TestClient(app)
    headers={"Authorization":"Bearer "+issued["token"]}
    assert api.get("/ui/notes",headers=headers).status_code==200
    assert api.post("/save",headers=headers,json={"body":"not allowed"}).status_code==403
    assert mcp(api,issued["token"],"save",{"body":"not allowed"})["isError"] is True
    assert mcp(api,issued["token"],"read",{"slug":"repo-hosting-policy"})["isError"] is False
    listing=client.get("/admin/overview").json()
    assert issued["token"] not in json.dumps(listing)
    record=next(t for t in listing["tokens"] if t["id"]==issued["id"])
    assert record["last_used"] and record["scope"]=="read"
    assert client.delete("/admin/tokens/"+issued["id"]).status_code==200
    assert api.get("/ui/notes",headers=headers).status_code==401
    assert api.post("/mcp/",headers=headers,json={}).status_code==401
    with control.db() as c:
        assert issued["token"] not in str([tuple(r) for r in c.execute("SELECT * FROM tokens")])


def test_legacy_tokens_visible_revocable_and_never_admin(admin):
    client,app,_,_=admin
    api=TestClient(app)
    headers={"Authorization":"Bearer test-token"}
    assert api.get("/ui/notes",headers=headers).status_code==200
    listing=client.get("/admin/overview").json()
    old=next(t for t in listing["tokens"] if t["label"]=="legacy")
    assert old["legacy"]==1
    assert api.get("/admin/overview",headers=headers).status_code==403
    assert client.delete("/admin/tokens/"+old["id"]).status_code==200
    assert api.get("/ui/notes",headers=headers).status_code==401


def test_new_store_user_isolation_and_grant_changes(admin):
    client,app,_,uid=admin
    response=client.post("/admin/stores",json={"id":"team","name":"Team memory","kind":"local"})
    assert response.status_code==200,response.text
    user=member(client,"team","write")
    issued=token(client,user["id"],"team","write")
    api=TestClient(app)
    headers={"Authorization":"Bearer "+issued["token"]}
    saved=api.post("/save",headers=headers,json={"title":"Team secret","body":"Only team can read this."})
    assert saved.status_code==200,saved.text
    assert saved.json()["saved"] is True
    assert api.get("/ui/notes",headers=headers).json()["total"]==1
    assert api.get("/ui/notes?profile=amber",headers=headers).status_code==403
    assert mcp(api,issued["token"],"read",{"slug":"repo-hosting-policy","profile":"amber"})["isError"] is True
    assert mcp(api,issued["token"],"read",{"slug":"team-secret"})["structuredContent"]["profile"]=="team"
    assert api.get("/ui/notes?profile=team",headers={"Authorization":"Bearer test-token"}).status_code==403
    client.put(f'/admin/users/{user["id"]}/grants',json={"grants":{"team":"read"}})
    assert api.post("/save",headers=headers,json={"body":"revoked write"}).status_code==403
    client.patch(f'/admin/users/{user["id"]}',json={"disabled":True})
    assert api.get("/ui/notes",headers=headers).status_code==401
    # A new account can log in, but has no administrator permissions.
    other=TestClient(app)
    assert other.post("/ui/login",json={"username":"member","password":user["temporary_password"]}).status_code==401


def test_settings_validate_paths_and_remote_transports(admin):
    client,_,tmp,_=admin
    for cfg in ({"repo_ssh":"ext::sh -c attack"},{"repo_ssh":"file:///etc"},{"repo_ssh":"https://user:secret@example.test/a.git"}):
        assert client.post("/admin/stores",json={"id":"bad","kind":"git","config":cfg}).status_code==400
    assert client.post("/admin/stores",json={"id":"bad","kind":"obsidian","config":{"vault_path":"/etc"}}).status_code==400
    assert client.post("/admin/stores",json={"id":"../escape","kind":"local"}).status_code==400
    assert client.put("/admin/stores/amber",json={"config":{"clone_path":"/tmp/escape"}}).status_code==200
    assert control.store("amber")["config"]["clone_path"]!="/tmp/escape"


def test_obsidian_import_write_folder_and_conflicts(admin):
    client,app,tmp,uid=admin
    vault=tmp/"vaults"/"notes";vault.mkdir(parents=True)
    original="---\ntags: [research]\ncustom_property: keep\n---\n# Human note\nA link to [[Another note]].\n"
    (vault/"Human.md").write_text(original)
    (vault/"Private").mkdir();(vault/"Private"/"secret.md").write_text("private")
    (vault/".obsidian").mkdir();(vault/".obsidian"/"config.md").write_text("skip")
    outside=tmp/"outside.md";outside.write_text("outside")
    (vault/"escape.md").symlink_to(outside)
    response=client.post("/admin/stores",json={"id":"vault","name":"Obsidian","kind":"obsidian","config":{"vault_path":str(vault),"exclude":["Private/**"],"sync_interval":0}})
    assert response.status_code==200,response.text
    issued=token(client,uid,"vault","write")
    api=TestClient(app);headers={"Authorization":"Bearer "+issued["token"]}
    listing=api.get("/ui/notes",headers=headers).json()
    assert listing["total"]==1,listing
    slug=listing["notes"][0]["slug"]
    assert api.post("/save",headers=headers,json={"slug":slug,"body":"overwrite"}).status_code==403
    assert (vault/"Human.md").read_text()==original
    saved=api.post("/save",headers=headers,json={"title":"Agent memory","body":"Useful context."})
    assert saved.status_code==200,saved.text
    output=next((vault/"Memories").glob("*.md"))
    assert "Useful context." in output.read_text()
    output.write_text(output.read_text()+"\nHuman edit\n")
    modified=output.read_text()
    saved=api.post("/save",headers=headers,json={"title":"Agent memory","body":"New agent version."})
    assert saved.json()["saved"] is True
    assert any("conflicting edit" in w for w in saved.json()["warnings"])
    assert output.read_text()==modified
    with pytest.raises(ValueError,match="conflict"):
        sources.run_job("vault","sync")
    conflict=client.get("/admin/stores/vault/conflicts").json()["conflicts"][0]
    assert "Human edit" in conflict["vault_preview"]
    assert "New agent version" in conflict["memd_preview"]
    stale={**conflict,"vault_hash":"changed","keep":"vault"}
    assert client.post("/admin/stores/vault/resolve-conflict",json=stale).status_code==409
    assert client.post("/admin/stores/vault/resolve-conflict",json={**conflict,"keep":"vault"}).status_code==200
    assert client.get("/admin/stores/vault/conflicts").json()["conflicts"]==[]
    assert output.read_text()==modified
    assert "Human edit" in api.post("/read",headers=headers,json={"slug":"agent-memory"}).json()["body"]
    sources.run_job("vault","sync")


def test_expired_tokens_and_failed_login_throttle(admin):
    client,app,_,uid=admin
    issued=token(client,uid)
    with control.db() as c:
        c.execute("UPDATE tokens SET expires=? WHERE id=?",(time.time()-1,issued["id"]))
    guest=TestClient(app)
    assert guest.get("/ui/notes",headers={"Authorization":"Bearer "+issued["token"]}).status_code==401
    for _ in range(10):
        assert guest.post("/ui/login",json={"username":"owner","password":"wrong"}).status_code==401
    assert guest.post("/ui/login",json={"username":"owner","password":PASSWORD}).status_code==429


def test_git_store_clone_pull_save_push_and_credentials(admin,monkeypatch):
    """Exercise real Git operations, substituting only the remote transport."""
    import subprocess
    client,app,tmp,uid=admin
    bare=tmp/"remote.git";seed=tmp/"seed"
    def run(*args,cwd=None):
        return subprocess.run(["git",*args],cwd=cwd,check=True,capture_output=True,text=True).stdout.strip()
    run("init","--bare","--initial-branch=main",str(bare))
    run("clone",str(bare),str(seed))
    run("config","user.name","Fixture",cwd=seed);run("config","user.email","fixture@example.test",cwd=seed)
    (seed/"guide.md").write_text("# Git source guide\nExisting repo content.\n")
    run("add",".",cwd=seed);run("commit","-m","seed",cwd=seed);run("push","origin","main",cwd=seed)
    url="https://example.test/memory.git"
    def local_git(cfg,*args,clone=None,timeout=60):
        rewritten=[str(bare) if a==url else a for a in args]
        command=["git","-c","core.hooksPath=/dev/null","-c","protocol.file.allow=always"]
        if clone: command += ["-C",str(clone)]
        return subprocess.run(command+rewritten,check=True,capture_output=True,text=True,env=sources.git_env(cfg),timeout=timeout).stdout.strip()
    monkeypatch.setattr(sources,"git",local_git)
    response=client.post("/admin/stores",json={"id":"git-store","name":"Git notes","kind":"git","config":{"repo_ssh":url,"secret":"fake-transport-password","credential_type":"https","sync_interval":0}})
    assert response.status_code==200,response.text
    assert "fake-transport-password" not in response.text
    row=control.store("git-store")
    assert Path(row["config"]["credential"]).stat().st_mode & 0o777 == 0o600
    issued=token(client,uid,"git-store","write")
    api=TestClient(app);headers={"Authorization":"Bearer "+issued["token"]}
    assert api.get("/ui/notes",headers=headers).json()["total"]==1
    saved=api.post("/save",headers=headers,json={"title":"Written by agent","body":"Published through ordinary Git."})
    assert saved.json()["saved"] is True and saved.json()["synced"] is True,saved.text
    run("pull","--ff-only",cwd=seed)
    assert any("Published through ordinary Git" in p.read_text() for p in seed.glob("*.md"))
    (seed/"guide.md").write_text("# Git source guide\nChanged remotely.\n")
    run("add",".",cwd=seed);run("commit","-m","update",cwd=seed);run("push",cwd=seed)
    sources.run_job("git-store","sync")
    body=api.post("/read",headers=headers,json={"slug":"git-source-guide"}).json()["body"]
    assert "Changed remotely" in body
    assert api.get("/ui/export?profile=amber",headers=headers).status_code==403
    exported=api.get("/ui/export",headers=headers)
    assert exported.status_code==200 and exported.headers["content-type"]=="application/zip"


def test_empty_store_healthy_but_missing_index_not_masked(admin,monkeypatch):
    client,app,tmp,_=admin
    client.post("/admin/stores",json={"id":"empty-store","name":"Empty","kind":"local"})
    monkeypatch.setattr(server,"embed_with_deadline",lambda *a,**kw:None)
    monkeypatch.setattr(server,"rerank",lambda *a,**kw:None)
    health=client.get("/health?profile=empty-store").json()
    assert health["ok"] is True and health["checks"]["index"]["ok"] is True
    row=control.store("empty-store");clone=Path(row["config"]["clone_path"])
    (clone/"new.md").write_text("# Not indexed yet\nPresent on disk.\n")
    health=client.get("/health?profile=empty-store").json()
    assert health["ok"] is False


# ---------------------------------------------------------------- team sharing in the web UI

def store_form(store,name,kind,**changes):
    """The payload the Settings store form sends (memd/web/admin.js): every field,
    with publish_review only when the checkbox was changed."""
    config={key:"" for key in ("repo_ssh","branch","vault_path","git_username","embed_url","embed_model","rerank_url","rerank_model","key_file")}
    config.update(write_folder="Memories",include=["**/*.md"],exclude=[],sync_interval=0,**changes)
    return {"id":store,"name":name,"kind":kind,"config":config}


def listed(client,store):
    return next(s for s in client.get("/admin/overview").json()["stores"] if s["id"]==store)


def test_settings_publish_review_checkbox_round_trip(admin,monkeypatch):
    from memd import share
    client,_,_,_=admin
    # The served (existing, indexed) store: on by default, as publishing does.
    assert listed(client,"amber")["publish_review"] is True and share.publish_review("amber") is True
    r=client.put("/admin/stores/amber",json=store_form("amber","amber","existing",publish_review=False))
    assert r.status_code==200,r.text
    assert r.json()["store"]["publish_review"] is False
    assert listed(client,"amber")["publish_review"] is False and share.publish_review("amber") is False
    # Saving other settings with the checkbox untouched keeps the choice.
    assert client.put("/admin/stores/amber",json=store_form("amber","Amber notes","existing")).status_code==200
    assert listed(client,"amber")["publish_review"] is False
    assert client.put("/admin/stores/amber",json=store_form("amber","amber","existing",publish_review=True)).status_code==200
    assert listed(client,"amber")["publish_review"] is True and share.publish_review("amber") is True
    assert client.put("/admin/stores/amber",json=store_form("amber","amber","existing",publish_review="no")).status_code==400
    # A new store created with the box cleared.
    r=client.post("/admin/stores",json=store_form("team","Team","local",publish_review=False))
    assert r.status_code==200,r.text
    assert listed(client,"team")["publish_review"] is False and share.publish_review("team") is False
    # A store without the setting shows its environment default, and saving the
    # form without touching the box does not pin that default as a setting.
    monkeypatch.setenv("MEMD_SHARED_PUBLISH_REVIEW","false")
    assert client.post("/admin/stores",json=store_form("shared","Shared","local")).status_code==200
    assert listed(client,"shared")["publish_review"] is False
    assert client.put("/admin/stores/shared",json=store_form("shared","Shared team","local")).status_code==200
    assert "publish_review" not in control.store("shared")["config"]
    monkeypatch.delenv("MEMD_SHARED_PUBLISH_REVIEW")
    assert listed(client,"shared")["publish_review"] is True


def issue_form(uid,store,scope,extra):
    """The payload the Administration token form sends (memd/web/admin.js)."""
    return {"label":"shared-agent","user_id":uid,"store_id":store,"scope":scope,
            "expires":time.time()+90*86400,"extra_stores":extra}


def test_token_form_issues_additional_stores(admin):
    client,app,_,uid=admin
    assert client.post("/admin/stores",json={"id":"team","name":"Team","kind":"local"}).status_code==200
    user=member(client,"amber","write")
    assert client.put(f'/admin/users/{user["id"]}/grants',json={"grants":{"amber":"write","team":"read"}}).status_code==200
    # The form offers the owner's store access as the server computes it.
    users={u["username"]:u for u in client.get("/admin/overview").json()["users"]}
    assert users["member"]["access"]=={"amber":"write","team":"read"}
    assert users["owner"]["access"]=={"amber":"write","team":"write"}
    r=client.post("/admin/tokens",json=issue_form(user["id"],"amber","write",{"team":"read"}))
    assert r.status_code==200,r.text
    issued=r.json()
    record=next(t for t in client.get("/admin/overview").json()["tokens"] if t["id"]==issued["id"])
    assert record["store_id"]=="amber" and record["extra"]=={"team":"read"}
    api=TestClient(app);headers={"Authorization":"Bearer "+issued["token"]}
    assert api.get("/ui/notes?profile=team",headers=headers).status_code==200
    assert api.post("/save",headers=headers,json={"profile":"team","title":"Nope","body":"read only"}).status_code==403
    assert api.get("/ui/notes",headers=headers).json()["profile"]=="amber"
    # Removing the owner's grant removes the token's reach there.
    assert client.put(f'/admin/users/{user["id"]}/grants',json={"grants":{"amber":"write"}}).status_code==200
    assert api.get("/ui/notes?profile=team",headers=headers).status_code==403
    # No additional stores chosen: the form sends an empty object.
    plain=client.post("/admin/tokens",json=issue_form(user["id"],"amber","read",{}))
    assert plain.status_code==200,plain.text
    assert next(t for t in client.get("/admin/overview").json()["tokens"] if t["id"]==plain.json()["id"])["extra"]=={}


def test_token_additional_stores_refused_beyond_owner_grants(admin):
    client,_,_,_=admin
    for store in ("team","other"):
        assert client.post("/admin/stores",json={"id":store,"name":store,"kind":"local"}).status_code==200
    user=member(client,"amber","read")
    assert client.put(f'/admin/users/{user["id"]}/grants',json={"grants":{"amber":"read","team":"read"}}).status_code==200
    before=len(client.get("/admin/overview").json()["tokens"])
    for extra,message in (({"other":"read"},"additional store"),   # not granted at all
                          ({"team":"write"},"additional store"),   # granted read only
                          ({"amber":"read"},"must differ"),         # the main store again
                          ({"missing":"read"},"Unknown store"),
                          ({"team":"admin"},"read or write"),
                          ({f"s{i}":"read" for i in range(16)},"at most"),
                          (["team"],"object")):
        r=client.post("/admin/tokens",json=issue_form(user["id"],"amber","read",extra))
        assert r.status_code==400 and message in r.json()["detail"],(extra,r.text)
    assert len(client.get("/admin/overview").json()["tokens"])==before


def test_dashboard_renders_both_settings_as_text(admin):
    client,app,_,_=admin
    page=TestClient(app).get("/")
    html=page.text
    assert "script-src 'self'" in page.headers["content-security-policy"] and "unsafe-inline" not in page.headers["content-security-policy"]
    assert '<input id="store-publish-review" type="checkbox" checked aria-describedby="store-publish-review-help">Published notes need review</label>' in html
    assert 'id="store-publish-review-help"' in html
    assert '<legend>Additional stores (optional)</legend>' in html and 'id="token-extra"' in html
    assert ' style="' not in html
    script=TestClient(app).get("/ui/assets/admin.js").text
    assert "extra_stores" in script and "publish_review" in script
    for unsafe in ("innerHTML","outerHTML","insertAdjacentHTML","document.write"):
        assert unsafe not in script
