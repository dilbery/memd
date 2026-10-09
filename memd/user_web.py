"""Personal memory browser surface for the user group. No request may select a memory owner/store."""
import asyncio
import hashlib
import hmac
import json
import os
import time

from cryptography.fernet import InvalidToken
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import httpx

from memd.registry import Registry, ControlError, Conflict, Denied
from memd.identity import bind_identity
from memd.oidc import identity_from_claims, OidcError
from memd.save import RevisionConflict, CommitOutcomeUnknown
from memd.store import StoreUnavailable
from memd.admin_web import BrowserOIDC, TEMPLATES, admin_group, settings, cipher, cookie, payload, user_group
from memd import user_memory

COOKIE = "__Host-memd-user-session"
LOGIN_COOKIE = "__Host-memd-user-login"


class UserOIDC(BrowserOIDC):
    callback_path = "/user-auth/callback"
    scope = "openid email profile memd-user-console"
    kind = "memory-user"

    def require_claims(self, claims):
        groups = claims.get("groups")
        if not isinstance(groups, list) or user_group() not in groups or claims.get("memd_user_active") is not True:
            raise PermissionError(f"{user_group()} membership required")
        try:
            identity_from_claims(claims)
        except OidcError:
            raise PermissionError("A valid user email is required") from None

    def extra_session(self, info, claims):
        store = identity_from_claims(info)
        if store != identity_from_claims(claims):
            raise PermissionError("Identity changed during login; sign in again")
        return {"store": store, "is_admin": admin_group() in info["groups"]}


