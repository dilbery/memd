import json
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from memd.registry import Registry, principal
from memd.admin_web import COOKIE, LOGIN_COOKIE

ORIGIN = "https://memd.example.invalid"
AUTH = "https://auth.example.invalid"
ISSUER = AUTH + "/application/o/memd-web/"


@pytest.fixture
def web(tmp_path, monkeypatch):
    secret = tmp_path / "client-secret"; secret.write_text("synthetic-secret-" + "x"*48)
    for key,value in {"MEMD_CONTROL_DB":str(tmp_path/"control.db"),"MEMD_WEB_OIDC_ISSUER":ISSUER,
                      "MEMD_WEB_CLIENT_ID":"memd-web","MEMD_WEB_CLIENT_SECRET_FILE":str(secret),
                      "MEMD_PUBLIC_URL":ORIGIN,"MEMD_REQUIRE_RECALL_TOKEN":"1"}.items():
        monkeypatch.setenv(key,value)
    registry = Registry(); registry.initialize()
    from memd.server import create_token_app
    app = create_token_app()
    with TestClient(app,base_url=ORIGIN,follow_redirects=False) as client:
        yield app, client, registry
    principal.set(None)


@pytest.fixture(scope="module")
def key(): return rsa.generate_private_key(public_exponent=65537,key_size=2048)


def provider(router,key):
    metadata = {"issuer":ISSUER,"authorization_endpoint":AUTH+"/authorize/", "token_endpoint":AUTH+"/token/",
                "jwks_uri":ISSUER+"jwks/","userinfo_endpoint":AUTH+"/userinfo/"}
    router.get(ISSUER+".well-known/openid-configuration").mock(return_value=httpx.Response(200,json=metadata))
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())); jwk.update(kid="web-key",alg="RS256",use="sig")
    router.get(ISSUER+"jwks/").mock(return_value=httpx.Response(200,json={"keys":[jwk]}))
    user = router.get(AUTH+"/userinfo/").mock(return_value=httpx.Response(200,json={
        "sub":"administrator-sub","email":"admin@example.invalid","groups":["memd-admins"],"memd_admin_active":True}))
    return user


def login(client,router,key,**overrides):
    response=client.get("/auth/login")
    assert response.status_code==303
    params=parse_qs(urlsplit(response.headers["location"]).query)
    assert params["code_challenge_method"]==["S256"]
    assert params["redirect_uri"]==[ORIGIN+"/auth/callback"]
    assert "client_secret" not in params
    now=int(time.time())
    claims={"iss":ISSUER,"sub":"administrator-sub","aud":"memd-web","iat":now,"exp":now+600,
            "nonce":params["nonce"][0],"email":"admin@example.invalid","groups":["memd-admins"],"memd_admin_active":True}
    claims.update(overrides)
    raw=jwt.encode(claims,key,algorithm="RS256",headers={"kid":"web-key"})
    def exchange(request):
        fields=parse_qs(request.content.decode())
        assert fields["code"]==["synthetic-code"]
        assert len(fields["code_verifier"][0])>=43
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(200,json={"id_token":raw,"access_token":"synthetic-access-token","token_type":"Bearer","expires_in":600})
    router.post(AUTH+"/token/").mock(side_effect=exchange)
    return client.get("/auth/callback",params={"code":"synthetic-code","state":params["state"][0]})


def mutation(client):
    registry=Registry(); data=registry.session_get(client.cookies.get(COOKIE))
    return {"Origin":ORIGIN,"X-CSRF-Token":data["csrf"]}


def new_token(**overrides):
    body={"label":"nightly job","owner":"Platform","purpose":"test automation","stores":["one"],
          "operations":["recall","read"],"days":90,"operation_id":str(uuid.uuid4())}
    body.update(overrides); return body


