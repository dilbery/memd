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
"""
from __future__ import annotations

import contextvars

current_actor: contextvars.ContextVar[str] = contextvars.ContextVar("memd_actor", default="")


def set_actor(label: str) -> None:
    """Record the caller for this request. An empty label means unattributed."""
    current_actor.set((label or "").strip())


def get_actor() -> str:
    return current_actor.get()
