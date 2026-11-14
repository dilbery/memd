"""Who is calling, for the duration of one request.

Both authentication points set the label that ``check_bearer`` returned: the REST
guards in ``memd.server`` and the ASGI wrapper in ``memd.mcp_http``. ``save()``
reads it and stamps the note's frontmatter and the Git commit, so "which client
wrote this fact" is answerable from the store alone.

``asyncio.to_thread`` copies the context, so the value reaches the worker thread
that runs the synchronous core on the MCP path.

A caller can never set this through a payload: ``normalize_fact`` drops unknown
keys and ``saved_by`` is not among the ones it accepts.

This is a CLIENT label, not a person: through a shared gateway token every user
looks the same. Plan 3 replaces the value with the authenticated email at these
same two points, without touching save.py.

``caller()`` adds the ids behind the label, for the places where a label is not
enough to tell two callers apart (who proposed an inbox candidate).
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass

current_actor: contextvars.ContextVar[str] = contextvars.ContextVar("memd_actor", default="")


def set_actor(label: str) -> None:
    """Record the caller for this request. An empty label means unattributed."""
    current_actor.set((label or "").strip())


def get_actor() -> str:
    return current_actor.get()


@dataclass(frozen=True)
class Caller:
    """The label plus the ids that tell callers apart. Labels are not unique."""
    label: str
    account: str | None = None
    credential: str | None = None


def caller() -> Caller:
    """The current caller's label, account and credential.

        caller              account                       credential
        account session     user id                       "session"
        account token       user id                       token id
        legacy file token   none                          "legacy:<digest>"
        registry token      identity:<store> if personal  registry token id
        bound identity      identity:<store>              the same
        local caller        none                          none

    Every session of an account carries the credential "session", so a
    credential names one caller only together with its account. A local caller
    (the CLI, stdio, an in-process hook) has no ids: its label is its identity.
    """
    from memd import access
    from memd.identity import current_identity
    from memd.registry import principal as registry_token
    label = get_actor()
    principal = access.current.get()
    if principal is not None:
        if principal.id.startswith("legacy:"):
            return Caller(label, None, principal.id)
        return Caller(label, principal.id, "session" if principal.session else principal.token_id or None)
    if not access.remote_request.get():
        return Caller(label)
    identity = current_identity()
    account = f"identity:{identity}" if identity else None
    return Caller(label, account, registry_token.get() or account)
