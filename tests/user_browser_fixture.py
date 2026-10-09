"""Isolated HTTPS browser fixture with synthetic stores; never shipped/imported."""
import os
import json
from pathlib import Path
import secrets
import subprocess
import time

root = Path(os.environ["MEMD_BROWSER_TEST_ROOT"])
root.mkdir(parents=True, exist_ok=True)
secret = root / "client-secret"; secret.write_text(secrets.token_urlsafe(48))
directory = root / 'directory.json'
directory.write_text(json.dumps({'generated':time.time(),'directory':[
    {'email':u,'subject':u,'name':u,'active':True} for u in ('alice@example.invalid','bob@example.invalid')]}))
os.environ['MEMD_KASM_USER_MAP']=str(directory)
os.environ.update(MEMD_CONTROL_DB=str(root/"control.db"), MEMD_STORES_ROOT=str(root/"stores"),
                  MEMD_PUBLIC_URL="https://localhost:8848", MEMD_STARTUP_REFRESH="0", MEMD_BACKGROUND_REFRESH="0",
                  MEMD_LOCAL_HOST="any", MEMD_REQUIRE_RECALL_TOKEN="1")
for prefix in ("MEMD_WEB", "MEMD_USER"):
    os.environ.update({prefix+"_OIDC_ISSUER":"https://auth.example.invalid/users/",
                       prefix+"_CLIENT_ID":"synthetic-users", prefix+"_CLIENT_SECRET_FILE":str(secret)})
from memd.registry import Registry
from memd.admin_web import cipher, settings, cookie
from memd.user_web import UserOIDC, COOKIE
from memd.identity import bind_identity, clear_identity
from memd.user_memory import owner_config
from fastapi.responses import RedirectResponse
import memd.save
memd.save._pull_rebase_push=lambda clone: None
registry=Registry()
if not registry.path.exists(): registry.initialize()
for user in ("alice@example.invalid", "bob@example.invalid"):
    bind_identity(user); cfg=owner_config()
    if cfg.clone.exists(): continue
    cfg.clone.mkdir(parents=True)
    for args in (("init","-q"),("config","user.name","Synthetic test"),("config","user.email",user)):
        subprocess.run(["git","-C",str(cfg.clone),*args],check=True,capture_output=True)
    for i in range(28):
        (cfg.clone/f"memory-{i:02}.md").write_text(f"---\ntitle: Project context {i:02}\nslug: memory-{i:02}\nsource: Synthetic browser fixture\ntags: [project, context]\n---\nPrivate context for {user}.\n\nDecision {i:02}: Keep documentation current and record the reasoning behind changes.\n\n<img src=x onerror=alert('unsafe')> is displayed as plain text.\n")
    subprocess.run(["git","-C",str(cfg.clone),"add","."],check=True,capture_output=True)
    subprocess.run(["git","-C",str(cfg.clone),"commit","-qm","seed"],check=True,capture_output=True)
clear_identity()

async def synthetic_userinfo(self, access, subject):
    return {"sub":subject,"email":subject,"groups":["memd-users"],"memd_user_active":True}
UserOIDC.userinfo=synthetic_userinfo
from memd.server import app

@app.get("/__test_login")
def fixture_login(user: str="alice@example.invalid"):
    assert user in {"alice@example.invalid","bob@example.invalid"}
    response=RedirectResponse("/memories")
    session={"kind":"memory-user","sub":user,"store":user,"name":user.split("@")[0].title()+" · synthetic test",
             "csrf":secrets.token_urlsafe(32),"checked":time.time(),"is_admin":False,
             "upstream":cipher(settings("MEMD_USER")).encrypt(b"synthetic").decode()}
    cookie(response, COOKIE, registry.session_create(session),28800)
    return response
