"""Request identities shared by REST, browser sessions and MCP tools."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from http.cookies import SimpleCookie
import json
import os
import time

from memd import control
from memd.config import default_profile


@dataclass(frozen=True)
class Principal:
    id: str
    label: str
    admin: bool
    grants: dict[str,str]
    default: str
    session: bool = False
    csrf: str = ""
    token_id: str = ""


current: ContextVar[Principal | None] = ContextVar("memd_principal", default=None)
remote_request: ContextVar[bool] = ContextVar("memd_remote", default=False)
# True when the caller was authenticated by the token-registry/Kasm/OIDC layer
# (memd.authentication) rather than an administered account. That layer then
# decides the store: memd.registry.enforce and the bound identity in
# profiles.resolve_profile.
delegated: ContextVar[bool] = ContextVar("memd_delegated", default=False)


def user_principal(c, user, *, session=False, csrf=""):
    if user["disabled"]:
        return None
    admin = user["role"] == "admin"
    grants = ({row[0]: "write" for row in c.execute("SELECT id FROM stores")} if admin else
              {r[0]:r[1] for r in c.execute("SELECT store_id,permission FROM grants WHERE user_id=?", (user["id"],))})
    serving = (os.environ.get("MEMD_PROFILE") or default_profile())
    default = serving if serving in grants else next(iter(grants), "")
    return Principal(user["id"],user["username"],admin,grants,default,session,csrf)


def authenticate_token(value):
    if not value:
        return None
    hashed = control.digest(value)
    now = time.time()
    if control.enabled():
        with control.db() as c:
            record = c.execute("SELECT * FROM tokens WHERE digest=?",(hashed,)).fetchone()
            if record:
                if record["revoked"] or (record["expires"] and record["expires"] <= now):
                    return None
                if not record["legacy"]:
                    user = c.execute("SELECT * FROM users WHERE id=?",(record["user_id"],)).fetchone()
                    if not user or user["disabled"]:
                        return None
                    principal = user_principal(c,user)
                    scope = principal.grants.get(record["store_id"])
                    if not scope:
                        return None
                    permission = "write" if scope == record["scope"] == "write" else "read"
                    grants = {record["store_id"]: permission}
                    # Additional stores (memd.share): the same intersection with
                    # the owner's CURRENT grants, so removing a grant removes the
                    # token's reach there too. A malformed value grants nothing.
                    try:
                        extra = json.loads(record["extra"] or "{}") if "extra" in record.keys() else {}
                    except ValueError:
                        extra = {}
                    for key, wanted in (extra.items() if isinstance(extra, dict) else ()):
                        held = principal.grants.get(key)
                        if held and key not in grants and wanted in {"read", "write"}:
                            grants[key] = "write" if held == wanted == "write" else "read"
                    c.execute("UPDATE tokens SET last_used=? WHERE id=?",(now,record["id"]))
                    return Principal(user["id"],record["label"],False,grants,record["store_id"],token_id=record["id"])
    # Legacy credentials retain only the serving instance's store, never admin.
    # MEMD_CONTROL_DB is a one-way cutover: once the token registry is on, the
    # legacy file is never consulted again (docs/ADMIN-WEB.md).
    from memd.registry import configured as registry_configured
    if registry_configured():
        return None
    from memd.mcp_http import load_tokens
    import hmac
    for candidate, label in load_tokens().items():
        if hmac.compare_digest(candidate.encode(), value.encode()):
            profile = (os.environ.get("MEMD_PROFILE") or default_profile())
            if control.enabled():
                with control.db() as c:
                    c.execute("INSERT OR IGNORE INTO tokens(id,digest,label,store_id,scope,masked,legacy,created) VALUES(?,?,?,?,?,?,1,?)",
                              (hashed[:24],hashed,label,profile,"write","••••"+value[-4:],now))
                    c.execute("UPDATE tokens SET last_used=? WHERE digest=?",(now,hashed))
            return Principal("legacy:"+hashed[:12],label,False,{profile:"write"},profile,token_id=hashed[:24])
    return None


def authenticate_headers(headers):
    authorization = headers.get("authorization", "")
    if authorization:
        return authenticate_token(authorization[7:].strip()) if authorization.startswith("Bearer ") else None
    if not control.enabled():
        return None
    cookies = SimpleCookie()
    try:
        cookies.load(headers.get("cookie", ""))
        value = cookies.get("memd_session")
    except Exception:
        return None
    if value:
        with control.db() as c:
            row = c.execute("SELECT * FROM sessions WHERE digest=? AND expires>?",(control.digest(value.value),time.time())).fetchone()
            if row:
                user = c.execute("SELECT * FROM users WHERE id=?",(row["user_id"],)).fetchone()
                if user:
                    return user_principal(c,user,session=True,csrf=row["csrf"])
    return None


def _unauthenticated_remote_allowed() -> bool:
    """Explicit opt-in for the pre-registry, open-read deployment style."""
    return os.environ.get("MEMD_ALLOW_UNAUTHENTICATED", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def authorize(profile=None, *, write=False):
    principal = current.get()
    if principal is None:
        if remote_request.get() and not delegated.get():
            # FAIL CLOSED. This used to also require control.enabled(), which is
            # only bool(os.environ["MEMD_ADMIN_DB"]) -- so a deployment that
            # never set or lost that one variable served every remote caller
            # unauthenticated, with nothing in the logs to say which mode was
            # live. An absent control database is a misconfiguration; it should
            # break loudly, not open the corpus quietly.
            if not _unauthenticated_remote_allowed():
                raise PermissionError("Sign in or supply a valid access token.")
        return profile  # Local CLI/stdio, or an explicitly open configuration.
    selected = profile or principal.default
    scope = principal.grants.get(selected)
    if not scope or (write and scope != "write"):
        raise PermissionError("This identity does not have the required store access.")
    return selected


class AccessMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope,receive,send)
        headers = {k.decode().lower():v.decode() for k,v in scope.get("headers",[])}
        # Run expensive/disk-backed authentication off the event loop.
        import asyncio
        principal = await asyncio.to_thread(authenticate_headers,headers)
        if principal and principal.session and scope["method"] not in {"GET","HEAD","OPTIONS"}:
            import hmac
            if not hmac.compare_digest(headers.get("x-csrf-token",""),principal.csrf):
                return await self.reject(send,403,"Refresh the page before making changes (CSRF check failed).")
        marker = current.set(principal)
        remote = remote_request.set(True)
        async def no_cache(message):
            if message["type"] == "http.response.start" and principal:
                existing = [(k,v) for k,v in message.get("headers",[]) if k.lower() != b"cache-control"]
                message = {**message,"headers":existing+[(b"cache-control",b"no-store")]}
            await send(message)
        try:
            await self.app(scope,receive,no_cache)
        finally:
            current.reset(marker); remote_request.reset(remote)

    @staticmethod
    async def reject(send,status,message):
        await send({"type":"http.response.start","status":status,"headers":[(b"content-type",b"application/json"),(b"cache-control",b"no-store")]})
        await send({"type":"http.response.body","body":json.dumps({"detail":message}).encode()})
