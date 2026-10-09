"""Token-guarded streamable-HTTP MCP for memd.

This module exposes the *same* two MCP tools (``recall`` and ``save``) that
``memd/mcp.py`` serves over stdio, but over a streamable-HTTP transport so that
a remote client can onboard with a single ``Authorization: Bearer <token>``
header and no local memd install.

To keep the two surfaces from drifting, this module does **not** redefine the
tools: it imports the fully-wired ``server`` object from ``memd.mcp`` and wraps
that single source of truth. Any change to the tool surfaces in
``memd.mcp`` is automatically reflected here.

Tokens are accepted from two sources — the legacy ``MEMD_TOKEN`` environment
variable and a per-line file (path configurable) — so that a single machine's
access can be revoked on its own without rotating everyone's token. The token
file is reloaded on every request, so issuing or revoking a token takes effect
immediately with no container restart.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Any

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from memd.actor import set_actor
from memd.mcp import server as _mcp_server


def _authenticate(authorization: str | None) -> str:
    from memd.authentication import authenticate
    return authenticate(authorization, check_bearer)

logger = logging.getLogger(__name__)

_TOKENS_FILE_DEFAULT = "/home/memd/.memd/tokens"


def load_tokens() -> dict[str, str]:
    """Return ``{token: label}`` for the current set of accepted tokens.

    Sources, merged with later entries winning on collision:

    1. ``MEMD_TOKEN`` env var, if set and non-empty, → label ``"legacy"``.
    2. The file at ``MEMD_TOKENS_FILE`` (default ``/home/memd/.memd/tokens``),
       if it exists and is readable. One entry per line, format
       ``<label> <token>`` split on the first run of whitespace. Blank lines
       and lines whose first non-space character is ``#`` are ignored. Lines
       lacking a whitespace separator are ignored silently so a typo cannot
       lock everyone out.

    The file is read on every call so issue/revoke takes effect immediately.
    """
    tokens: dict[str, str] = {}

    legacy = os.environ.get("MEMD_TOKEN")
    if legacy:
        tokens[legacy] = "legacy"

    path = os.environ.get("MEMD_TOKENS_FILE", _TOKENS_FILE_DEFAULT)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue
                label, token = parts[0], parts[1].strip()
                if label and token:
                    tokens[token] = label
    except OSError:
        logger.debug("token file not readable: %s", path, exc_info=True)

    return tokens


def check_bearer(authorization: str | None) -> str | None:
    """Return the label for a valid ``Bearer <token>`` header, else ``None``.

    Comparison is constant-time via :func:`hmac.compare_digest`. Returns
    ``None`` for a missing header, a header not starting with ``Bearer ``,
    or an unknown token.
    """
    from memd.access import authenticate_token
    from memd.registry import Registry, configured, principal
    principal.set(None)
    if not authorization or not authorization.startswith("Bearer "):
        return None
    candidate = authorization[len("Bearer "):].strip()
    if not candidate:
        return None
    # Administered accounts and, until the registry cutover, the legacy file.
    account = authenticate_token(candidate)
    if account is not None:
        return account.label
    if configured():
        row = Registry().authenticate(candidate)
        if row:
            principal.set(row["id"])
            if row["personal_subject"]:
                from memd.identity import bind_identity
                bind_identity(row["personal_store"])
            return row["label"]
    return None


def build_mcp_app() -> tuple[Any, Any]:
    """Return ``(asgi_app, lifespan_factory)`` for the memd MCP over HTTP.

    ``asgi_app`` is an async ASGI callable that authenticates via
    :func:`check_bearer` and delegates to a stateless
    :class:`StreamableHTTPSessionManager` wrapping ``memd.mcp.server``.
    ``lifespan_factory`` returns the async context manager that drives the
    session manager's startup/shutdown.
    """
    session_manager = StreamableHTTPSessionManager(
        app=_mcp_server,
        stateless=True,
        json_response=True,
    )

    def lifespan_factory() -> Any:
        # session_manager.run() IS the async context manager; return it directly.
        # Making this `async def` would yield a coroutine instead, which the
        # caller cannot use with `async with`.
        return session_manager.run()

    async def asgi_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await session_manager.handle_request(scope, receive, send)
            return

        headers = {
            k.decode("utf-8", "ignore").lower(): v.decode("utf-8", "ignore")
            for k, v in scope.get("headers", [])
        }
        authorization = headers.get("authorization")
        label = _authenticate(authorization)
        if not label:
            # `WWW-Authenticate` is a MUST on a 401 in the MCP authorisation
            # spec: it is how the client discovers where to authenticate. With
            # OIDC on it names the protected-resource document, so a fresh
            # client can complete the handshake with nothing configured but the
            # server URL.
            from memd.oidc import www_authenticate

            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", www_authenticate().encode("utf-8")),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"error":"invalid or missing token"}',
            })
            return

        # Record who called before dispatching; save() stamps the note with it.
        set_actor(label)
        await session_manager.handle_request(scope, receive, send)

    return asgi_app, lifespan_factory
