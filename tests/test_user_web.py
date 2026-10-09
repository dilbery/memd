import json
import time
from urllib.parse import urlsplit,parse_qs

import httpx
import jwt
import pytest
import respx
from fastapi.testclient import TestClient

from tests.test_admin_web import key
from tests.test_user_memory import stores
from memd.registry import Registry
from memd.identity import clear_identity
from memd.user_web import COOKIE
from memd import user_memory

ORIGIN="https://memd.example.invalid"
AUTH="https://auth.example.invalid"
ISSUER=AUTH+"/application/o/memd-users/"


@pytest.fixture
def userweb(stores,tmp_path,monkeypatch):
    secret=tmp_path/"client-secret";secret.write_text("synthetic-"+"x"*48)
    monkeypatch.setenv("MEMD_CONTROL_DB",str(tmp_path/"control.db"))
    monkeypatch.setenv("MEMD_PUBLIC_URL",ORIGIN)
    for prefix in ("MEMD_WEB","MEMD_USER"):
        monkeypatch.setenv(prefix+"_CLIENT_SECRET_FILE",str(secret))
        monkeypatch.setenv(prefix+"_CLIENT_ID","memd-users" if prefix=="MEMD_USER" else "memd-web")
        monkeypatch.setenv(prefix+"_OIDC_ISSUER",ISSUER if prefix=="MEMD_USER" else AUTH+"/application/o/memd-web/")
    registry=Registry();registry.initialize()
    clear_identity()
    from memd.server import create_token_app
    app=create_token_app()
    with TestClient(app,base_url=ORIGIN,follow_redirects=False) as client:
        yield client,registry,app
    clear_identity()


def sign_in(client,router,key,**over):
    router.get(ISSUER+".well-known/openid-configuration").mock(return_value=httpx.Response(200,json={
        "issuer":ISSUER,"authorization_endpoint":AUTH+"/authorize/","token_endpoint":AUTH+"/token/","jwks_uri":ISSUER+"jwks/","userinfo_endpoint":AUTH+"/userinfo/"}))
    result=client.get("/user-auth/login");assert result.status_code==303
    args=parse_qs(urlsplit(result.headers["location"]).query)
    assert args["code_challenge_method"]==["S256"]
    assert args["redirect_uri"]==[ORIGIN+"/user-auth/callback"]
    now=int(time.time());claims={"iss":ISSUER,"sub":"alice-sub","aud":"memd-users","iat":now,"exp":now+600,
        "nonce":args["nonce"][0],"email":"alice@example.invalid","groups":["memd-users"],"memd_user_active":True}
    claims.update(over)
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()));jwk.update(kid="user-key",alg="RS256",use="sig")
    router.get(ISSUER+"jwks/").mock(return_value=httpx.Response(200,json={"keys":[jwk]}))
    token=jwt.encode(claims,key,algorithm="RS256",headers={"kid":"user-key"})
    router.post(AUTH+"/token/").mock(return_value=httpx.Response(200,json={"id_token":token,"access_token":"synthetic-user-access","token_type":"Bearer"}))
    userinfo=router.get(AUTH+"/userinfo/").mock(return_value=httpx.Response(200,json=claims))
    result=client.get("/user-auth/callback",params={"code":"synthetic","state":args["state"][0]})
    return result,userinfo


def headers(client,registry):
    session=registry.session_get(client.cookies.get(COOKIE))
    return {"Origin":ORIGIN,"X-CSRF-Token":session["csrf"]}


def test_signed_user_login_isolated_browsing_edit_and_retract(userweb,key):
    client,registry,_=userweb
    with respx.mock(assert_all_called=False) as router:
        result,_=sign_in(client,router,key);assert result.status_code==303
        page=client.get("/memories");assert page.status_code==200
        assert page.headers["cache-control"]=="no-store"
        listed=client.get("/memories/api/list");assert listed.status_code==200
        assert "bob@example.invalid" not in listed.text
        assert client.get("/memories/api/note?slug=bob-only").status_code==404
        assert client.get("/memories/api/list?profile=bob@example.invalid").status_code==400
        assert client.get("/admin/api/tokens").status_code==401
        item=client.get("/memories/api/note?slug=widget").json()
        h=headers(client,registry)
        mutation={"slug":"widget","revision":item["revision"],"title":"Corrected","body":"Only Alice can change this","tags":["corrected"]}
        assert client.post("/memories/api/edit",headers=h,json=mutation).status_code==200
        assert client.post("/memories/api/edit",headers=h,json=mutation).status_code==409
        updated=client.get("/memories/api/note?slug=widget").json()
        assert updated["body"]=="Only Alice can change this"
        assert client.post("/memories/api/retract",headers=h,json={"slug":"widget","revision":updated["revision"]}).status_code==200
        assert client.get("/memories/api/list?status=retracted").json()["total"]==1
        assert client.post("/user-auth/logout",headers=h,json={}).status_code==200
        assert client.get("/memories/api/list").status_code==401


@pytest.mark.parametrize("claims",[{"groups":["memd-admins"]},{"memd_user_active":False},{"aud":"memd-web"},
    {"email":"../bob@example.invalid"},{"email":""},{"nonce":"bad"},{"iss":AUTH+"/wrong"}])
def test_user_oidc_rejects_wrong_group_audience_identity_nonce(userweb,key,claims):
    client,_,_=userweb
    with respx.mock(assert_all_called=False) as router:
        result,_=sign_in(client,router,key,**claims);assert result.status_code in (401,403)
        assert client.get("/memories/api/list").status_code==401


@pytest.mark.parametrize("fresh", [
    {"email":"bob@example.invalid","groups":["memd-users"],"memd_user_active":True},
    {"email":"alice@example.invalid","groups":[],"memd_user_active":False},
])
def test_csrf_and_identity_change_invalidate_session(userweb,key,fresh):
    client,registry,_=userweb
    with respx.mock(assert_all_called=False) as router:
        _,info=sign_in(client,router,key)
        assert client.post("/memories/api/retract",json={}).status_code==403
        h=headers(client,registry)
        assert client.post("/memories/api/edit",headers={**h,"Origin":"https://evil.invalid"},json={}).status_code==403
        with registry.connection(write=True) as db:
            for row in db.execute("SELECT id,data FROM sessions").fetchall():
                data=json.loads(row["data"]);data["checked"]=time.time()-301
                db.execute("UPDATE sessions SET data=? WHERE id=?",(json.dumps(data),row["id"]))
        info.mock(return_value=httpx.Response(200,json={"sub":"alice-sub",**fresh}))
        assert client.get("/memories/api/list").status_code==403
        assert client.get("/memories/api/list").status_code==401


def test_admin_and_bearer_cannot_be_reused_as_user_session(userweb):
    client,registry,_=userweb
    cookie=registry.session_create({"kind":"admin","sub":"admin","csrf":"admin"})
    client.cookies.set(COOKIE,cookie)
    assert client.get("/memories/api/list").status_code==401
    assert client.get("/memories/api/list",headers={"Authorization":"Bearer forged","X-Forwarded-User":"alice@example.invalid"}).status_code==401
