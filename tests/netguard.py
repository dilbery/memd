"""Structural guarantee that no test opens an outbound network connection.

The suite was once found making LIVE authenticated HTTP calls to a production memd
server during test runs, using a developer's real MEMD_TOKEN, and pulling real
private notes into test assertions. Cause: the developer's shell profile exported
MEMD_REMOTE / MEMD_TOKEN into every shell, pytest inherited them, and
memd.hooks.auto_recall.build_context() branches on MEMD_REMOTE -- so it took an HTTP
path instead of the in-process one the tests were mocking. Confirmed against
memd's own access log during a run.

That specific leak is fixed by clearing MEMD_* in conftest's autouse hermeticity
floor. This module is the SECOND layer, and it exists because the first one is a
blocklist: clearing env vars stops the leak we know about, while this stops the whole
class. A future code path that hardcodes a URL, reads a different variable, or grows
a new default endpoint cannot reach the network from inside a test, because the
socket primitives themselves refuse.

By patching the low-level socket calls rather than any HTTP client, the guard is
independent of which library does the dialling -- httpx, urllib, mcp, or something
not yet added.
"""

from __future__ import annotations

import socket
from typing import Any, cast

# Not every platform defines AF_UNIX (Windows historically did not). Resolve it once
# here so _is_blocked() cannot raise AttributeError at call time on such a box; the
# sentinel is an int no real family will equal.
_AF_UNIX = getattr(socket, "AF_UNIX", -1)


class NetworkBlockedInTests(RuntimeError):
    """Raised when a test attempts to initiate an outbound network connection."""


def _get_destination_info(address: Any) -> str:
    """Render an address as 'host:port', falling back to str() for odd shapes.

    Used only to build the error message, so it must never raise -- a guard that
    crashes while reporting a block is worse than the block itself.
    """
    if isinstance(address, tuple) and len(address) >= 2:
        host = address[0]
        port = address[1]
        return f"{host}:{port}"
    return str(address)


def _is_loopback(host: str) -> bool:
    """Check whether the host string names the local machine."""
    return host in ("127.0.0.1", "::1", "localhost")


def _is_blocked(family: int, address: Any, allow_loopback: bool) -> bool:
    """Decide whether one connection attempt should be refused.

    Rules:
    1. AF_UNIX is always allowed. Unix sockets are local IPC -- they cannot leave
       the machine, and blocking them would break unrelated tooling that uses them.
    2. AF_INET / AF_INET6 are blocked, unless allow_loopback is True and the
       destination is local.
    3. Any other family is allowed; the guard is about IP egress, and refusing
       families we have not reasoned about would cause confusing failures.
    """
    if family == _AF_UNIX:
        return False

    if family in (socket.AF_INET, socket.AF_INET6):
        if not allow_loopback:
            return True

        # AF_INET addresses are (host, port); AF_INET6 may carry flow/scope too, so
        # read the host positionally rather than unpacking a fixed arity.
        if isinstance(address, tuple) and len(address) >= 1:
            if _is_loopback(str(address[0])):
                return False
            return True
        # An address we cannot parse is treated as remote: fail closed.
        return True

    return False


def install_socket_guard(monkeypatch: Any, *, allow_loopback: bool = False) -> None:
    """Patch the socket primitives so outbound IP connections raise.

    Patches all three of socket.socket.connect, socket.socket.connect_ex and
    socket.create_connection. All three are required: patching only the methods
    misses socket.create_connection, which is the module-level helper httpx and
    urllib actually call, and patching only create_connection misses code that
    builds a socket by hand. connect_ex is included because it is the variant that
    reports failure by return code rather than exception, so a library probing with
    it would otherwise dial out silently.

    connect_ex deliberately RAISES here rather than returning an errno. A test that
    reaches the network is a defect to surface loudly, not a condition to be handled.

    allow_loopback defaults to False. Nothing in this suite needs to dial even the
    local machine -- the FastAPI tests drive the app in-process through Starlette's
    TestClient, which uses no sockets at all -- so the strict default keeps the
    guarantee simple. Pass True only for a test that genuinely binds a local server.

    monkeypatch is typed Any rather than pytest.MonkeyPatch so this module stays
    import-clean of pytest; only monkeypatch.setattr is used, so pytest handles
    teardown and no state is restored by hand.
    """
    # Capture the originals BEFORE patching, and call these on the allow path --
    # calling through the patched names instead would recurse forever.
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_create_connection = socket.create_connection

    error_template = (
        "blocked network connection to {dest} during tests.\n"
        "The memd suite is hermetic by design -- see tests/netguard.py. A test that "
        "needs HTTP must mock it (respx / pytest-httpx / a fake transport), never dial "
        "out. If you are seeing this after adding a feature, that feature is reaching "
        "the network from inside a test."
    )

    def _raise_network_error(address: Any) -> None:
        raise NetworkBlockedInTests(
            error_template.format(dest=_get_destination_info(address))
        )

    def _patched_connect(self: socket.socket, address: Any, *args: Any) -> None:
        if _is_blocked(self.family, address, allow_loopback):
            _raise_network_error(address)
        return orig_connect(self, address, *args)

    def _patched_connect_ex(self: socket.socket, address: Any, *args: Any) -> int:
        if _is_blocked(self.family, address, allow_loopback):
            _raise_network_error(address)
        return orig_connect_ex(self, address, *args)

    def _patched_create_connection(
        address: Any, *args: Any, **kwargs: Any
    ) -> socket.socket:
        # There is no socket object yet, so infer the family from the address shape:
        # a 4-tuple is the IPv6 (host, port, flow, scope) form. The distinction only
        # matters for readability -- both families follow the same rule. Written as a
        # conditional expression so the isinstance() check does not narrow `address`
        # for the passthrough call below, whose signature is stricter than Any.
        family = (
            socket.AF_INET6
            if (isinstance(address, tuple) and len(address) >= 4)
            else socket.AF_INET
        )

        if _is_blocked(family, address, allow_loopback):
            _raise_network_error(address)
        # cast: the isinstance() narrowing above leaves `address` as a tuple union,
        # which is stricter than create_connection's declared parameter type.
        return orig_create_connection(cast(Any, address), *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _patched_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _patched_connect_ex)
    monkeypatch.setattr(socket, "create_connection", _patched_create_connection)