def test_admin_signed_oidc_session_and_token_lifecycle(web,key):
    app,client,registry=web
    with respx.mock(assert_all_called=False) as router:
        provider(router,key)
        callback=login(client,router,key)
        assert callback.status_code==303, callback.text
        assert callback.headers["location"]=="/admin"
        cookies=callback.headers.get_list("set-cookie")
        assert any("Secure" in c and "HttpOnly" in c and "SameSite=lax" in c for c in cookies)
        page=client.get("/admin"); assert page.status_code==200
        assert "admin@example.invalid" in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert page.headers["cache-control"]=="no-store"
        headers=mutation(client)
        body=new_token(); created=client.post("/admin/api/tokens",headers=headers,json=body)
        assert created.status_code==200,created.text
        secret=created.json()["secret"]; token=created.json()["token"]
        assert registry.authenticate(secret)
        assert client.post("/admin/api/tokens",headers=headers,json=body).status_code==409
        metadata=client.get("/admin/api/tokens")
        assert secret not in metadata.text and "verifier" not in metadata.text
        assert client.post(f'/admin/api/tokens/{token["id"]}/revoke',headers=headers,
                           json={"revision":1,"operation_id":str(uuid.uuid4())}).status_code==200
        assert registry.authenticate(secret) is None
        activity=client.get("/admin/api/activity")
        assert len(activity.json()["events"])==2
        assert secret not in activity.text
        assert client.post("/auth/logout",headers=headers,json={}).status_code==200
        assert client.get("/admin/api/tokens").status_code==401


@pytest.mark.parametrize("changes",[{"aud":"memd-agent"},{"iss":AUTH+"/other/"},{"nonce":"wrong"},
    {"exp":1},{"groups":["memd-users"]},{"groups":"memd-admins"},{"memd_admin_active":False},
    {"aud":["memd-web","other"],"azp":"other"},{"sub":""},{"at_hash":"wrong"}])
def test_reject_wrong_audience_identity_nonce_group_and_expiry(web,key,changes):
    _,client,_=web
    with respx.mock(assert_all_called=False) as router:
        provider(router,key)
        assert login(client,router,key,**changes).status_code in (401,403)
        assert client.get("/admin/api/tokens").status_code==401


def test_no_bearer_or_forged_header_can_administer(web):
    _,client,registry=web
    created=registry.issue(actor="cli",**new_token())
    for headers in ({},{"Authorization":"Bearer "+created["secret"]},
                    {"X-Forwarded-User":"admin","X-Forwarded-Groups":"memd-admins"},
                    {"Cookie":COOKIE+"=forged"}):
        assert client.get("/admin/api/tokens",headers=headers).status_code==401
        assert client.post("/admin/api/tokens",headers=headers,json=new_token()).status_code==401
    assert client.post("/",json={}).status_code==401
    assert client.get("/",headers={"Accept":"text/event-stream"}).status_code==401
    assert client.get("/").status_code==200


def test_csrf_origin_and_callback_replay(web,key):
    _,client,registry=web
    with respx.mock(assert_all_called=False) as router:
        provider(router,key); assert login(client,router,key).status_code==303
        for headers in ({},{"Origin":ORIGIN},{**mutation(client),"Origin":"https://evil.invalid"},
                        {**mutation(client),"X-CSRF-Token":"wrong"}):
            assert client.post("/admin/api/tokens",headers=headers,json=new_token()).status_code==403
        assert registry.list()==[]
        assert client.get("/auth/callback?code=synthetic-code&state=replay").status_code==401
        assert client.post("/auth/logout",headers={"Origin":"https://evil.invalid"}).status_code==403


def test_live_group_removal_revokes_existing_session(web,key):
    _,client,registry=web
    with respx.mock(assert_all_called=False) as router:
        userinfo=provider(router,key); assert login(client,router,key).status_code==303
        headers=mutation(client)
        with registry.connection(write=True) as db:
            row=db.execute("SELECT id,data FROM sessions").fetchone(); data=json.loads(row["data"])
            assert "synthetic-access-token" not in row["data"]
            data["checked"]=time.time()-61
            db.execute("UPDATE sessions SET data=? WHERE id=?",(json.dumps(data),row["id"]))
        userinfo.mock(return_value=httpx.Response(200,json={"sub":"administrator-sub","groups":["memd-users"],"memd_admin_active":False}))
        assert client.post("/admin/api/tokens",headers=headers,json=new_token()).status_code==403
        assert client.get("/admin/api/tokens").status_code==401
        assert registry.list()==[]


def test_export_escapes_formulas_and_ui_escapes_html(web,key):
    _,client,registry=web
    registry.issue(actor="=HYPERLINK(bad)",**new_token(label="<script>alert(1)</script>"))
    with respx.mock(assert_all_called=False) as router:
        provider(router,key); assert login(client,router,key).status_code==303
        export=client.get("/admin/api/activity.csv")
        assert "'=HYPERLINK(bad)" in export.text
        assert "attachment" in export.headers["content-disposition"]
        assert client.get("/assets/console.js").headers["content-type"].startswith("application/javascript")
        assert client.get("/assets/nope").status_code==404
