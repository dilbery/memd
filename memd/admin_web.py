"""OIDC browser console, isolated from every agent bearer authentication path."""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
import secrets
import ssl
import time
from urllib.parse import urlsplit

from authlib.integrations.httpx_client import AsyncOAuth2Client
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
import jwt

from memd.registry import Registry, ControlError, Conflict, configured

COOKIE = "__Host-memd-session"
LOGIN_COOKIE = "__Host-memd-login"
ASSETS = Path(__file__).parent / "web_assets"
TEMPLATES = Environment(loader=FileSystemLoader(Path(__file__).parent / "web_templates"), autoescape=select_autoescape())


def org_name() -> str:
    """Name shown in the web consoles' header and footer (MEMD_ORG_NAME)."""
    return os.environ.get("MEMD_ORG_NAME", "").strip() or "memd"


def admin_group() -> str:
    """Identity-provider group whose members may administer (MEMD_ADMIN_GROUP)."""
    return os.environ.get("MEMD_ADMIN_GROUP", "").strip() or "memd-admins"


def user_group() -> str:
    """Identity-provider group whose members get a personal store (MEMD_USER_GROUP)."""
    return os.environ.get("MEMD_USER_GROUP", "").strip() or "memd-users"


TEMPLATES.globals.update(org_name=org_name, admin_group=admin_group, user_group=user_group)


def settings(prefix="MEMD_WEB"):
    issuer = os.environ[prefix + "_OIDC_ISSUER"]
    origin = os.environ["MEMD_PUBLIC_URL"].rstrip("/")
    for value in (issuer, origin):
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
            raise RuntimeError("Web issuer and public URL must be fixed HTTPS URLs")
    if urlsplit(origin).path:
        raise RuntimeError("Web public URL must be an origin without a path")
    secret = Path(os.environ[prefix + "_CLIENT_SECRET_FILE"]).read_text().strip()
    if len(secret) < 32:
        raise RuntimeError("Web client secret is missing or too short")
    return {"issuer": issuer, "origin": origin, "client_id": os.environ[prefix + "_CLIENT_ID"], "secret": secret}


def cipher(cfg):
    # Dedicated confidential client secret is mounted outside the persistent DB.
    key = hashlib.sha256(b"memd-web-session-v1\0" + cfg["secret"].encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


class BrowserOIDC:
    callback_path = "/auth/callback"
    scope = "openid email profile memd-admin"
    kind = "admin"

    def require_claims(self, claims):
        require_admin(claims)

    def extra_session(self, info, claims):
        return {}

    def __init__(self, cfg):
        self.cfg = cfg
        self.cached = None
        self.until = 0
        # Use the host's CA bundle, as the agent OIDC path does: HTTPX's default
        # certifi bundle does not include a private identity provider's CA.
        self.tls = ssl.create_default_context()

    async def metadata(self):
        if self.cached and time.time() < self.until:
            return self.cached
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, verify=self.tls) as client:
            response = await client.get(self.cfg["issuer"].rstrip("/") + "/.well-known/openid-configuration")
            response.raise_for_status()
            data = response.json()
        if data.get("issuer") != self.cfg["issuer"]:
            raise ValueError("Discovery issuer mismatch")
        expected = urlsplit(self.cfg["issuer"])
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri", "userinfo_endpoint"):
            endpoint = urlsplit(data[key])
            if endpoint.scheme != "https" or endpoint.netloc != expected.netloc or endpoint.username:
                raise ValueError("Unexpected OIDC endpoint origin")
        self.cached, self.until = data, time.time() + 300
        return data

    def client(self, **kwargs):
        return AsyncOAuth2Client(self.cfg["client_id"], self.cfg["secret"],
                                 redirect_uri=self.cfg["origin"] + self.callback_path,
                                 scope=self.scope,
                                 code_challenge_method="S256", timeout=10, verify=self.tls, **kwargs)

    async def begin(self):
        metadata = await self.metadata()
        verifier, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        async with self.client() as client:
            uri, state = client.create_authorization_url(metadata["authorization_endpoint"],
                                                          code_verifier=verifier, nonce=nonce)
        return uri, {"kind": "login", "state": state, "verifier": verifier, "nonce": nonce}

    async def finish(self, pending, query):
        metadata = await self.metadata()
        async with self.client(state=pending["state"]) as client:
            token = await client.fetch_token(metadata["token_endpoint"],
                                             authorization_response=self.cfg["origin"] + self.callback_path + "?" + query,
                                             code_verifier=pending["verifier"])
        async with httpx.AsyncClient(timeout=10, verify=self.tls) as client:
            response = await client.get(metadata["jwks_uri"])
            response.raise_for_status()
            keyset = jwt.PyJWKSet.from_dict(response.json())
        raw = token["id_token"]
        header = jwt.get_unverified_header(raw)
        key = next(k for k in keyset.keys if k.key_id == header.get("kid") and k.algorithm_name == "RS256")
        claims = jwt.decode(raw, key.key, algorithms=["RS256"], audience=self.cfg["client_id"],
                            issuer=self.cfg["issuer"], options={"require": ["iss", "sub", "aud", "iat", "exp", "nonce"]})
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise ValueError("Missing subject")
        if not hmac.compare_digest(claims["nonce"], pending["nonce"]):
            raise ValueError("Nonce mismatch")
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        if (len(audiences) > 1 or "azp" in claims) and claims.get("azp") != self.cfg["client_id"]:
            raise ValueError("Authorized party mismatch")
        if "at_hash" in claims:
            expected = base64.urlsafe_b64encode(hashlib.sha256(token["access_token"].encode()).digest()[:16]).rstrip(b"=").decode()
            if not hmac.compare_digest(claims["at_hash"], expected):
                raise ValueError("Access token hash mismatch")
        self.require_claims(claims)
        info = await self.userinfo(token["access_token"], claims["sub"])
        return {"kind": self.kind, "sub": claims["sub"], "name": info.get("email") or claims["sub"],
                "csrf": secrets.token_urlsafe(32), "checked": time.time(),
                "upstream": cipher(self.cfg).encrypt(token["access_token"].encode()).decode(),
                **self.extra_session(info, claims)}

    async def userinfo(self, access_token, subject):
        metadata = await self.metadata()
        async with httpx.AsyncClient(timeout=10, verify=self.tls) as client:
            response = await client.get(metadata["userinfo_endpoint"], headers={"Authorization": "Bearer " + access_token})
            response.raise_for_status()
            info = response.json()
        if info.get("sub") != subject:
            raise ValueError("Userinfo subject mismatch")
        self.require_claims(info)
        return info


