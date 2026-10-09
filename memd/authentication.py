"""Shared REST/MCP authentication. Browser administration has a separate guard."""
import logging


def authenticate(authorization, check_static):
    from memd.identity import clear_identity
    clear_identity()
    from memd.kasm import KasmError, kasm_enabled, kasm_identity, looks_like_kasm_token
    from memd.oidc import OidcError, bearer_identity, oidc_enabled
    label = check_static(authorization)
    if label is not None:
        return label
    # A presented registry credential must never fall through to other issuers.
    if authorization and authorization.startswith("Bearer memd_"):
        return ""
    if kasm_enabled() and looks_like_kasm_token(authorization):
        try:
            return kasm_identity(authorization)
        except KasmError as exc:
            logging.getLogger(__name__).warning("kasm: rejected session token: %s", exc)
            return ""
    if oidc_enabled():
        try:
            return bearer_identity(authorization)
        except OidcError as exc:
            logging.getLogger(__name__).warning("oidc: rejected bearer: %s", exc)
    return ""
