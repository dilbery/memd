"""The authenticated caller's own store, for the duration of one request.

Distinct from ``memd.actor`` on purpose, and the distinction is the whole point:

* ``actor`` is a CLIENT LABEL for attribution. It is stamped into a note's
  frontmatter and the Git commit so "which client wrote this" is answerable
  from the store alone. It decides nothing.
* ``identity`` is WHO THE CALLER IS, and it selects the store. It is an
  authorization input.

Keeping them apart is what stops a token label from becoming a store name.
Label-to-store is Option A in §9a, marked "fallback only", and it is the design
a previous session built by mistake; a reviewer seeing ``set_actor`` next to
``bind_identity`` should be able to tell at a glance that only one of them
grants access.

**This module ships no identity source.** Nothing in piece 1 calls
:func:`bind_identity` with a value a remote caller controls. The OIDC
resource-server layer (§9b) is what will call it, with the ``email`` claim of a
bearer whose signature, issuer, expiry and ``memd`` scope have all been checked
first. Until that lands, every request is unbound and memd behaves as it does
today. A request field must NEVER reach this function.

``asyncio.to_thread`` copies the context, so the value reaches the worker
thread running the synchronous core, the same way ``actor`` does.
"""
from __future__ import annotations

import contextvars

from memd.stores import store_name

_current_identity: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "memd_identity", default=None
)


def bind_identity(identity: str) -> str:
    """Bind the authenticated caller's store for this request and return it.

    `identity` is normalised through :func:`memd.stores.store_name`, so the
    store is the lower-cased email (§9b) and a malformed one raises
    InvalidStoreName rather than being sanitised into some other user's store.
    A refused bind leaves the previous state untouched.
    """
    name = store_name(identity)
    _current_identity.set(name)
    return name


def current_identity() -> str | None:
    """The bound store name, or None when the caller is unauthenticated."""
    return _current_identity.get()


def clear_identity() -> None:
    """Unbind. Used by tests and by any caller reusing a context."""
    _current_identity.set(None)
