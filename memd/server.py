"""FastAPI surface.

  GET  /health             -> {ok, status, app_commit, checks} (detail needs a token)
  GET  /stats              -> index stats                (token)
  GET  /metrics            -> Prometheus text            (same auth as /stats; memd.metrics)
  GET  /insights           -> memory health report       (token; memd.insights)
  GET  /entities, /entities/{kind}/{name} -> entity pages (token; memd.entities)
  POST /recall {query,...} -> {notes:[...]}              (open on LAN)
  POST /timeline {subject,predicate?,at?} -> {facts:[...]}  (same auth as /recall)
  POST /ask {question,k?,stores?|scope?,host?,tags?} -> {answer,citations,...}  (same auth as /recall; memd.ask)
  POST /save  {title,...}  -> SaveResult                 (requires MEMD_TOKEN)
  POST /propose {title,...} -> {id, lint}               (same auth as /save; memd.inbox)
  GET  /handoff?repo=      -> {handoff: {text,as_of,...}} (same auth as /propose; memd.handoff)
  POST /publish {slug,target_store,store?} -> receipt   (read on source + write on target; memd.share)
  GET  /inbox, /inbox/{id}; POST /inbox/{id}/approve|reject  (account session; memd.inbox)
  POST /reindex {profile?,pull?} -> {ok,notes,head,pulled}   (requires MEMD_TOKEN)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import dataclasses
import subprocess
import sys
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

import sqlite_vec
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Query

from memd.registry import configured as control_configured, enforce, Denied
from memd.actor import set_actor
from memd.config import Config, DEFAULT_PROFILE
from memd.embed import DIM, CANARY_STRING, embed_with_deadline
from memd.index import open_db, reindex, head_in_index, set_head_in_index, lexical_head_in_index, pending_vectors
from memd.mcp_http import build_mcp_app, check_bearer, load_tokens
from memd.normalize import NormalizeError, normalize_fact, normalize_recall_args
from memd.profiles import guard_paths, locked_profile, resolve_profile, ProfileMismatch, UnknownProfile, effective_environment
from memd.read import read as read_memory, ReadRevisionConflict, ReadUnavailable
from memd.render import render_result
from memd import refresh, usage
from memd.recall import recall
from memd.save import save
from memd.rerank import rerank
from memd.store import git_head_sha, clone_lock, assert_readable_tree

# Module-level health cache: (timestamp, result). Single profile per process.
_health_cache: tuple[float, dict] | None = None
_HEALTH_TTL = 15.0


def _cfg(profile: str | None = None) -> Config:
    """Config for a request. When a profile is given, thread it through so the
    profile is authoritative for clone/db selection (§9.5 hard isolation) and the
    recall/save guard resolves the right isolated paths."""
    from memd.store_bootstrap import ensure_store

    if profile is None:
        return Config.from_env()
    env = effective_environment()
    env["MEMD_PROFILE"] = profile
    cfg = Config.from_env(env, env_file=None)
    # See memd.mcp._cfg_for: per-user stores are created on first use.
    ensure_store(cfg)
    return cfg


def _lock_profile() -> str | None:
    return locked_profile()


def _resolve_profile(requested: str | None, operation: str = "read") -> str:
    try:
        return resolve_profile(enforce(operation, requested))
    except (ProfileMismatch, Denied) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except UnknownProfile as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _health_dim(cfg: Config) -> int:
    # Dim is asserted by the startup canary; report the configured value.
    return cfg.embed_dim


# ---------------------------------------------------------------------------
# Root-path MCP dispatch.
#
# WHY: a bare-hostname MCP client is configured with just the hostname and no
# `/mcp/` path to remember, so https://memd.example.com itself must be a valid MCP
# endpoint. We add an ASGI middleware so the dispatch happens *before* FastAPI
# routing, without changing create_token_app()'s FastAPI return type.
#
# GET is ambiguous at `/` (a human browser/curl GET must still hit the existing
# index()), so the discriminator is the `Accept` header: a GET whose Accept
# contains text/event-stream is an MCP event-stream request; any non-GET method
# (POST/DELETE — MCP uses DELETE to end a session) is treated as MCP. The scope
# is handed straight to the MCP ASGI app, which does not care about the path,
# rather than rewriting the path to /mcp and re-dispatching into FastAPI.
# ---------------------------------------------------------------------------


class RootMcpDispatch:
    """ASGI middleware that routes the bare root path to the MCP app.

    Treats the request as MCP when the path is exactly ``/`` (or empty) and:
      - the method is not GET (POST, DELETE — MCP uses DELETE to end a session),
        OR
      - the method is GET and the Accept header contains text/event-stream.

    Everything else at ``/`` falls through to the wrapped FastAPI app (the
    human index). Non-http scopes (lifespan) and any other path are passed
    through untouched.
    """

    def __init__(self, app: Any, mcp_app: Any) -> None:
        self.app = app
        self.mcp_app = mcp_app

    async def __call__(
        self, scope: dict[str, Any], receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        # Pass through any non-http scope (e.g. lifespan) untouched.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        # Only the bare root is in scope for dispatch.
        if path not in ("", "/"):
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        accept = ""
        headers = scope.get("headers", []) or []
        for name, value in headers:
            if isinstance(name, (bytes, bytearray)):
                name = name.decode("latin-1")
            if name.lower() == "accept":
                accept = value if isinstance(value, str) else (
                    value.decode("latin-1") if isinstance(value, (bytes, bytearray))
                    else str(value)
                )
                break

        is_mcp = (method != "GET") or ("text/event-stream" in accept.lower())

        if is_mcp:
            # Hand the scope straight to the MCP ASGI app; it does not care
            # about the path, so no rewrite to /mcp is performed.
            await self.mcp_app(scope, receive, send)
        else:
            await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# The single shipped app factory. Exposes the module-level core seam names
# (`_core_recall`, `_core_save`, `_health`) and enforces the write token via ONE
# `require_token` dependency (`Authorization: Bearer <MEMD_TOKEN>`). There is no
# second auth implementation — a prior header-token `create_app` factory was
# removed so there is exactly one auth code path to reason about.
# ---------------------------------------------------------------------------


def _core_recall(query, profile="amber", k=8, include_core=True, **filters):
    """Default recall used by the module-level app (tests monkeypatch this).

    Threads the request's profile into Config so the profile is authoritative
    for clone/db selection and the recall guard resolves the right isolated db.
    include_core=False returns query-relevant notes only (for callers that load
    the carved MEMORY.md core separately, e.g. Hermes/the auto-recall hook).
    """
    return recall(query, profile=profile, k=int(k), cfg=_cfg(profile),
                  include_core=include_core, **filters)


def _core_timeline(subject, predicate=None, at=None, profile="amber"):
    """Default timeline lookup used by the module-level app (tests monkeypatch this)."""
    from memd.facts import timeline
    return timeline(subject, predicate, at, profile=profile, cfg=_cfg(profile))


def _recall_db_path(profile: str):
    """The profile's index path for recall's read-only current-facts block."""
    cfg = _cfg(profile)
    return guard_paths(profile, cfg.clone, cfg.db)[1]


def _log_recall(profile: str, query: str, shaped: dict) -> str | None:
    """Record a served recall in the store's usage log; its recall_id, or None."""
    try:
        return usage.record_recall(_cfg(profile), profile, query, shaped)
    except Exception:
        return None     # usage logging must never fail a recall


