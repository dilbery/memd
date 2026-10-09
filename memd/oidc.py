"""memd as an OAuth 2.1 resource server (Plan 2 piece 2, §9b).

authentik is the authorisation server, the agent (Claude Code, Codex, omp,
VS Code, Cursor) is the client, and every request carries a short-lived bearer.
The user adds the server once and never pastes a key; offboarding is removal
from the directory group, after which refresh fails and the access token expires on
its own.

**This module is the only production caller of `identity.bind_identity`.** Piece
1 built a substrate where the store follows the authenticated caller and no
request field can move them off it; nothing could authenticate, so nothing was
ever bound. This is what closes that gap, which is why the validation below is
written to be read by someone checking it rather than to be short.

Order of checks, deliberately: signature, then issuer and time, then scope,
then identity. Nothing about a token is trusted before its signature is, and no
store name is derived from a token that has not passed every earlier check.

**Why scope and not audience.** The spec wants RFC 8707 `resource` honoured as
`aud`. authentik's behaviour there is unverified, and under dynamic client
registration every client has its own `client_id`, so `aud` cannot identify the
resource. A custom `memd` scope, defined on the provider, is the practical
check: a token authentik minted for any other application is signed by the same
key and carries the same issuer, and is refused here because it does not carry
that scope. Revisit if authentik's `resource` handling is ever confirmed.

MEMD_OIDC_ISSUER unset means this layer is off and memd authenticates exactly as
it does today, so taking this code changes no running deployment.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import httpx

log = logging.getLogger(__name__)

# authentik issues RS256 by default. Pinned as an allowlist rather than read
# from the token, because "trust the header's alg" is how `alg: none` and the
# RS256-to-HS256 confusion attack both work.
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512")

# A JWKS is small and changes only on key rotation.
JWKS_TTL_SECONDS = 3600.0
# An unknown `kid` means either rotation (refetch and succeed) or a forged
# token (refetch and still fail). Rate-limited so the second case cannot be
# used to hammer authentik with one request per forged token.
JWKS_REFETCH_COOLDOWN_SECONDS = 60.0
JWKS_TIMEOUT_SECONDS = 5.0
# Clock skew allowance on exp/iat. Small on purpose: authentik and every client
# here are NTP-synced, access tokens are minutes long, and a generous leeway
# quietly extends the life of a revoked session.
CLOCK_SKEW_LEEWAY_SECONDS = 10


class OidcError(Exception):
    """A bearer was absent, malformed, or did not validate.

    Deliberately one type. The caller turns it into a 401 with a
    `WWW-Authenticate` header, and the reason goes to the log rather than to the
    client: which of the checks failed is useful to an operator and useful to an
    attacker, and only one of them is entitled to it.
    """


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def oidc_enabled() -> bool:
    """True when an issuer is configured. Absent means this layer is off."""
    return bool(_env("MEMD_OIDC_ISSUER"))


def issuer() -> str:
    return _env("MEMD_OIDC_ISSUER")


def required_scope() -> str:
    return _env("MEMD_OIDC_SCOPE", "memd")


def advertised_scopes() -> list[str]:
    """The scopes memd ADVERTISES in its RFC 9728 document. Not what it requires.

    These are two different questions and they used to be answered by one
    function. `required_scope()` is what `validate_bearer` insists on. This list
    is what a client READS to decide what to ask authentik for, and authentik
    issues a refresh token only when `offline_access` was asked for and is
    mapped on the provider. With one scope advertised, no agent ever asked, no
    refresh token was ever issued, and every session more than ten minutes after
    the last one needed a browser.

    Adding a name here adds no requirement. A token still has to carry
    `required_scope()`, the check is membership, and a token carrying more
    scopes than that has always been accepted.

    The default is derived from `required_scope()` rather than written out, so
    that changing `MEMD_OIDC_SCOPE` cannot leave the advertised list describing
    a scope the check no longer wants. Today it reads `memd offline_access`.
    `required_scope()` is force-included whatever the variable says: a list that
    omitted it would tell every client to ask for the one thing that cannot get
    them in.

    An empty or blank value falls back to the default rather than advertising
    nothing, because "set but empty" is what a half-written env file looks like
    and an empty list would break every first sign-in.
    """
    want = required_scope()
    raw = _env("MEMD_OIDC_ADVERTISED_SCOPES")
    if not raw:
        raw = (want + " offline_access").strip()
    out: list[str] = []
    for name in raw.split():
        if name not in out:
            out.append(name)
    if want and want not in out:
        log.warning(
            "MEMD_OIDC_ADVERTISED_SCOPES omits the required scope %r; adding it", want
        )
        out.insert(0, want)
    return out


def authorization_server() -> str:
    """The authorisation server URL ADVERTISED to clients.

    Not necessarily the issuer. authentik's `issuer_mode: global` makes every
    provider mint tokens with `iss = https://auth.../`, which is what lets one
    resource accept tokens from several agent clients, but authentik serves NO
    discovery document at that root (404 for both well-known paths). Discovery
    lives at `.../application/o/<app>/`, which reports the global issuer inside
    it.

    So a client must be pointed at the per-application URL while the token is
    validated against the global one. Advertising the bare issuer sends clients
    to a 404; requiring the per-application URL as `iss` rejects every token.
    Both were observed on the first real sign-in.

    Defaults to the issuer, so a deployment whose issuer does serve its own
    metadata needs no extra configuration.
    """
    return _env("MEMD_OIDC_AUTHORIZATION_SERVER", issuer()).rstrip("/") + "/"


def resource_url() -> str:
    return _env("MEMD_PUBLIC_URL").rstrip("/")


def jwks_uri() -> str:
    """The JWKS location: explicit if set, else derived from the issuer.

    authentik serves it at `<issuer>jwks/`. Overridable because deriving a URL
    from another URL is exactly the kind of assumption that breaks quietly on an
    upgrade.
    """
    explicit = _env("MEMD_OIDC_JWKS_URI")
    if explicit:
        return explicit
    # Derived from the ADVERTISED server, not the issuer: under a global issuer
    # the issuer is a bare origin with no `jwks/` beneath it, while the
    # per-application URL does have one.
    return authorization_server().rstrip("/") + "/jwks/"


def protected_resource_metadata() -> dict:
    """RFC 9728 `/.well-known/oauth-protected-resource`.

    Note what is NOT here: `registration_endpoint`. Claude Code fails metadata
    validation on a null value and attempts dynamic client registration on a
    present one, and authentik does not advertise DCR in its own metadata
    either (checked against a live server). The key must be
    absent, not empty.
    """
    return {
        "resource": resource_url(),
        "authorization_servers": [authorization_server()],
        # ADVERTISED, not required. The check in validate_bearer() is still
        # required_scope(); this list is only what makes a client ASK for
        # offline_access, which is the one way authentik issues a refresh token.
        "scopes_supported": advertised_scopes(),
        "bearer_methods_supported": ["header"],
    }


def www_authenticate() -> str:
    """The `WWW-Authenticate` value for a 401 (spec MUST).

    It is how a client discovers where to authenticate, so a 401 without it
    leaves the client with nowhere to go.
    """
    res = resource_url()
    # Without an issuer there is nothing to discover; pointing a client at OAuth
    # metadata would send it into an authorisation flow that cannot succeed.
    if not res or not oidc_enabled():
        return "Bearer"
    return f'Bearer resource_metadata="{res}/.well-known/oauth-protected-resource"'


# --------------------------------------------------------------------------
# JWKS cache
# --------------------------------------------------------------------------

_lock = threading.Lock()
_keys: dict[str, object] = {}
_fetched_at = 0.0
# None means "never refetched", NOT 0.0. time.monotonic()'s reference point is
# arbitrary (uptime on Linux), so on a freshly booted host `now - 0.0` is less
# than the cooldown and a legitimate key rotation in the first minute of uptime
# would be refused. Surfaced as a flaky test; it is a real defect.
_last_refetch: float | None = None


def reset_jwks_cache() -> None:
    """Drop the cached keys. For tests and for an operator forcing a refresh."""
    global _keys, _fetched_at, _last_refetch
    with _lock:
        _keys = {}
        _fetched_at = 0.0
        _last_refetch = None


def _fetch_jwks() -> dict[str, object]:
    from jwt import PyJWK

    url = jwks_uri()
    try:
        r = httpx.get(url, timeout=JWKS_TIMEOUT_SECONDS)
        r.raise_for_status()
        doc = r.json()
    except Exception as exc:
        raise OidcError(f"JWKS unavailable at {url}: {exc}") from exc

    out: dict[str, object] = {}
    for entry in doc.get("keys", []):
        kid = entry.get("kid")
        if not kid:
            continue
        try:
            out[kid] = PyJWK(entry).key
        except Exception:
            # One unusable entry must not deny every other key.
            log.warning("skipping unusable JWKS entry kid=%s", kid)
    if not out:
        raise OidcError(f"JWKS at {url} contained no usable keys")
    return out


def _key_for(kid: str | None):
    """Resolve a `kid` to a verification key, refetching on a miss.

    Refetch is rate-limited: rotation is rare and legitimate, an unknown kid
    from a forged token is neither, and without the cooldown the second case
    turns every junk token into a request to authentik.
    """
    global _keys, _fetched_at, _last_refetch
    with _lock:
        now = time.monotonic()

        # 1. Populate or refresh on age. This is not a "refetch" for cooldown
        #    purposes: an empty or expired cache has to be filled regardless.
        if not _keys or (now - _fetched_at) >= JWKS_TTL_SECONDS:
            _keys = _fetch_jwks()
            _fetched_at = now

        if kid in _keys:
            return _keys[kid]

        # 2. Known-good cache, unknown kid. Either authentik rotated its signing
        #    key, or the token is forged. Refetch once to cover the first, and
        #    rate-limit so the second cannot turn every junk token into a
        #    request to authentik.
        if _last_refetch is None or                 (now - _last_refetch) >= JWKS_REFETCH_COOLDOWN_SECONDS:
            _last_refetch = now
            _keys = _fetch_jwks()
            _fetched_at = now

        key = _keys.get(kid)
    if key is None:
        raise OidcError(f"no key for kid={kid!r} in the issuer's JWKS")
    return key


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not isinstance(authorization, str):
        raise OidcError("no Authorization header")
    scheme, _, rest = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise OidcError("Authorization header is not a Bearer token")
    token = rest.strip()
    if not token:
        raise OidcError("empty Bearer token")
    return token


def _scopes(claims: dict) -> set[str]:
    """Scopes as a set, from either representation.

    `scope` is a space-delimited string in OAuth 2; some servers also emit a
    `scp` list. Split rather than substring-match, so `memdxyz` never satisfies
    a requirement for `memd`.
    """
    raw = claims.get("scope") or ""
    out = set(raw.split()) if isinstance(raw, str) else set()
    scp = claims.get("scp")
    if isinstance(scp, str):
        out |= set(scp.split())
    elif isinstance(scp, (list, tuple)):
        out |= {str(s) for s in scp}
    return out


def validate_bearer(authorization: str | None) -> dict:
    """Verify the bearer and return its claims, or raise OidcError.

    Signature first, then registered claims, then scope. Every failure is the
    same exception type on purpose.
    """
    import jwt

    token = _bearer_token(authorization)

    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:
        raise OidcError(f"malformed token header: {exc}") from exc

    # Read the header only to select a key. The algorithm the token ASKS for is
    # never used; `algorithms=` below is the allowlist that decides.
    key = _key_for(header.get("kid"))

    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=list(ALLOWED_ALGORITHMS),
            issuer=issuer(),
            # `aud` is not the check here: under per-client registration every
            # client has its own client_id, so audience cannot identify this
            # resource. The custom scope below does that job.
            options={"verify_aud": False, "require": ["exp", "iss"]},
            leeway=CLOCK_SKEW_LEEWAY_SECONDS,
        )
    except Exception as exc:
        raise OidcError(f"token rejected: {type(exc).__name__}: {exc}") from exc

    want = required_scope()
    if want and want not in _scopes(claims):
        raise OidcError(
            f"token does not carry the {want!r} scope; it was issued for a "
            "different application"
        )
    return claims


def identity_from_claims(claims: dict) -> str:
    """The store name for a validated token: the lower-cased `email` claim.

    §9b: identity is the email everywhere on the hub. Validation happens in
    `stores.store_name`, so an email that is not a usable single path segment
    is refused rather than sanitised into somebody else's store.
    """
    from memd.stores import InvalidStoreName, store_name

    email = claims.get("email")
    if not email or not isinstance(email, str):
        raise OidcError("token carries no usable email claim")
    try:
        return store_name(email)
    except InvalidStoreName as exc:
        raise OidcError(f"email claim is not a usable store name: {exc}") from exc


def bearer_identity(authorization: str | None) -> str:
    """Validate a bearer and BIND the caller's store, returning its name.

    The single entry point for the transports. Binding happens here, after
    every check, so there is no window in which a partially validated token has
    set an identity; a raise leaves nothing bound.
    """
    from memd.identity import bind_identity

    claims = validate_bearer(authorization)
    store = identity_from_claims(claims)
    bind_identity(store)
    # `sub` survives an email change; recorded so a rename can be reconciled
    # later without guessing which store belonged to whom.
    log.info("oidc: bound store=%s sub=%s", store, claims.get("sub"))
    return store
