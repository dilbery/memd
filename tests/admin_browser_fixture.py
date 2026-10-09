"""Local synthetic browser fixture. Never imported by the shipped application.

Run with MEMD_BROWSER_TEST_ROOT pointing at a temporary directory and uvicorn
tests.admin_browser_fixture:app --host 127.0.0.1 --ssl-keyfile ... --ssl-certfile ...
"""
import os
import json
from pathlib import Path
import secrets
import time
import uuid

root = Path(os.environ["MEMD_BROWSER_TEST_ROOT"])
root.mkdir(parents=True, exist_ok=True)
secret = root / "client-secret"; secret.write_text(secrets.token_urlsafe(48))
directory = root / "directory.json"
directory.write_text(json.dumps({"directory":[{"email":"alice@example.invalid","name":"Alice Example","username":"alice"}]}))
os.environ["MEMD_KASM_USER_MAP"] = str(directory)
os.environ.update(MEMD_CONTROL_DB=str(root/"control.db"), MEMD_WEB_OIDC_ISSUER="https://auth.example.invalid/web/",
                  MEMD_WEB_CLIENT_ID="memd-web", MEMD_WEB_CLIENT_SECRET_FILE=str(secret),
                  MEMD_PUBLIC_URL="https://localhost:8847", MEMD_STARTUP_REFRESH="0", MEMD_BACKGROUND_REFRESH="0")
from memd.registry import Registry
from memd.admin_web import BrowserOIDC, COOKIE, cipher, settings, cookie
from fastapi.responses import RedirectResponse

registry=Registry()
if not registry.path.exists():
    registry.initialize()
    for index in range(19):
        registry.issue(actor="synthetic-admin", operation_id=str(uuid.uuid4()), label=f"Synthetic index job {index+1:02}",
                       owner="Platform services", purpose="Browser test fixture", stores=["automation"], operations=["reindex","stats"])

async def synthetic_userinfo(self, access, subject):
    return {"sub":subject,"groups":["memd-admins"],"memd_admin_active":True}
BrowserOIDC.userinfo=synthetic_userinfo
from memd.server import app

@app.get("/__test_login")
def fixture_login():
    response=RedirectResponse("/admin")
    session={"kind":"admin","sub":"synthetic-admin","name":"Administrator · synthetic test", "csrf":secrets.token_urlsafe(32),
             "checked":time.time(),"upstream":cipher(settings()).encrypt(b"synthetic").decode()}
    cookie(response, COOKIE, registry.session_create(session),28800)
    return response
