"""Browser account, settings and administration routes."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, Response

from memd import access, control
from memd.config import default_profile


def available():
    if not control.enabled():
        raise HTTPException(503,"Administration is not configured on this server.")


def signed_in():
    available()
    p = access.current.get()
    if not p:
        raise HTTPException(401,"Sign in to continue.")
    return p


def administrator():
    p = signed_in()
    if not p.admin or not p.session:
        raise HTTPException(403,"An administrator account is required. Agent tokens cannot administer this server.")
    return p


def own_account():
    p = signed_in()
    if not p.session:
        raise HTTPException(403,"Sign in with an account to manage settings.")
    return p


def view_identity(p):
    stores = control.stores() if control.enabled() else {}
    return {"ok":True,"enabled":control.enabled(),"authenticated":bool(p),
            "username":p.label if p else None,"user_id":p.id if p else None,
            "admin":bool(p and p.admin),"session":bool(p and p.session),
            "csrf":p.csrf if p and p.session else "", "default_store":p.default if p else "",
            "stores":[{"id":key,"name":stores.get(key,{}).get("name",key),"permission":permission}
                      for key,permission in (p.grants.items() if p else [])]}


def install(app):
    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(_request, _exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409,content={"detail":"That name already exists or the resource is in use."})

    @app.get("/ui/me")
    def me():
        return view_identity(access.current.get())

    @app.post("/ui/login",dependencies=[Depends(available)])
    def login(payload:dict, request:Request, response:Response):
        origin = request.headers.get("origin", "")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(403,"Sign in from this server's own page.")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403,"Cross-site login is not allowed.")
        username = str(payload.get("username", "")).lower().strip()[:100]
        password = payload.get("password", "")
        key = control.digest((request.client.host if request.client else "local") + ":" + username)
        now = time.time()
        with control.db() as c:
            attempt = c.execute("SELECT * FROM login_attempts WHERE key=?",(key,)).fetchone()
            if attempt and attempt["since"] > now - 900 and attempt["failures"] >= 10:
                raise HTTPException(429,"Too many attempts. Try again in 15 minutes.")
            user = c.execute("SELECT * FROM users WHERE username=?",(username,)).fetchone()
            # A fixed valid hash avoids a fast unknown-user path.
            # Must carry the CURRENT cost: a legacy 3-part dummy verifies ~9x
            # faster than a real n=2**17 hash, which is a username-enumeration
            # oracle. Any change to the cost must update this in lockstep.
            hashed = (user["password"] if user else
                      f"scrypt${control.SCRYPT_N}$unknown$" + "0"*128)
            valid = control.password_matches(password,hashed) and user is not None and not user["disabled"]
            if not valid:
                if not attempt or attempt["since"] < now-900:
                    c.execute("INSERT OR REPLACE INTO login_attempts VALUES(?,1,?)",(key,now))
                else:
                    c.execute("UPDATE login_attempts SET failures=failures+1 WHERE key=?",(key,))
            else:
                c.execute("DELETE FROM login_attempts WHERE key=?",(key,))
                # Upgrade a hash stored at an older scrypt cost. Only possible
                # here, where the plaintext is in hand; without it an existing
                # account keeps its weaker hash forever and raising the cost
                # protects nobody who already has a password.
                if control.needs_rehash(hashed):
                    c.execute("UPDATE users SET password=? WHERE id=?",
                              (control.password_hash(password), user["id"]))
                    control.audit(c,user["username"],"password.rehash",user["id"])
                c.execute("DELETE FROM sessions WHERE expires<?",(now,))
                session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                c.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",(control.digest(session),user["id"],csrf,now,now+43200))
                control.audit(c,user["username"],"login",user["id"])
                principal = access.user_principal(c,user,session=True,csrf=csrf)
        if not valid:
            raise HTTPException(401,"Username or password not accepted.")
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie("memd_session",session,httponly=True,secure=secure,samesite="strict",max_age=43200,path="/")
        response.headers["Cache-Control"] = "no-store"
        return view_identity(principal)

    @app.post("/ui/logout")
    def logout(request:Request,response:Response):
        if control.enabled():
            value = request.cookies.get("memd_session", "")
            with control.db() as c:
                c.execute("DELETE FROM sessions WHERE digest=?",(control.digest(value),))
        response.delete_cookie("memd_session",path="/")
        return {"ok":True}

    @app.post("/ui/password")
    def password(payload:dict,p=Depends(own_account)):
        with control.db() as c:
            user = c.execute("SELECT * FROM users WHERE id=?",(p.id,)).fetchone()
            if not control.password_matches(payload.get("current", ""),user["password"]):
                raise HTTPException(400,"Current password not accepted.")
            try:
                hashed = control.password_hash(payload.get("password"))
            except ValueError as exc:
                raise HTTPException(400,str(exc))
            c.execute("UPDATE users SET password=? WHERE id=?",(hashed,p.id))
            c.execute("DELETE FROM sessions WHERE user_id=?",(p.id,))
            control.audit(c,p.label,"password.change",p.id)
        return {"ok":True}

    @app.get("/admin/overview")
    def overview(p=Depends(administrator)):
        from memd.mcp_http import load_tokens
        import os
        control.refresh_legacy_files()
        tokens = control.token_records(load_tokens(),(os.environ.get("MEMD_PROFILE") or default_profile()))
        with control.db() as c:
            # `access` is the store access a token issued for this user can
            # reach, from the same function that authorizes its requests; the
            # token form lists additional stores from it (issue_token checks).
            users = []
            for r in c.execute("SELECT id,username,role,disabled,created FROM users ORDER BY username").fetchall():
                principal = access.user_principal(c,r)
                users.append({**dict(r),"access":dict(principal.grants) if principal else {}})
            grants = [dict(r) for r in c.execute("SELECT * FROM grants")]
            # Read entries would bury the admin events; they stay queryable in the table.
            marks = ",".join("?" * len(control.READ_AUDIT_ACTIONS))
            audit = [dict(r) for r in c.execute(
                f"SELECT * FROM audit WHERE action NOT IN ({marks}) ORDER BY id DESC LIMIT 100",
                control.READ_AUDIT_ACTIONS)]
            jobs = [dict(r) for r in c.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 30")]
        return {"ok":True,"users":users,"grants":grants,"tokens":tokens,"audit":audit,"jobs":jobs,
                "stores":[control.public_store(s) for s in control.stores().values()]}

    @app.post("/admin/users")
    def add_user(payload:dict,p=Depends(administrator)):
        try:
            temporary = secrets.token_urlsafe(20)
            uid = control.create_user(payload.get("username"),temporary,payload.get("role","member"),actor=p.label)
        except ValueError as exc:
            raise HTTPException(400,str(exc))
        return {"ok":True,"id":uid,"temporary_password":temporary}

    @app.patch("/admin/users/{uid}")
    def edit_user(uid:str,payload:dict,p=Depends(administrator)):
        with control.db() as c:
            c.execute("BEGIN IMMEDIATE")
            user = c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()
            if not user:
                raise HTTPException(404,"Unknown user.")
            role = payload.get("role",user["role"])
            disabled = payload.get("disabled",bool(user["disabled"]))
            if role not in {"admin","member"} or not isinstance(disabled,bool):
                raise HTTPException(400,"Invalid role or enabled state.")
            if uid == p.id and (disabled or role != "admin"):
                raise HTTPException(400,"You cannot disable or demote your current administrator account.")
            if user["role"] == "admin" and (disabled or role != "admin"):
                others = c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0 AND id<>?",(uid,)).fetchone()[0]
                if not others:
                    raise HTTPException(400,"Keep at least one active administrator.")
            c.execute("UPDATE users SET role=?,disabled=? WHERE id=?",(role,int(disabled),uid))
            if disabled:
                c.execute("DELETE FROM sessions WHERE user_id=?",(uid,))
            control.audit(c,p.label,"user.update",user["username"])
        return {"ok":True}

    @app.post("/admin/users/{uid}/reset-password")
    def reset_password(uid:str,p=Depends(administrator)):
        temporary = secrets.token_urlsafe(20)
        with control.db() as c:
            if not c.execute("UPDATE users SET password=? WHERE id=?",(control.password_hash(temporary),uid)).rowcount:
                raise HTTPException(404,"Unknown user.")
            c.execute("DELETE FROM sessions WHERE user_id=?",(uid,))
            control.audit(c,p.label,"password.reset",uid)
        return {"ok":True,"temporary_password":temporary}

    @app.put("/admin/users/{uid}/grants")
    def update_grants(uid:str,payload:dict,p=Depends(administrator)):
        grants = payload.get("grants",{})
        known = control.stores()
        if not isinstance(grants,dict) or any(key not in known or value not in {"read","write"} for key,value in grants.items()):
            raise HTTPException(400,"Choose existing stores and read/write permissions.")
        with control.db() as c:
            if not c.execute("SELECT id FROM users WHERE id=?",(uid,)).fetchone():
                raise HTTPException(404,"Unknown user.")
            c.execute("DELETE FROM grants WHERE user_id=?",(uid,))
            c.executemany("INSERT INTO grants VALUES(?,?,?)",[(uid,key,value) for key,value in grants.items()])
            control.audit(c,p.label,"grants.update",uid)
        return {"ok":True}

    @app.post("/admin/tokens")
    def add_token(payload:dict,p=Depends(administrator)):
        try:
            result = control.issue_token(payload.get("label"),payload.get("user_id"),payload.get("store_id"),payload.get("scope","read"),payload.get("expires"),actor=p.label,extra_stores=payload.get("extra_stores"))
        except ValueError as exc:
            raise HTTPException(400,str(exc))
        return {"ok":True,**result}

    @app.delete("/admin/tokens/{tid}")
    def revoke_token(tid:str,p=Depends(administrator)):
        with control.db() as c:
            if not c.execute("UPDATE tokens SET revoked=COALESCE(revoked,?) WHERE id=?",(time.time(),tid)).rowcount:
                raise HTTPException(404,"Unknown token.")
            control.audit(c,p.label,"token.revoke",tid)
        return {"ok":True}

    @app.get("/ui/stores")
    def list_stores(p=Depends(signed_in)):
        return {"ok":True,"stores":[{"id":key,"name":control.stores().get(key,{}).get("name",key),"permission":scope}
                                     for key,scope in p.grants.items()]}

    @app.get("/ui/export")
    def export_store(profile:str | None=None,p=Depends(signed_in)):
        from memd.profiles import resolve_profile,guard_paths
        from memd.config import Config
        from memd.store import list_notes,clone_lock,dump_note
        import io,os,zipfile
        selected = resolve_profile(profile)
        # A full-corpus download is the single highest-value read to have a
        # record of; it was previously indistinguishable from no activity.
        control.audit_read("export", selected)
        cfg = Config.from_env({**os.environ,"MEMD_PROFILE":selected},env_file=None)
        clone,_ = guard_paths(selected,cfg.clone,cfg.db)
        buffer = io.BytesIO()
        with clone_lock(clone),zipfile.ZipFile(buffer,"w",zipfile.ZIP_DEFLATED) as archive:
            from pathlib import Path
            from memd.save import note_filename
            names = set()
            for note in list_notes(clone):
                name = Path(note.path).relative_to(clone).as_posix()
                if name.endswith(".md.enc"):
                    # An encrypted store exports readable Markdown under the
                    # note's slug-derived name, never the opaque stored name.
                    name = note_filename(note.slug)
                    while name in names:
                        name = name[:-3] + "_.md"
                names.add(name)
                archive.writestr(name,dump_note(note))
        return Response(buffer.getvalue(),media_type="application/zip",headers={"Content-Disposition":f'attachment; filename="{selected}-memories.zip"',"Cache-Control":"no-store"})

    @app.get("/admin/stores/{profile}/conflicts")
    def vault_conflicts(profile:str,p=Depends(administrator)):
        from memd.sources import conflicts
        try:
            return {"ok":True,"conflicts":conflicts(profile)}
        except ValueError as exc:
            raise HTTPException(400,str(exc))

    @app.post("/admin/stores/{profile}/resolve-conflict")
    def resolve_vault_conflict(profile:str,payload:dict,p=Depends(administrator)):
        from memd.sources import resolve_conflict
        try:
            resolve_conflict(profile,payload,actor=p.label)
            return {"ok":True}
        except ValueError as exc:
            raise HTTPException(409,str(exc))

    @app.post("/admin/stores")
    def create_store(payload:dict,p=Depends(administrator)):
        from memd.sources import create_store as create
        try:
            return {"ok":True,"store":create(payload,actor=p.label)}
        except ValueError as exc:
            raise HTTPException(400,str(exc))

    @app.put("/admin/stores/{profile}")
    def edit_store(profile:str,payload:dict,p=Depends(administrator)):
        from memd.sources import update_store
        try:
            return {"ok":True,"store":update_store(profile,payload,actor=p.label)}
        except ValueError as exc:
            raise HTTPException(400,str(exc))

    @app.post("/admin/stores/{profile}/{action}")
    def store_action(profile:str,action:str,p=Depends(administrator)):
        from memd.sources import start_job
        if action not in {"test","sync","reindex"}:
            raise HTTPException(404,"Unknown operation.")
        if not control.store(profile):
            raise HTTPException(404,"Unknown store.")
        try:
            return {"ok":True,"job":start_job(profile,action,p.label)}
        except ValueError as exc:
            raise HTTPException(409,str(exc))
