"""Accept a Kasm session token as proof of identity.

A Kasm workspace has already signed the user in through authentik, so making
them sign in AGAIN in a browser inside the container is a poor experience. Every
Kasm session container is given a `KASM_API_JWT` signed by Kasm's own RSA key,
and this module accepts that as an identity.

**Why this is not the header-based design that was rejected.** The user has a
shell in their own workspace, so anything the CONTAINER asserts about who they
are is forgeable by them, and a header saying "I am Bob" would let any user read
any other user's memory. A Kasm-signed JWT is different in kind: the user can
read their own token but cannot mint one naming somebody else.

**memd holds no Kasm credentials.** Two inputs, both unprivileged:

  * `MEMD_KASM_JWT_PUBKEY`, Kasm's PUBLIC signing key, which Kasm already
    publishes to its own components.
  * `MEMD_KASM_USER_MAP`, a JSON file the onboarding job maintains on the identity-provider host,
    where the privileged credentials already are. memd only reads it.

**The thing that is genuinely unpleasant, and its mitigation.** These tokens
carry an `exp` years in the future, so a copied one would
otherwise be a very long-lived bearer. So `kasm_id` must appear in the map's
`active_sessions`: a token stops working when the workspace closes rather than
years later. The list is refreshed by the same five-minute job, which is the
window of exposure after a session ends.

Everything here fails CLOSED. A missing map, an absent `active_sessions`, an
unreadable key: all refuse. A map that has not been written yet must never mean
"admit everybody".

MEMD_KASM_JWT_PUBKEY unset means this layer is off and nothing changes.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

# Kasm signs with RS256. Pinned as an allowlist rather than read from the token,
# because trusting the header's `alg` is how `alg: none` works.
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512")

# The map is a small local file the sync job rewrites; re-stat it often enough
# that a new starter works without restarting memd, cheaply enough that it is
# not a per-request read.
MAP_TTL_SECONDS = 30.0
CLOCK_SKEW_LEEWAY_SECONDS = 10


class KasmError(Exception):
    """A Kasm session token was absent, malformed, or did not validate.

    One type on purpose: the caller turns it into a 401 and the reason goes to
    the log, not to the client.
    """


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def kasm_enabled() -> bool:
    """True when a Kasm public key is configured. Absent means this layer is off."""
    return bool(_env("MEMD_KASM_JWT_PUBKEY"))


_lock = threading.Lock()
_map: dict | None = None
_map_at = 0.0
_map_mtime = 0.0
_key = None


def reset_kasm_cache() -> None:
    """Drop the cached key and map. For tests and for forcing a reread."""
    global _map, _map_at, _map_mtime, _key
    with _lock:
        _map, _map_at, _map_mtime, _key = None, 0.0, 0.0, None


def _public_key():
    global _key
    with _lock:
        if _key is not None:
            return _key
        path = _env("MEMD_KASM_JWT_PUBKEY")
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_public_key

            _key = load_pem_public_key(open(path, "rb").read())
        except Exception as exc:
            raise KasmError(f"cannot read the Kasm public key at {path}: {exc}") from exc
        return _key


def _user_map() -> dict:
    """The uuid-to-email map plus the live session list, cached briefly.

    Re-read when the file's mtime changes so a user who appears between runs of
    the sync job works without restarting memd.
    """
    global _map, _map_at, _map_mtime
    path = _env("MEMD_KASM_USER_MAP")
    if not path:
        raise KasmError("MEMD_KASM_USER_MAP is not configured")
    try:
        mtime = os.stat(path).st_mtime
    except OSError as exc:
        raise KasmError(f"Kasm user map unreadable at {path}: {exc}") from exc

    now = time.monotonic()
    with _lock:
        fresh = _map is not None and (now - _map_at) < MAP_TTL_SECONDS
        if fresh and mtime == _map_mtime:
            return _map
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            raise KasmError(f"Kasm user map at {path} is not readable JSON: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
            raise KasmError(f"Kasm user map at {path} has no users object")
        _map, _map_at, _map_mtime = data, now, mtime
        return data


def _bearer(authorization: str | None) -> str:
    if not authorization or not isinstance(authorization, str):
        raise KasmError("no Authorization header")
    scheme, _, rest = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise KasmError("Authorization header is not a Bearer token")
    token = rest.strip()
    if not token:
        raise KasmError("empty Bearer token")
    return token


def looks_like_kasm_token(authorization: str | None) -> bool:
    """Cheap, signature-free triage: is this plausibly a Kasm session token?

    Used only to decide which validator to spend work on. It proves nothing;
    `kasm_identity` still verifies the signature. Kept deliberately narrow so an
    OIDC bearer is never sent down this path and reported with the wrong error.
    """
    if not kasm_enabled():
        return False
    try:
        import jwt

        token = _bearer(authorization)
        claims = jwt.decode(token, options={"verify_signature": False})
        return "kasm_id" in claims and "user_id" in claims
    except Exception:
        return False


def validate(authorization: str | None) -> dict:
    """Verify a Kasm session token and return its claims, or raise."""
    import jwt

    token = _bearer(authorization)
    try:
        claims = jwt.decode(
            token,
            key=_public_key(),
            algorithms=list(ALLOWED_ALGORITHMS),
            options={"verify_aud": False, "require": ["exp", "user_id", "kasm_id"]},
            leeway=CLOCK_SKEW_LEEWAY_SECONDS,
        )
    except KasmError:
        raise
    except Exception as exc:
        raise KasmError(f"token rejected: {type(exc).__name__}: {exc}") from exc

    data = _user_map()

    # The session must be LIVE. Without this a token copied out of a container
    # keeps working for years. `active_sessions` absent is a refusal, not a
    # wildcard: a map the job has not written yet must not admit everybody.
    active = data.get("active_sessions")
    if not isinstance(active, list):
        raise KasmError("Kasm user map has no active_sessions list; refusing to "
                        "treat every session as live")
    if claims.get("kasm_id") not in set(active):
        raise KasmError("this Kasm session is not active")

    return claims


def identity_from_claims(claims: dict) -> str:
    """Resolve a validated token's `user_id` to a store name."""
    from memd.stores import InvalidStoreName, store_name

    uid = claims.get("user_id")
    email = (_user_map().get("users") or {}).get(uid)
    if not email:
        raise KasmError(f"Kasm user {uid!r} is not in the user map")
    try:
        return store_name(email)
    except InvalidStoreName as exc:
        raise KasmError(f"Kasm user {uid!r} maps to an unusable store name: {exc}") from exc


def kasm_identity(authorization: str | None) -> str:
    """Validate a Kasm session token and BIND the caller's store.

    Binding happens only after every check, so a raise leaves nothing bound.
    """
    from memd.identity import bind_identity

    claims = validate(authorization)
    store = identity_from_claims(claims)
    bind_identity(store)
    log.info("kasm: bound store=%s session=%s", store, claims.get("kasm_id"))
    return store