def require_admin(claims):
    groups = claims.get("groups")
    if not isinstance(groups, list) or admin_group() not in groups or claims.get("memd_admin_active") is not True:
        raise PermissionError(f"{admin_group()} membership required")


def cookie(response, name, value, age):
    response.set_cookie(name, value, max_age=age, secure=True, httponly=True, samesite="lax", path="/")


class SafeAccessLog(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            args[2] = str(args[2]).split("?", 1)[0]
            record.args = tuple(args)
        return True


def install_web(app):
    if not os.environ.get("MEMD_WEB_OIDC_ISSUER"):
        return
    if not configured():
        raise RuntimeError("Web administration requires MEMD_CONTROL_DB")
    cfg = settings()
    oidc = BrowserOIDC(cfg)
    registry = Registry()
    app.state.browser_oidc = oidc
    logging.getLogger("uvicorn.access").addFilter(SafeAccessLog())

    @app.middleware("http")
    async def web_headers(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith(("/admin", "/auth", "/assets/", "/memories", "/user-auth")):
            response.headers.update({"Cache-Control": "no-store", "Pragma": "no-cache",
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY", "Permissions-Policy": "camera=(), microphone=(), geolocation=()"})
        return response

    async def guard(request, mutation=False):
        sid = request.cookies.get(COOKIE)
        session = registry.session_get(sid)
        if not session or session.get("kind") != "admin":
            raise HTTPException(401, "Sign in with your administrator account")
        if mutation and (request.headers.get("origin") != cfg["origin"] or
                         not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"])):
            raise HTTPException(403, "Request verification failed; reload this page")
        # Re-evaluate authentik's current group/user mapping, never the old JWT claim.
        if time.time() - session["checked"] >= (60 if mutation else 300):
            try:
                access = cipher(cfg).decrypt(session["upstream"].encode()).decode()
                await oidc.userinfo(access, session["sub"])
            except PermissionError:
                registry.session_delete(sid)
                raise HTTPException(403, f"{admin_group()} membership required") from None
            except (httpx.HTTPError, ValueError, InvalidToken, KeyError):
                registry.session_delete(sid)
                raise HTTPException(401, "Sign in again to verify your current access") from None
            session["checked"] = time.time()
            with registry.connection(write=True) as db:
                db.execute("UPDATE sessions SET data=? WHERE id=?", (json.dumps(session), hashlib.sha256(sid.encode()).hexdigest()))
        return session

    @app.get("/auth/login")
    async def login(request: Request):
        try:
            uri, pending = await oidc.begin()
        except (httpx.HTTPError, ValueError, KeyError):
            raise HTTPException(503, "Sign-in service is temporarily unavailable") from None
        registry.session_delete(request.cookies.get(LOGIN_COOKIE))
        response = RedirectResponse(uri, status_code=303)
        cookie(response, LOGIN_COOKIE, registry.session_create(pending, 300), 300)
        return response

    @app.get("/auth/callback")
    async def callback(request: Request):
        pending = registry.session_consume(request.cookies.get(LOGIN_COOKIE))
        try:
            if not pending or pending.get("kind") != "login":
                raise ValueError("Login expired")
            session = await oidc.finish(pending, request.url.query)
        except PermissionError:
            response = HTMLResponse(TEMPLATES.get_template("welcome.html").render(error=f"Access is limited to {admin_group()}."), status_code=403)
        except Exception:
            # OAuth errors can contain code/token material. Never log the exception.
            response = HTMLResponse(TEMPLATES.get_template("welcome.html").render(error="Sign-in could not be verified. Please try again."), status_code=401)
        else:
            registry.session_delete(request.cookies.get(COOKIE))
            response = RedirectResponse("/admin", status_code=303)
            cookie(response, COOKIE, registry.session_create(session), 28800)
        response.delete_cookie(LOGIN_COOKIE, secure=True, httponly=True, samesite="lax")
        return response

    @app.post("/auth/logout")
    async def logout(request: Request):
        # Local logout remains possible during a provider outage.
        session = registry.session_get(request.cookies.get(COOKIE))
        if session and (request.headers.get("origin") != cfg["origin"] or not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session.get("csrf", ""))):
            raise HTTPException(403, "Request verification failed")
        registry.session_delete(request.cookies.get(COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="lax")
        return response

    async def welcome():
        return HTMLResponse(TEMPLATES.get_template("welcome.html").render(error=None, users_enabled=bool(os.environ.get("MEMD_USER_OIDC_ISSUER"))))

    # With administered accounts (MEMD_ADMIN_DB) the account dashboard owns "/"
    # and the SSO entry lives at /sso. Otherwise replace only the HTML GET
    # entry; RootMcpDispatch still owns MCP at root.
    from memd import control as accounts
    if accounts.enabled():
        app.add_api_route("/sso", welcome, methods=["GET"], include_in_schema=False)
    else:
        app.router.routes[:] = [r for r in app.router.routes if not (getattr(r, "path", None) == "/" and "GET" in (getattr(r, "methods", None) or set()))]
        app.add_api_route("/", welcome, methods=["GET"], include_in_schema=False)

    @app.get("/assets/{name}")
    async def asset(name: str):
        media = {"theme.css": "text/css", "theme.js": "application/javascript", "console.css": "text/css", "console.js": "application/javascript", "memories.css": "text/css", "memories.js": "application/javascript", "onboarding.css":"text/css", "onboarding.js":"application/javascript", "inbox.css":"text/css", "inbox.js":"application/javascript"}
        if name not in media:
            raise HTTPException(404)
        return Response((ASSETS / name).read_bytes(), media_type=media[name])

    @app.get("/admin")
    async def console(request: Request):
        try:
            session = await guard(request)
        except HTTPException as exc:
            if exc.status_code == 401:
                return RedirectResponse("/auth/login", status_code=303)
            raise
        return HTMLResponse(TEMPLATES.get_template("console.html").render(name=session["name"], csrf=session["csrf"], origin=cfg["origin"]))

    @app.get("/admin/api/tokens")
    async def tokens(request: Request):
        await guard(request)
        return {"tokens": registry.list()}

    @app.get("/admin/api/owners")
    async def owners(request: Request):
        await guard(request)
        from memd.owners import directory
        return directory()

    @app.get("/admin/api/activity")
    async def activity(request: Request):
        await guard(request)
        return {"events": registry.events()}

    @app.get("/admin/api/activity.csv")
    async def export(request: Request):
        await guard(request)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        fields = ["seq", "at", "actor", "action", "token_id", "operation_id", "detail"]
        writer.writerow(fields)
        for row in registry.events(10000):
            cells = [str(row[k] if row[k] is not None else "") for k in fields]
            writer.writerow(["'" + c if c.lstrip().startswith(("=", "+", "-", "@")) or c.startswith(("\t", "\r", "\n")) else c for c in cells])
        return Response(stream.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="memd-activity.csv"'})

    @app.post("/admin/api/tokens")
    async def create(request: Request):
        session = await guard(request, True)
        data = await payload(request)
        try:
            return registry.issue(actor=session["sub"], **data)
        except (ControlError, TypeError) as exc:
            raise HTTPException(409 if isinstance(exc, Conflict) else 400, str(exc) if isinstance(exc, ControlError) else "Invalid fields") from None

    @app.post("/admin/api/tokens/{token_id}/{action}")
    async def change(token_id: str, action: str, request: Request):
        session = await guard(request, True)
        if action not in {"rotate", "revoke", "edit"}:
            raise HTTPException(404)
        data = await payload(request)
        try:
            return getattr(registry, action)(token_id, actor=session["sub"], **data)
        except (ControlError, TypeError) as exc:
            raise HTTPException(409 if isinstance(exc, Conflict) else 400, str(exc) if isinstance(exc, ControlError) else "Invalid fields") from None


async def payload(request, max_bytes=16384):
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "JSON required")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(413, "Request too large")
    try:
        data = json.loads(body)
        if not isinstance(data, dict) or "actor" in data:
            raise ValueError()
        return data
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON object") from None