def _core_save(fact, profile="amber"):
    """Default save used by the module-level app (tests monkeypatch this)."""
    return save(fact, profile=profile, cfg=_cfg(profile))


def _core_propose(fact, profile="amber", *, source="agent"):
    """Default inbox proposal used by the module-level app (memd.inbox)."""
    from memd import inbox
    from memd.actor import get_actor
    return inbox.propose(fact, profile, cfg=_cfg(profile), source=source, proposer=get_actor())


def _core_save_with_cfg(fact, profile="amber", cfg=None):
    """Inbox approval's save: the same `_core_save` seam as POST /save."""
    return _core_save(fact, profile=profile)


def _core_reindex(profile: str = "amber", pull: bool = True) -> dict:
    """Default reindex used by the module-level app (tests monkeypatch this).

    Bring a profile's index up to date: optionally ``git pull --rebase
    --autostash`` the clone, then reindex. Serialized on the clone lock so
    it never interleaves with a concurrent ``save()`` / ``reflect`` (fix-group
    4a).
    """
    cfg = _cfg(profile)
    clone, db_path = guard_paths(profile, cfg.clone, cfg.db)
    cfg = dataclasses.replace(cfg, clone=clone, db=db_path, profile=profile)
    pulled = False
    if pull:
        with clone_lock(cfg.clone):
            subprocess.run(
                ["git", "-C", str(cfg.clone), "pull", "--rebase", "--autostash"],
                check=True, capture_output=True, text=True, timeout=30,
            )
            pulled = True
    # reindex owns short snapshot/apply locks and releases them while embedding.
    db = open_db(cfg.db, dim=cfg.embed_dim)
    try:
        n = reindex(db, cfg)
        head = head_in_index(db)
        lexical_head = lexical_head_in_index(db)
        pending = pending_vectors(db)
    finally:
        db.close()
    return {"ok": True, "profile": profile, "notes": n, "head": head,
            "lexical_head": lexical_head, "pending_vectors": pending, "pulled": pulled}



def _truncate(s: str, n: int = 120) -> str:
    if len(s) <= n:
        return s
    return s[:n - 1] + "…"


def _app_commit() -> str:
    """The commit of the CODE this server is running -- not the notes clone.

    /health.head is the notes clone's git head, which says nothing about the
    deployed application version. Without this field a deployment can keep
    running a stale image for weeks: nothing observable names the running
    code. memd-maint's drift check (design doc
    2026-08-21-memd-maint-design.md) compares this against the remote's main
    HEAD over plain HTTP, so deploy drift is caught daily with no SSH involved.

    Resolution order: $MEMD_GIT_SHA (stamped by deploy.sh at image build),
    else the checkout's own .git next to the package, else "unknown".
    """
    sha = os.environ.get("MEMD_GIT_SHA", "").strip()
    if sha:
        return sha
    try:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


# Resolved once: the commit cannot change while the process lives.
_APP_COMMIT = _app_commit()


ENCRYPTION_CHECK_FAILED = "encryption check failed; see server log"
# The last encryption failure logged per clone, so a monitor polling /health
# logs a failure once (and again when its reason changes), not every probe.
_encryption_errors: dict[str, str] = {}


def _log_encryption_failure(clone: str, error: Exception) -> None:
    """Log why a store's encryption check failed. Codec and key errors never
    carry key material (memd.crypt); they may name the key file or a note file,
    which is why this stays in the server log."""
    reason = str(error) if isinstance(error, RuntimeError) else f"{type(error).__name__}: {error}"
    if _encryption_errors.get(clone) != reason:
        _encryption_errors[clone] = reason
        logging.getLogger(__name__).warning("encryption check failed for store at %s: %s", clone, reason)