def install_user_web(app):
    if not os.environ.get("MEMD_USER_OIDC_ISSUER"):
        return
    if not os.environ.get("MEMD_WEB_OIDC_ISSUER") or not os.environ.get("MEMD_STORES_ROOT"):
        raise RuntimeError("The user console requires the configured web service and per-user stores")
    cfg = settings("MEMD_USER")
    oidc = UserOIDC(cfg); registry = Registry()
    app.state.user_oidc = oidc

    async def guard(request, mutation=False):
        sid = request.cookies.get(COOKIE)
        session = registry.session_get(sid)
        if not session or session.get("kind") != "memory-user":
            raise HTTPException(401, "Sign in to view your memories")
        if mutation and (request.headers.get("origin") != cfg["origin"] or
                         not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"])):
            raise HTTPException(403, "Request verification failed; reload this page")
        if time.time() - session["checked"] >= (60 if mutation else 300):
            try:
                info = await oidc.userinfo(cipher(cfg).decrypt(session["upstream"].encode()).decode(), session["sub"])
                if identity_from_claims(info) != session["store"]:
                    raise PermissionError("Identity changed; sign in again")
            except PermissionError:
                registry.session_delete(sid)
                raise HTTPException(403, "Your current account does not have access. Sign in again or contact your administrator.") from None
            except (httpx.HTTPError, ValueError, InvalidToken, KeyError, OidcError):
                registry.session_delete(sid)
                raise HTTPException(401, "Sign in again to verify your current access") from None
            session["checked"] = time.time()
            session["is_admin"] = admin_group() in info.get("groups", [])
            with registry.connection(write=True) as db:
                db.execute("UPDATE sessions SET data=? WHERE id=?", (json.dumps(session), hashlib.sha256(sid.encode()).hexdigest()))
        bind_identity(session["store"])
        return session

    @app.get("/user-auth/login")
    async def login(request: Request):
        try:
            uri, pending = await oidc.begin()
        except (httpx.HTTPError, ValueError, KeyError):
            raise HTTPException(503, "Sign-in service is temporarily unavailable") from None
        pending["kind"] = "user-login"
        registry.session_delete(request.cookies.get(LOGIN_COOKIE))
        response = RedirectResponse(uri, 303)
        cookie(response, LOGIN_COOKIE, registry.session_create(pending, 300), 300)
        return response

    @app.get("/user-auth/callback")
    async def callback(request: Request):
        pending = registry.session_consume(request.cookies.get(LOGIN_COOKIE))
        registry.session_delete(request.cookies.get(COOKIE))
        try:
            if not pending or pending.get("kind") != "user-login":
                raise ValueError("Login expired")
            session = await oidc.finish(pending, request.url.query)
        except PermissionError:
            response = HTMLResponse(TEMPLATES.get_template("welcome.html").render(error=f"My memories is available to {user_group()}.", users_enabled=True),403)
        except Exception:
            response = HTMLResponse(TEMPLATES.get_template("welcome.html").render(error="Sign-in could not be verified. Please try again.", users_enabled=True),401)
        else:
            response = RedirectResponse("/memories",303)
            cookie(response, COOKIE, registry.session_create(session),28800)
        response.delete_cookie(LOGIN_COOKIE,secure=True,httponly=True,samesite="lax")
        return response

    @app.post("/user-auth/logout")
    async def logout(request: Request):
        session = registry.session_get(request.cookies.get(COOKIE))
        if session and (request.headers.get("origin") != cfg["origin"] or not hmac.compare_digest(request.headers.get("x-csrf-token", ""),session.get("csrf", ""))):
            raise HTTPException(403,"Request verification failed")
        registry.session_delete(request.cookies.get(COOKIE))
        response = JSONResponse({"ok":True})
        response.delete_cookie(COOKIE,secure=True,httponly=True,samesite="lax")
        return response

    @app.get("/memories")
    async def page(request: Request):
        try:
            session = await guard(request)
        except HTTPException as exc:
            if exc.status_code == 401:
                return RedirectResponse("/user-auth/login",303)
            raise
        return HTMLResponse(TEMPLATES.get_template("memories.html").render(name=session["name"],csrf=session["csrf"],is_admin=session.get("is_admin",False)))

    @app.get("/memories/api/list")
    async def listing(request: Request, q: str = "", status: str = "active", page: int = 1):
        await guard(request)
        if set(request.query_params) - {"q", "status", "page"}:
            raise HTTPException(400,"Unsupported search fields")
        return await operation(user_memory.browse,q,status,page)

    @app.get("/memories/onboarding")
    async def onboarding(request: Request):
        try:
            session = await guard(request)
        except HTTPException as exc:
            if exc.status_code == 401:
                return RedirectResponse("/user-auth/login",303)
            raise
        return HTMLResponse(TEMPLATES.get_template("onboarding.html").render(
            name=session["name"],csrf=session["csrf"],is_admin=session.get("is_admin",False),origin=cfg["origin"]))

    @app.get("/memories/onboarding/api/tokens")
    async def personal_tokens(request: Request):
        session = await guard(request)
        return {"tokens":registry.personal_list(session["sub"],session["store"])}

    @app.post("/memories/onboarding/api/tokens")
    async def issue_personal(request: Request):
        session = await guard(request,True)
        data = await payload(request)
        if set(data) != {"label","days","operation_id"}:
            raise HTTPException(400,"Use a label, expiry and operation ID")
        return token_operation(registry.issue_personal,subject=session["sub"],store=session["store"],**data)

    @app.post("/memories/onboarding/api/tokens/{token_id}/revoke")
    async def revoke_personal(token_id: str, request: Request):
        session = await guard(request,True)
        data = await payload(request)
        if set(data) != {"revision","operation_id"}:
            raise HTTPException(400,"A revision and operation ID are required")
        return token_operation(registry.revoke_personal,token_id,subject=session["sub"],store=session["store"],**data)

    @app.get("/memories/api/note")
    async def note(request: Request, slug: str):
        await guard(request)
        if set(request.query_params) != {"slug"}:
            raise HTTPException(400,"Unsupported memory fields")
        return await operation(user_memory.detail,slug)

    @app.get("/memories/inbox")
    async def inbox_page(request: Request):
        try:
            session = await guard(request)
        except HTTPException as exc:
            if exc.status_code == 401:
                return RedirectResponse("/user-auth/login",303)
            raise
        return HTMLResponse(TEMPLATES.get_template("inbox.html").render(name=session["name"],csrf=session["csrf"],is_admin=session.get("is_admin",False)))

    @app.get("/memories/api/inbox")
    async def inbox_listing(request: Request, status: str = "pending", page: int = 1):
        await guard(request)
        if set(request.query_params) - {"status", "page"} or not 1 <= page <= 100000:
            raise HTTPException(400,"Unsupported inbox fields")
        return await inbox_operation(user_inbox_list,status,page)

    @app.get("/memories/api/inbox/{candidate_id}")
    async def inbox_candidate(candidate_id: str, request: Request):
        await guard(request)
        if request.query_params:
            raise HTTPException(400,"Unsupported inbox fields")
        return await inbox_operation(user_inbox_get,candidate_id)

    @app.post("/memories/api/inbox/{candidate_id}/{decision}")
    async def inbox_decide(candidate_id: str, decision: str, request: Request):
        session = await guard(request,True)
        if decision not in {"approve", "reject"}:
            raise HTTPException(404)
        data = await payload(request, max_bytes=262144)
        return await inbox_operation(user_inbox_decide,candidate_id,decision,data,reviewer=session["store"])

    @app.post("/memories/api/{action}")
    async def change(action: str, request: Request):
        session = await guard(request,True)
        if action not in {"edit", "retract"}:
            raise HTTPException(404)
        data = await payload(request, max_bytes=262144)
        return await operation(user_memory.change,action,data,subject=session["sub"])


async def operation(fn,*args,**kwargs):
    try:
        return await asyncio.to_thread(fn,*args,**kwargs)
    except RevisionConflict as exc:
        raise HTTPException(409,str(exc)) from None
    except FileNotFoundError:
        raise HTTPException(404,"Memory not found") from None
    except PermissionError:
        raise HTTPException(403,"You can access only your own memories") from None
    except (StoreUnavailable,CommitOutcomeUnknown):
        raise HTTPException(503,"Your memory store needs attention. Reload before retrying or contact your administrator.") from None
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from None
    except Exception:
        # Filesystem paths, git remote credentials and memory contents stay off the wire.
        raise HTTPException(503,"The memory service could not complete the operation. Reload before retrying.") from None


INBOX_EDITS = {"title", "body", "tags", "description", "importance"}


def user_inbox_list(status, page):
    from memd import inbox
    cfg = user_memory.owner_config()
    result = inbox.list_candidates(cfg, cfg.profile, status=status, limit=25, offset=(page-1)*25)
    return {**result, "page": page, "pages": max(1, (result["total"]+24)//25)}


def user_inbox_get(candidate_id):
    from memd import inbox
    cfg = user_memory.owner_config()
    return inbox.get(cfg, cfg.profile, candidate_id)


def user_inbox_decide(candidate_id, decision, data, *, reviewer):
    """Approve (with edits) or reject a candidate in the signed-in user's own store."""
    from memd import access, inbox
    cfg = user_memory.owner_config()
    if decision == "reject":
        if set(data) - {"reason"}:
            raise ValueError("Unsupported inbox fields")
        return inbox.reject(cfg, cfg.profile, candidate_id, reviewer=reviewer, reason=data.get("reason") or "")
    edits = data.get("edits") or {}
    if set(data) - {"edits"} or not isinstance(edits, dict) or set(edits) - INBOX_EDITS:
        raise ValueError("Only the title, summary, memory text, tags and importance can be edited")
    for key, maximum in (("title", 300), ("body", user_memory.MAX_BODY), ("description", 1000)):
        if key in edits and (not isinstance(edits[key], str) or len(edits[key]) > maximum
                             or (key != "description" and not edits[key].strip())):
            raise ValueError(f"Invalid {key}; maximum {maximum} characters")
    tags = edits.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 30 or not all(isinstance(t, str) and 0 < len(t.strip()) <= 80 for t in tags):
        raise ValueError("Use at most 30 tags of up to 80 characters")
    # The console session is the caller: its bound identity selects the store,
    # exactly as an authenticated bearer's does for save.
    access.delegated.set(True)
    return inbox.approve(cfg, cfg.profile, candidate_id, reviewer=reviewer, edits=edits)


async def inbox_operation(fn,*args,**kwargs):
    from memd import inbox

    def decided_is_conflict(*a, **k):
        try:
            return fn(*a, **k)
        except inbox.NotPending as exc:
            raise RevisionConflict(str(exc)) from None
    return await operation(decided_is_conflict,*args,**kwargs)


def token_operation(fn,*args,**kwargs):
    try:
        return fn(*args,**kwargs)
    except Denied as exc:
        raise HTTPException(403,str(exc)) from None
    except Conflict as exc:
        raise HTTPException(409,str(exc)) from None
    except ControlError as exc:
        raise HTTPException(400,str(exc)) from None