def _health(force: bool = False, profile: str | None = None) -> dict:
    """GET /health body.

    Probes the index, embed, rerank, and git backends and returns a structured
    status dict. Results are cached for 15 seconds (single profile per process);
    pass force=True to bypass the cache.
    """
    global _health_cache
    if not force and profile is None:
        cached = _health_cache
        if cached is not None:
            ts, result = cached
            if (time.monotonic() - ts) < _HEALTH_TTL:
                return result

    cfg = _cfg(profile) if profile is not None else _cfg()
    dim = _health_dim(cfg)
    checks: dict[str, dict] = {}

    # --- git check -------------------------------------------------------
    git_head = None
    git_sync = False
    git_ok = False
    git_detail = ""
    try:
        assert_readable_tree(cfg.clone)
        git_head = git_head_sha(cfg.clone)
        git_ok = bool(git_head)
        git_detail = "ok" if git_ok else "Git HEAD unavailable"
    except Exception as e:
        git_ok = False
        git_detail = _truncate(f"git_head_sha error: {e}")
        git_head = None

    # --- encryption check ------------------------------------------------
    # Only for a store that is (or is configured to be) encrypted. A missing,
    # wrong or too-permissive key, or a tree the codec refuses (plaintext notes
    # or mem-carve output beside the envelopes), fails closed: nothing can be
    # read or written. /health is unauthenticated and the reason can name the
    # key file or a note file, so it goes to the server log (and `mem-crypt
    # status`), never into this response.
    crypt_ok = True
    try:
        from memd.codec import codec_for, is_encrypted, key_binding
        if cfg.clone is not None and (is_encrypted(cfg.clone) or key_binding(cfg.clone).key_file):
            codec = codec_for(cfg.clone)
            codec.note_files()
            checks["encryption"] = {"ok": True, "key_id": codec.key_id,
                                    "detail": "notes encrypted (AES-256-GCM)"}
            _encryption_errors.pop(str(cfg.clone), None)
    except Exception as e:
        crypt_ok = False
        _log_encryption_failure(str(cfg.clone), e)
        checks["encryption"] = {"ok": False, "key_id": None, "detail": ENCRYPTION_CHECK_FAILED}

    # --- index check -----------------------------------------------------
    index_ok = False
    index_notes = 0
    vector_pending = 0
    index_detail = ""
    try:
        db = open_db(cfg.db, dim=cfg.embed_dim)
        try:
            row = db.execute(
                "SELECT COUNT(*) FROM notes WHERE superseded_by IS NULL"
            ).fetchone()
            index_notes = row[0]
            vector_pending = pending_vectors(db)
            if index_notes == 0:
                index_ok = False
                index_detail = "no live notes in index"
                # An intentionally empty Git store is usable. A missing cache
                # over a nonempty store remains a failure, not an empty success.
                from memd.store import list_notes
                if git_ok and not any(not n.superseded_by for n in list_notes(cfg.clone)):
                    index_ok = True
                    index_detail = "empty store; ready for its first memory"
            else:
                index_ok = True
                index_detail = f"notes={index_notes}"
        finally:
            db.close()
    except Exception as e:
        index_ok = False
        index_notes = 0
        from memd.codec import CodecError
        if isinstance(e, CodecError):     # its reason stays in the log, as above
            _log_encryption_failure(str(cfg.clone), e)
            index_detail = ENCRYPTION_CHECK_FAILED
        else:
            index_detail = _truncate(f"index query error: {e}")

    checks["index"] = {
        "ok": index_ok,
        "notes": index_notes,
        "pending_vectors": vector_pending,
        "refresh": refresh.refresh_status(cfg),
        "detail": index_detail,
    }

    # --- git sync (only meaningful if git_head resolved) -----------------
    if git_ok and git_head is not None:
        try:
            db = open_db(cfg.db, dim=cfg.embed_dim)
            try:
                idx_head_val = lexical_head_in_index(db) or head_in_index(db)
            finally:
                db.close()
            git_sync = (idx_head_val == git_head)
            if not git_sync:
                git_detail = _truncate(
                    f"head mismatch: git={git_head[:8]} index={str(idx_head_val)[:8]}"
                )
        except Exception as e:
            git_sync = False
            git_detail = _truncate(
                f"index head lookup error: {e}"
            )
    else:
        git_sync = False

    checks["git"] = {
        "ok": git_ok,
        "head": git_head,
        "in_sync": git_sync,
        "detail": git_detail,
    }

    # --- embed check -----------------------------------------------------
    embed_ok = False
    embed_dim: int | None = None
    embed_ms = 0
    embed_detail = ""
    try:
        start = time.monotonic()
        vec = embed_with_deadline(CANARY_STRING, cfg=cfg, ms=2000)
        elapsed = time.monotonic() - start
        embed_ms = int(elapsed * 1000)
        if vec is not None and len(vec) == dim:
            embed_ok = True
            embed_dim = len(vec)
            embed_detail = f"ok ({embed_ms}ms)"
        else:
            embed_dim = len(vec) if vec is not None else None
            embed_ok = False
            embed_detail = _truncate(
                f"embed returned vector of length {embed_dim} (expected {dim})"
            )
    except Exception as e:
        embed_ms = 0
        embed_detail = _truncate(f"embed error: {e}")

    checks["embed"] = {
        "ok": embed_ok,
        "dim": embed_dim,
        "ms": embed_ms,
        "detail": embed_detail,
    }

    # --- rerank check ----------------------------------------------------
    rerank_ok = False
    rerank_ms = 0
    rerank_detail = ""
    try:
        probe_docs = [{"slug": "_health_probe",
                       "body": "a short synthetic document for the rerank probe"}]
        start = time.monotonic()
        result = rerank(CANARY_STRING, probe_docs, top_n=1, ms=1500, cfg=cfg)
        elapsed = time.monotonic() - start
        rerank_ms = int(elapsed * 1000)
        if result is not None:
            rerank_ok = True
            rerank_detail = f"ok ({rerank_ms}ms)"
        else:
            rerank_ok = False
            rerank_detail = "rerank returned None"
    except Exception as e:
        rerank_ms = 0
        rerank_detail = _truncate(f"rerank error: {e}")

    checks["rerank"] = {
        "ok": rerank_ok,
        "ms": rerank_ms,
        "detail": rerank_detail,
    }

    # --- assemble status -------------------------------------------------
    # overall status:
    #   "down" when index fails — recall cannot return anything useful.
    #   "degraded" when index is fine but embed/rerank failed, or git OoO.
    #   "ok" otherwise.
    if not index_ok or not crypt_ok:
        status = "down"
    elif (not embed_ok) or (not rerank_ok) or (not git_sync) or vector_pending:
        status = "degraded"
    else:
        status = "ok"

    # ok (legacy boolean) must be status != "down". Rationale: onboard.sh and
    # other callers treat a non-2xx / ok:false as "server unreachable"; a
    # degraded-but-usable server must not block onboarding.
    result = {
        "ok": status != "down",
        "status": status,
        "dim": dim,
        "head": git_head,
        "app_commit": _APP_COMMIT,
        "checks": checks,
    }

    if profile is None:
        _health_cache = (time.monotonic(), result)
    return result


def _metrics_public_request(request: Request) -> bool:
    """An unauthenticated scrape allowed by MEMD_METRICS_PUBLIC: loopback, not proxied."""
    import ipaddress
    from memd.metrics import public_enabled
    if not public_enabled() or request.client is None:
        return False
    if any(h in request.headers for h in ("x-forwarded-for", "forwarded", "x-real-ip")):
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _metrics_public_stores() -> list[str]:
    """The serving store alone, for a public loopback scrape."""
    from memd.profiles import registry
    env = effective_environment()
    serving = (env.get("MEMD_PROFILE") or "").strip() or DEFAULT_PROFILE
    try:
        serving = locked_profile() or serving
    except Exception:
        pass
    return [serving] if serving in registry() else []


def _metrics_visible_stores() -> list[str]:
    """Stores the authenticated caller could ask GET /stats about (bounded).

    Each candidate is checked with exactly the resolution /stats uses, so a
    store the caller may not see is never named in the output. A registry token
    without the stats operation sees none and is refused, as /stats refuses it.
    """
    from memd import access
    from memd.metrics import MAX_STORES
    from memd.profiles import registry
    from memd.registry import Registry, configured, principal as registry_principal
    candidates: dict[str, None] = {}
    token_id = registry_principal.get()
    if token_id is not None and configured():
        import json
        try:
            with Registry().connection() as db:
                row = db.execute("SELECT stores FROM tokens WHERE id=?", (token_id,)).fetchone()
            candidates.update(dict.fromkeys(json.loads(row[0]) if row else []))
        except Exception:
            pass
    held = access.current.get()
    if held is not None:
        candidates.update(dict.fromkeys(held.grants))
    try:
        candidates[_resolve_profile(None, "stats")] = None
    except HTTPException:
        pass
    candidates.update(dict.fromkeys(registry()))
    visible = []
    for store in list(candidates)[:4 * MAX_STORES]:
        try:
            if _resolve_profile(store, "stats") == store:
                visible.append(store)
        except Exception:       # HTTPException included: not visible to this caller
            continue
        if len(visible) >= MAX_STORES:
            break
    if token_id is not None and not visible:
        raise HTTPException(status_code=403, detail="Token does not permit stats")
    return visible


def _metrics_body(stores: list[str]) -> str:
    from memd import metrics
    gauges = {}
    for store in stores:
        try:
            # Not _cfg(): a scrape must never create a per-user store on first use.
            env = effective_environment()
            env["MEMD_PROFILE"] = store
            cfg = Config.from_env(env, env_file=None)
            clone, db_path = guard_paths(store, cfg.clone, cfg.db)
        except Exception:
            continue
        gauges[store] = metrics.store_gauges(store, clone, db_path)
    return metrics.render(_APP_COMMIT, gauges)


def _public_health(full: dict) -> dict:
    """/health for a caller without a token: pass/fail only, no detail.

    The full payload names the note count, the notes' git HEAD, the embedding
    dimension, per-service timings and error text. None of that is needed to
    decide whether a deployment is up, so an unauthenticated caller gets the
    overall status, the app build (deploy.sh proves the rollout served the
    commit it built) and each check's pass/fail. Deploy and doctor checks read
    exactly these fields; keep them stable.
    """
    checks = {}
    for name, check in (full.get("checks") or {}).items():
        if isinstance(check, dict):
            checks[name] = {"ok": check.get("ok") is True}
            if "in_sync" in check:
                checks[name]["in_sync"] = check.get("in_sync") is True
    return {"ok": full.get("ok", False), "status": full.get("status", "unknown"),
            "app_commit": full.get("app_commit"), "checks": checks}


def _health_detail_allowed(authorization: str | None) -> bool:
    """A signed-in caller, any valid token (legacy MEMD_TOKEN included), or an
    explicitly open deployment sees the full /health payload."""
    from memd import access

    if access.current.get() is not None or access._unauthenticated_remote_allowed():
        return True
    if not authorization:
        return False
    try:
        return bool(_authenticate(authorization))
    except Exception:
        return False


def _unauthorised(detail: str) -> HTTPException:
    """401 carrying the discovery header when OIDC is on.

    The MCP authorisation spec makes `WWW-Authenticate` a MUST on a 401: it is
    how a client learns WHERE to authenticate. A bare 401 leaves it with
    nowhere to go and the user with a dead server entry.
    """
    from memd.oidc import oidc_enabled, www_authenticate

    headers = {"WWW-Authenticate": www_authenticate()} if oidc_enabled() else None
    return HTTPException(status_code=401, detail=detail, headers=headers)


def _authenticate(authorization: str | None) -> str:
    from memd.authentication import authenticate
    return authenticate(authorization, check_bearer)



async def require_token(authorization: str | None = Header(default=None)) -> None:
    """Write-path guard: require a valid `Authorization: Bearer <token>`.

    ASYNC on purpose. FastAPI runs a SYNCHRONOUS dependency and a synchronous
    endpoint in two different threadpool workers, and each run_in_threadpool copies
    the context from the event loop task, never from the previous worker. A
    contextvar set here while this was `def` was therefore invisible to save_route,
    so the caller stamp silently vanished over REST while every unit test passed.
    As a coroutine this runs in the request task itself, and the endpoint inherits
    the context. tests/test_actor_over_http.py pins the behaviour.

    Accepts every token load_tokens() knows about: the MEMD_TOKEN env var (label
    "legacy") plus each per-client token in MEMD_TOKENS_FILE. The MCP path
    already accepts the per-client tokens, so REST must too -- onboard.sh hands
    every machine its own token, and one that works over MCP but 401s on /save
    or /reindex is a trap (it reads as "expired" rather than "wrong endpoint").
    """
    from memd import access, control
    from memd.oidc import oidc_enabled

    principal = access.current.get()
    if principal is not None:
        set_actor(principal.label)
        return
    if not control.enabled() and not control_configured() and not load_tokens() and not oidc_enabled():
        raise HTTPException(status_code=503, detail="MEMD_TOKEN not configured")
    label = _authenticate(authorization)
    if not label:
        raise _unauthorised("invalid or missing token")
    set_actor(label)
    access.delegated.set(True)


def _maybe_dict(obj):
    """Render a core result: prefer to_dict(), fall back to vars()."""
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return vars(obj)


def _bearer(authorization: str | None) -> str | None:
    """Token from an `Authorization: Bearer <token>` header, else None."""
    if authorization and authorization.startswith("Bearer "):
        return authorization[len("Bearer "):]
    return None


async def recall_token_guard(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """Observe-first auth for /recall.

    Async for the same reason as require_token: a contextvar set in a sync
    dependency does not reach the endpoint.

    /recall was open when memd bound to localhost. It is now LAN-exposed and
    serves every agent on every machine, so anything on the network can read the
    whole private corpus. Flipping it to required immediately would silently
    break a client -- the recall hook fails open, so an unauthorised client just
    quietly gets no memory -- and one machine could not be verified.

    So: enforce only when MEMD_REQUIRE_RECALL_TOKEN is truthy (read live, like
    _lock_profile does). Until then, serve as before but log the caller, so the
    absence of that log line is the signal that enforcing is safe.
    """
    # Same accepted-token set as require_token. Enforcing recall against only
    # the master token would 401 every per-client token onboard.sh issues and
    # blind every machine at once -- the exact failure this flag exists to avoid.
    from memd import access, control
    from memd.oidc import oidc_enabled

    principal = access.current.get()
    if principal is not None:
        set_actor(principal.label)
        return
    # Accepts a static token OR an OIDC bearer, and an OIDC bearer BINDS the
    # caller's store, so recall reads the right person's memory.
    label = _authenticate(authorization) or None
    set_actor(label or "")
    if label is not None:
        access.delegated.set(True)
    elif control.enabled():
        raise _unauthorised("invalid or missing token")
    enforcing = (os.environ.get("MEMD_REQUIRE_RECALL_TOKEN") or "").strip().lower() \
        in {"1", "true", "yes", "on"}

    if enforcing:
        if not control_configured() and not load_tokens() and not oidc_enabled():
            raise HTTPException(status_code=503, detail="MEMD_TOKEN not configured")
        if label is None:
            raise _unauthorised("invalid or missing token")
        return

    if label is None:
        # Everything arriving via memd.example.com is proxied, so request.client.host
        # is always the reverse proxy (e.g. 172.17.0.1). Prefer X-Forwarded-For so the log names the
        # actual client and the signal is actionable.
        xff = request.headers.get("x-forwarded-for")
        ip = (xff.split(",")[0].strip() if xff
              else (request.client.host if request.client else "unknown"))
        print(
            f"memd: UNAUTHENTICATED /recall from {ip} "
            "(set MEMD_REQUIRE_RECALL_TOKEN=1 to enforce)",
            file=sys.stderr, flush=True,
        )


def create_token_app() -> FastAPI:
    # MCP over streamable HTTP at /mcp, so a remote client needs no local memd
    # install. It reuses memd.mcp's server object, so the stdio and HTTP tool
    # surfaces cannot drift. The session manager must be running before it can
    # serve, hence the lifespan.
    mcp_asgi, mcp_lifespan = build_mcp_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if os.environ.get("MEMD_STARTUP_REFRESH", "1").strip().lower() not in {"0", "false", "off", "no"}:
            try:
                cfg = _cfg()
                await asyncio.to_thread(refresh.ensure_lexical, cfg)
                refresh.request_refresh(cfg)
            except Exception as exc:
                logging.getLogger(__name__).warning("startup memory refresh unavailable: %s", exc)
        from memd import control
        import threading
        stop = threading.Event()
        if control.enabled():
            from memd.sources import scheduler
            with control.db() as c:
                c.execute("UPDATE jobs SET state='failed',detail='Interrupted; retry this operation.',finished=? WHERE state IN ('queued','running') AND created<?",(time.time(),time.time()-300))
            thread = threading.Thread(target=scheduler,args=(stop,),daemon=True,name="memd-sync")
            thread.start()
        try:
            async with mcp_lifespan():
                yield
        finally:
            stop.set()

    token_app = FastAPI(title="memd", lifespan=lifespan)
    from memd import control, access
    if control.enabled():
        control.initialize()
        control.register_existing()
    from memd.admin_api import install as install_admin
    install_admin(token_app)

    @token_app.exception_handler(PermissionError)
    async def access_denied(_request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @token_app.get("/.well-known/oauth-protected-resource")
    def protected_resource_route() -> dict:
        """RFC 9728. Deliberately UNAUTHENTICATED: a client that cannot yet
        authenticate has to be able to read it, which is the whole point of the
        discovery handshake. It carries no secret, only the issuer URL."""
        from memd.oidc import oidc_enabled, protected_resource_metadata

        if not oidc_enabled():
            raise HTTPException(status_code=404, detail="OIDC is not configured")
        return protected_resource_metadata()

    @token_app.get("/health")
    def health(profile: str | None = None,
               authorization: str | None = Header(default=None)) -> dict:
        full = _health(profile=_resolve_profile(profile)) if profile else _health()
        if _health_detail_allowed(authorization):
            return full
        return _public_health(full)

    @token_app.get("/stats", dependencies=[Depends(require_token)])
    def stats_route(profile: str | None = None) -> dict:
        """Index statistics for a profile — used by dashboard tiles, Grafana, etc.

        Rebuild the config with dataclasses.replace, never by hand. A hand-built
        Config silently resets every field the call site does not name, and
        embed_dim falling back to its default makes the open_db migration guard
        drop the whole vector cache on a read-only request.
        """
        profile = _resolve_profile(profile, "stats")
        cfg = _cfg(profile)
        clone, db_path = guard_paths(profile, cfg.clone, cfg.db)
        cfg = dataclasses.replace(cfg, clone=clone, db=db_path, profile=profile)
        try:
            db = open_db(cfg.db, dim=cfg.embed_dim)
            total = db.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            vec_rows = db.execute("SELECT COUNT(*) FROM vec_notes").fetchone()[0]
            chunk_rows = db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
            pending = pending_vectors(db)
            lexical_head = lexical_head_in_index(db)
            fts_rows = db.execute("SELECT COUNT(*) FROM fts_notes").fetchone()[0]
            superseded = db.execute(
                "SELECT COUNT(*) FROM notes WHERE superseded_by IS NOT NULL"
            ).fetchone()[0]
            core = db.execute(
                "SELECT COUNT(*) FROM notes "
                "WHERE importance >= 4 AND superseded_by IS NULL"
            ).fetchone()[0]
            profile_breakdown = db.execute(
                "SELECT profile, COUNT(*) FROM notes GROUP BY profile"
            ).fetchall()
            importance_breakdown = db.execute(
                "SELECT importance, COUNT(*) FROM notes GROUP BY importance"
            ).fetchall()
            hosts = db.execute(
                "SELECT host, COUNT(*) FROM notes GROUP BY host"
            ).fetchall()
            head = db.execute(
                "SELECT value FROM meta WHERE key='head'"
            ).fetchone()
            db.close()
        except Exception as e:
            return {"ok": False, "err": str(e)}

        db_size = db_path.stat().st_size if db_path.exists() else 0

        return {
            "ok": True,
            "profile": profile,
            "notes": total,
            "vec": vec_rows,
            "vec_chunks": chunk_rows,
            "pending_vectors": pending,
            "lexical_head": lexical_head,
            "refresh": refresh.refresh_status(cfg),
            "fts": fts_rows,
            "superseded": superseded,
            "core": core,
            "size": db_size,
            "head": head[0] if head else None,
            "profiles": [{"profile": p, "count": c} for p, c in profile_breakdown],
            "importance": [{"importance": i, "count": c} for i, c in importance_breakdown],
            "hosts": [{"host": h, "count": c} for h, c in hosts],
        }

    @token_app.get("/metrics", include_in_schema=False)
    async def metrics_route(request: Request, authorization: str | None = Header(default=None)):
        """Prometheus exposition (memd.metrics); authorised like /stats.

        A stats-only (monitoring) token is enough. Store gauges cover only the
        stores the caller could ask /stats about. MEMD_METRICS_PUBLIC=1 also
        serves an unauthenticated scrape, but only from a loopback address with
        no forwarding headers (a reverse proxy on the same host is not local),
        and then only the serving store's gauges.
        """
        from fastapi.responses import PlainTextResponse
        from memd import metrics
        if authorization is None and _metrics_public_request(request):
            stores = await asyncio.to_thread(_metrics_public_stores)
        else:
            await require_token(authorization)
            stores = await asyncio.to_thread(_metrics_visible_stores)
        body = await asyncio.to_thread(_metrics_body, stores)
        return PlainTextResponse(body, media_type=metrics.CONTENT_TYPE,
                                 headers={"Cache-Control": "no-store"})

    @token_app.get("/insights", dependencies=[Depends(require_token)])
    def insights_route(profile: str | None = None,
                       limit: int = Query(default=20, ge=1, le=100),
                       fresh: bool = False):
        """Memory health report (memd.insights): read-only, cached per store for 60 s.

        Authorized as a read, not as stats: the report names notes and states
        facts, which a stats-only (monitoring) token must not see.
        """
        from fastapi.responses import JSONResponse
        from memd import insights
        profile = _resolve_profile(profile, "read")
        try:
            body = insights.report(_cfg(profile), profile, limit=limit, fresh=fresh)
        except Exception as e:
            body = {"ok": False, "err": _truncate(f"memory health unavailable: {e}", 200)}
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @token_app.get("/entities", dependencies=[Depends(require_token)])
    def entities_route(profile: str | None = None, fresh: bool = False):
        """Hosts, services and tags memory knows about, with counts (memd.entities).

        Read-only and cached per store for 30 s; authorized as a read like /insights.
        """
        from fastapi.responses import JSONResponse
        from memd import entities
        profile = _resolve_profile(profile, "read")
        try:
            body = entities.list_entities(_cfg(profile), profile, fresh=fresh)
        except Exception as e:
            body = {"ok": False, "err": _truncate(f"entities unavailable: {e}", 200)}
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @token_app.get("/entities/{kind}/{name:path}", dependencies=[Depends(require_token)])
    def entity_route(kind: str, name: str, profile: str | None = None, fresh: bool = False):
        """One entity's page: facts, timeline, notes, related, conflicts, inbox.

        Pending inbox candidates are listed only for a caller who may review this
        store's inbox (memd.inbox.reviewer_for); anyone else gets their count.
        """
        from fastapi.responses import JSONResponse
        from memd import entities, inbox
        profile = _resolve_profile(profile, "read")
        try:
            inbox.reviewer_for(profile)
            can_review = True
        except Exception:
            can_review = False
        try:
            body = entities.entity(_cfg(profile), profile, kind, name, can_review=can_review, fresh=fresh)
        except entities.UnknownEntity:
            raise HTTPException(status_code=404, detail="No such entity in this store.") from None
        except Exception as e:
            body = {"ok": False, "err": _truncate(f"entity unavailable: {e}", 200)}
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    def _federated_recall_route(payload: dict, args: dict, stores: list[str]) -> dict:
        """Recall across several granted stores (memd.share), each result labelled."""
        from memd import share

        def one(store, query, k, **kw):
            return _core_recall(query, profile=store, k=k, **kw)
        out = share.federated_recall(args, stores, one)
        shaped = out["shaped"]
        context = shaped.pop("text")
        control.audit_read("recall.federated", ",".join(map(str, stores))[:200])
        # One recall_id, logged in each store's usage log with that store's matches.
        try:
            recall_id = usage.record_federated(_cfg, args["query"], shaped)
        except Exception:
            recall_id = None
        response = {"ok": True, "profile": stores[0], "stores": out["stores"],
                    "stores_skipped": out["stores_skipped"], "context": context,
                    "rendering": shaped, "recall_id": recall_id}
        if payload.get("format") != "context":
            response["notes"] = out["notes"]
        return response

    def _federation(payload: dict, operation: str = "recall") -> list[str] | None:
        from memd import share
        if payload.get("stores") is None and payload.get("scope") in (None, ""):
            return None
        if isinstance(payload.get("profile"), str) and payload["profile"].strip():
            raise HTTPException(status_code=400, detail="send profile or stores/scope, not both")
        try:
            return share.resolve_stores(payload.get("stores"), payload.get("scope"), operation=operation)
        except share.StoreRefused as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @token_app.post("/recall", dependencies=[Depends(recall_token_guard)])
    def recall_route(payload: dict) -> dict:
        # Same tolerance as the MCP path (memd/normalize.py): an aliased or
        # missing query returns the core set instead of a 422/KeyError.
        args = normalize_recall_args(payload or {})
        stores = _federation(payload or {})
        if stores is not None:
            return _federated_recall_route(payload, args, stores)
        query = args["query"]
        profile = _resolve_profile(args.get("profile"), "recall")
        k = args["k"]
        # Keep the old notes response for existing clients; compact consumers
        # can request format=context and avoid transferring every full body.
        kw = {} if args.get("include_core", True) else {"include_core": False}
        for key in ("host", "tags", "include_archived"):
            if args.get(key):
                kw[key] = args[key]
        notes = [_maybe_dict(n) for n in _core_recall(query, profile=profile, k=k, **kw)]
        from memd.share import annotate_upstream
        annotate_upstream(notes, operation="recall", default_store=profile)
        shaped = render_result(
            notes, top_n=k, max_chars=args.get("max_chars", 14000), query=query,
            core_limit=args.get("core_limit"), include_core=args.get("include_core", True),
        )
        from memd.facts import append_current_facts
        from memd.recall import CURRENT_INTENT
        if query and CURRENT_INTENT.search(query):
            try:
                append_current_facts(shaped, query=query, db_path=_recall_db_path(profile),
                                     max_chars=args.get("max_chars", 14000))
            except Exception:
                pass    # the facts block is optional; recall itself already succeeded
        context = shaped.pop("text")
        control.audit_read("recall", f"{profile}:{shaped.get('returned_matches', 0)}"
                                     f"+{shaped.get('returned_core', 0)}")
        response = {"ok": True, "profile": profile, "context": context, "rendering": shaped,
                    "recall_id": _log_recall(profile, query, shaped)}
        if payload.get("format") != "context":
            response["notes"] = notes
        return response

    @token_app.post("/timeline", dependencies=[Depends(recall_token_guard)])
    def timeline_route(payload: dict | None = None) -> dict:
        """Read-only fact lookup; authorised exactly like /recall."""
        payload = payload or {}
        profile = _resolve_profile(payload.get("profile"), "recall")
        subject = payload.get("subject") or payload.get("query") or ""
        if not isinstance(subject, str) or not subject.strip():
            raise HTTPException(status_code=400, detail="subject is required")
        predicate = payload.get("predicate") if isinstance(payload.get("predicate"), str) else None
        try:
            return _core_timeline(subject.strip(), predicate, payload.get("at"), profile=profile)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @token_app.post("/ask", dependencies=[Depends(recall_token_guard)])
    def ask_route(payload: dict | None = None) -> dict:
        """One short cited answer (memd.ask); authorised and scoped exactly like /recall."""
        from memd.ask import DEFAULT_K, ask
        payload = payload or {}
        args = normalize_recall_args({"k": DEFAULT_K, **payload})
        if not args["query"].strip():
            raise HTTPException(status_code=400, detail="question is required")
        stores = _federation(payload, "recall")
        federated = stores is not None
        if stores is None:
            stores = [_resolve_profile(args.get("profile"), "recall")]

        def one(store, query, k, **kw):
            return _core_recall(query, profile=store, k=k, **kw)
        return ask(args["query"], stores=stores, recall_one=one, cfg_for=_cfg, federated=federated,
                   k=args["k"], host=args.get("host"), tags=args.get("tags"))

    @token_app.post("/read", dependencies=[Depends(require_token)])
    def read_route(payload: dict) -> dict:
        from memd.share import annotate_read, store_argument
        try:
            requested = store_argument(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        profile = _resolve_profile(requested)
        control.audit_read("read", f"{profile}:{str(payload.get('slug', ''))[:80]}")
        try:
            cfg = _cfg(profile)
            receipt = read_memory(
                payload.get("slug", ""), profile=profile, cfg=cfg,
                offset=payload.get("offset", 0), limit=payload.get("limit", 8000),
                revision=payload.get("revision"),
            )
            usage.record_read(cfg, profile, receipt, payload.get("recall_id"))
            return annotate_read(receipt, store=profile)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ReadRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ReadUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @token_app.post("/save", dependencies=[Depends(require_token)])
    def save_route(payload: dict) -> dict:
        # normalize_fact() before the core: _save_locked() does a bare
        # fact["title"], so an aliased payload would KeyError inside the
        # git-locked write path instead of returning a usable message here.
        try:
            fact = normalize_fact(payload or {})
        except NormalizeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        profile = _resolve_profile(fact.pop("profile", None), "save")
        access.authorize(profile, write=True)
        from memd import inbox
        if inbox.save_routes_to_inbox():
            # MEMD_SAVE_MODE=inbox: an agent token's save waits for review.
            try:
                queued = inbox.queued_receipt(_core_propose(fact, profile=profile, source="save"))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=202, content=queued)
        try:
            receipt = _maybe_dict(_core_save(fact, profile=profile))
        except ValueError as exc:
            status = 409 if type(exc).__name__ == "RevisionConflict" else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if receipt.get("saved") is False or receipt.get("ok") is False:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=503, content=receipt)
        return receipt

    @token_app.post("/publish", dependencies=[Depends(require_token)])
    def publish_route(payload: dict | None = None):
        """Copy a note into another store with provenance (memd.share)."""
        from fastapi.responses import JSONResponse
        from memd import share
        payload = payload or {}
        allowed = {"slug", "target_store", "store", "source_store", "profile", "allow_decrypted_publish"}
        if set(payload) - allowed:
            raise HTTPException(status_code=400, detail="Send {slug, target_store, store?, allow_decrypted_publish?}")
        try:
            if payload.get("store") is not None and payload.get("source_store") is not None \
                    and payload["store"] != payload["source_store"]:
                raise ValueError("store and source_store name different stores; send one")
            source = share.store_argument({"store": payload.get("store") or payload.get("source_store"),
                                           "profile": payload.get("profile")})
            out = share.publish(payload.get("slug"), payload.get("target_store"), source_store=source,
                                allow_decrypted_publish=payload.get("allow_decrypted_publish", False))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            status = 409 if type(exc).__name__ == "RevisionConflict" else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        if out.get("queued"):
            return JSONResponse(status_code=202, content=out)
        if not out.get("ok"):
            return JSONResponse(status_code=503, content=out)
        return out

    @token_app.post("/propose", dependencies=[Depends(require_token)])
    def propose_route(payload: dict) -> dict:
        """File a candidate memory for review (memd.inbox); same auth as /save."""
        try:
            fact = normalize_fact(payload or {})
        except NormalizeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        profile = _resolve_profile(fact.pop("profile", None), "save")
        access.authorize(profile, write=True)
        try:
            return _core_propose(fact, profile=profile)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @token_app.get("/handoff", dependencies=[Depends(require_token)])
    def handoff_route(repo: str = "", profile: str | None = None, max_age_days: int | None = None) -> dict:
        """The newest session handoff for a repository (memd.handoff); same auth as /propose.

        Only the caller's own store; a pending handoff only for the credential that
        proposed it. Text only.
        """
        from memd import handoff
        from memd.actor import get_actor
        profile = _resolve_profile(profile, "save")
        access.authorize(profile, write=True)
        try:
            found = handoff.latest(_cfg(profile), profile, repo.strip().casefold(), proposer=get_actor(),
                                   max_age=handoff.max_age_days(max_age_days))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "handoff": found}

    def _inbox_call(fn):
        from memd import inbox
        try:
            return fn()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except inbox.NotPending as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            status = 409 if type(exc).__name__ == "RevisionConflict" else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    def _inbox_reviewer(profile: str | None):
        from memd import inbox
        profile = _resolve_profile(profile, "save")
        try:
            reviewer, is_token = inbox.reviewer_for(profile)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return profile, reviewer, is_token

    @token_app.get("/inbox", dependencies=[Depends(require_token)])
    def inbox_list(profile: str | None = None, status: str = "pending",
                   offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200)):
        from memd import inbox
        profile, _, _ = _inbox_reviewer(profile)
        return _inbox_call(lambda: {**inbox.list_candidates(_cfg(profile), profile, status=status,
                                                            limit=limit, offset=offset),
                                    "profile": profile})

    @token_app.get("/inbox/{candidate_id}", dependencies=[Depends(require_token)])
    def inbox_get(candidate_id: str, profile: str | None = None):
        from memd import inbox
        profile, _, _ = _inbox_reviewer(profile)
        return _inbox_call(lambda: inbox.get(_cfg(profile), profile, candidate_id))

    @token_app.post("/inbox/{candidate_id}/approve", dependencies=[Depends(require_token)])
    def inbox_approve(candidate_id: str, payload: dict | None = None):
        from memd import inbox
        payload = payload or {}
        if set(payload) - {"profile", "edits"}:
            raise HTTPException(status_code=400, detail="Send {edits?, profile?}")
        profile, reviewer, is_token = _inbox_reviewer(payload.get("profile"))
        return _inbox_call(lambda: inbox.approve(
            _cfg(profile), profile, candidate_id, reviewer=reviewer, edits=payload.get("edits"),
            reviewer_is_token=is_token, save_fn=_core_save_with_cfg))

    @token_app.post("/inbox/{candidate_id}/reject", dependencies=[Depends(require_token)])
    def inbox_reject(candidate_id: str, payload: dict | None = None):
        from memd import inbox
        payload = payload or {}
        if set(payload) - {"profile", "reason"}:
            raise HTTPException(status_code=400, detail="Send {reason?, profile?}")
        profile, reviewer, is_token = _inbox_reviewer(payload.get("profile"))
        return _inbox_call(lambda: inbox.reject(
            _cfg(profile), profile, candidate_id, reviewer=reviewer,
            reason=payload.get("reason") or "", reviewer_is_token=is_token))

    @token_app.post("/reindex", dependencies=[Depends(require_token)])
    def reindex_route(payload: dict | None = None) -> dict:
        payload = payload or {}
        profile = _resolve_profile(payload.get("profile"), "reindex")
        pull = bool(payload.get("pull", True))
        access.authorize(profile, write=True)
        try:
            return _core_reindex(profile=profile, pull=pull)
        except Exception as e:
            return {"ok": False, "err": str(e)}

    # Browser assets are public; note content still requires the existing bearer
    # token. Explicit routes preserve the root and /mcp/ agent transports.
    from pathlib import Path
    from fastapi.responses import FileResponse, JSONResponse
    web_dir = Path(__file__).resolve().parent / "web"
    web_headers = {
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                                   "connect-src 'self'; img-src 'self' data:; "
                                   "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    }

    @token_app.get("/")
    def dashboard():
        return FileResponse(web_dir / "index.html", headers=web_headers)

    @token_app.get("/ui/assets/{name}")
    def dashboard_asset(name: str):
        if name not in {"app.js", "style.css", "icon.svg", "admin.js", "health.js", "entities.js"}:
            raise HTTPException(status_code=404, detail="unknown asset")
        return FileResponse(web_dir / name, headers=web_headers)

    @token_app.get("/ui/notes", dependencies=[Depends(require_token)])
    def dashboard_notes(profile: str | None = None,
                        offset: int = Query(default=0, ge=0),
                        limit: int = Query(default=24, ge=1, le=100),
                        tag: str | None = None):
        """Paged note list, with tags exposed and filterable.

        Tags are first class on save and filterable in recall, but the memory
        browser showed none of it -- this endpoint did not even SELECT the
        column they live in -- so every note appeared in one undifferentiated
        list. /entities already lists the tag vocabulary with counts; this adds
        the per-note tags and the filter the browser needs to use it.

        Tags live in the notes.metadata JSON blob rather than a column, so the
        filter and paging are applied in Python over the profile's rows. Fine
        at a few hundred notes; a much larger corpus wants a tags table with an
        index, and this endpoint is the only thing that would change.
        """
        import json as _json
        profile = _resolve_profile(profile)
        cfg = _cfg(profile)
        _, db_path = guard_paths(profile, cfg.clone, cfg.db)
        db = open_db(db_path)
        try:
            rows = db.execute(
                "SELECT slug,title,host,importance,substr(body,1,240),metadata FROM notes "
                "WHERE profile=? AND superseded_by IS NULL "
                "ORDER BY title COLLATE NOCASE,slug",
                (profile,),
            ).fetchall()
        finally:
            db.close()

        def tags_of(blob):
            try:
                value = (_json.loads(blob) or {}).get("tags") or []
            except (TypeError, ValueError):
                return []
            return [str(t) for t in value if str(t).strip()]

        notes = [{"slug": r[0], "title": r[1], "host": r[2], "importance": r[3],
                  "body": r[4], "tags": tags_of(r[5])} for r in rows]
        wanted = (tag or "").strip().casefold()
        if wanted:
            notes = [n for n in notes if wanted in {t.casefold() for t in n["tags"]}]
        return JSONResponse({"ok": True, "profile": profile, "total": len(notes),
                             "offset": offset, "tag": tag or "",
                             "notes": notes[offset:offset + limit]},
                            headers={"Cache-Control": "no-store"})

    @token_app.get("/help", include_in_schema=False)
    def index():
        """Portable setup reference; browser instructions live on /#onboarding."""
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("""memd — durable memory for agents

Open /#onboarding for setup instructions using this server's address.

1. Ask the server administrator for a token for your machine.
   In the server deployment directory:
       ./memd-token issue PROFILE MACHINE_LABEL

2. On the machine you want to connect, replace SERVER_URL below:
       curl -fsS SERVER_URL/clients/onboard.sh -o onboard.sh
       MEMD_SERVER=SERVER_URL bash onboard.sh
   The installer prompts for the token and configures supported clients.

3. Restart your agent and ask it to recall a saved topic.

ENDPOINTS
    GET  /                    Memory index and onboarding dashboard
    GET  /health              Service and index health
    GET  /stats               Index statistics
    GET  /metrics             Prometheus metrics (token with stats access)
    GET  /insights            Memory health report (bearer token required)
    GET  /entities            Hosts, services and tags with counts (bearer token required)
    GET  /entities/KIND/NAME  Everything memory holds about one entity (bearer token required)
    GET  /ui/notes            Paginated active notes (bearer token required)
    POST /recall              Search (bearer token when auth is enforced)
    POST /timeline            Current or dated facts about a subject (as /recall)
    POST /ask                 One short answer citing its notes (as /recall)
    POST /read                Read a note (bearer token required)
    POST /save                Save a note (bearer token required)
    POST /propose             Propose a note for review (bearer token required)
    GET  /handoff?repo=       Latest session handoff for a repository (bearer token required)
    POST /publish             Publish a note into another store (bearer token required)
    GET  /inbox               Candidates waiting for review (signed-in account)
    POST /reindex             Refresh the index (bearer token required)
    POST / or /mcp/           MCP over HTTP (bearer token required)

CLIENT DOWNLOADS
    /clients/onboard.sh
    /clients/memd-recall-hook
    /clients/memd-activity-hook
    /clients/memd-handoff-hook
    /clients/pi-memd.ts
    /clients/memd-mcp-bridge

Use a distinct token per machine. Revocation takes effect without a restart.
The dashboard keeps its token only in tab memory; reload or lock to clear it.
""")

    # Client artifacts, served by memd itself so onboarding needs exactly one
    # URL: the bootstrap, the Claude Code recall hook, and the pi extension.
    # Deliberately unauthenticated — these files contain no secrets, and a
    # machine being onboarded has no token yet.
    @token_app.get("/clients/{name}")
    def client_file(name: str):
        from fastapi.responses import PlainTextResponse
        from pathlib import Path
        allowed = {
            "onboard.sh": "onboard.sh",
            "memd-recall-hook": "memd-recall-hook",
            "memd-activity-hook": "memd-activity-hook",
            "memd-handoff-hook": "memd-handoff-hook",
            "pi-memd.ts": "pi-memd.ts",
            "memd-mcp-bridge": "memd-mcp-bridge",
        }
        fname = allowed.get(name)
        if fname is None:
            raise HTTPException(status_code=404, detail="unknown client artifact")
        path = Path(__file__).resolve().parent.parent / "clients" / fname
        try:
            return PlainTextResponse(path.read_text(encoding="utf-8"))
        except OSError:
            raise HTTPException(status_code=404, detail="artifact not present")

    from memd.admin_web import install_web
    install_web(token_app)
    from memd.user_web import install_user_web
    install_user_web(token_app)

    # Mounted last so the explicit routes above take precedence.
    token_app.mount("/mcp", mcp_asgi)

    # Root-path MCP dispatch: make https://memd.example.com itself a valid MCP
    # endpoint so a client is configured with just the bare hostname and no
    # /mcp/ path. This is purely additive — /mcp/* is untouched and all other
    # routes keep working. The middleware must be added after mount so the
    # underlying FastAPI app (with its router + mounted sub-apps) is the
    # fallback handler the middleware delegates to.
    token_app.add_middleware(RootMcpDispatch, mcp_app=mcp_asgi)
    token_app.add_middleware(access.AccessMiddleware)
    # Outermost, so refused and MCP requests are counted too (memd.metrics).
    from memd.metrics import MetricsMiddleware
    token_app.add_middleware(MetricsMiddleware)

    return token_app


# The module-level app is the single token-guarded app.
app = create_token_app()
