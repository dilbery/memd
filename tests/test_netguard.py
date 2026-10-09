"""Tests for the network hermeticity ceiling in tests/netguard.py.

These verify that the socket guard actually blocks outbound IP connections, and --
just as important -- that it does NOT block the local IPC that unrelated tooling
depends on.

Note the guard under test is ALREADY ACTIVE inside every test here, installed by the
autouse `_no_outbound_network` fixture in conftest.py. So this file never calls
`install_socket_guard` itself, and deliberately does not import it: installing a
second guard inside a test would stack on top of the ambient one, and a nested
`allow_loopback=True` would still be refused by the outer strict guard, failing for a
reason that has nothing to do with the behaviour under test.

That is why the allow_loopback matrix is tested through the pure `_is_blocked`
helper rather than end-to-end.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.netguard import NetworkBlockedInTests, _is_blocked

# RFC 5737 TEST-NET-1. Reserved for documentation and guaranteed not to be routed, so
# if the guard ever fails open these tests still cannot touch a real host.
BLOCKED_HOST = "192.0.2.1"


def test_socket_connect_to_remote_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(NetworkBlockedInTests):
            s.connect((BLOCKED_HOST, 80))


def test_create_connection_to_remote_is_blocked() -> None:
    # create_connection is the module-level helper httpx and urllib actually call, so
    # it needs its own coverage -- patching only socket.socket.connect would miss it.
    with pytest.raises(NetworkBlockedInTests):
        socket.create_connection((BLOCKED_HOST, 80), timeout=1)


def test_connect_ex_raises_rather_than_returning_errno() -> None:
    # connect_ex normally reports failure by RETURN CODE rather than by raising. The
    # guard deliberately raises anyway: a test reaching the network is a defect to
    # surface loudly, and a caller ignoring the errno would leak silently.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(NetworkBlockedInTests):
            s.connect_ex((BLOCKED_HOST, 80))


def test_error_message_names_the_destination_and_explains() -> None:
    """Pin the message text -- it is what a future developer reads when blocked."""
    with pytest.raises(NetworkBlockedInTests) as excinfo:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((BLOCKED_HOST, 80))
    msg = str(excinfo.value)
    assert f"{BLOCKED_HOST}:80" in msg
    assert "hermetic by design" in msg
    assert "tests/netguard.py" in msg


def test_httpx_cannot_reach_the_network() -> None:
    # The guarantee is that no request leaves the machine; which exception surfaces is
    # an httpx implementation detail, since it wraps transport failures in its own
    # hierarchy. Accept either.
    with httpx.Client() as client:
        with pytest.raises((NetworkBlockedInTests, httpx.TransportError)):
            client.get(f"http://{BLOCKED_HOST}/")


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="AF_UNIX not supported on this platform"
)
def test_unix_socket_is_not_blocked(tmp_path: Path) -> None:
    # AF_UNIX is local IPC -- it cannot leave the machine, and blocking it would break
    # unrelated tooling. Reaching FileNotFoundError proves the call went through to the
    # real syscall rather than being refused by the guard.
    sock_path = tmp_path / "does-not-exist.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        with pytest.raises(FileNotFoundError):
            s.connect(str(sock_path))


@pytest.mark.parametrize(
    "family, address, allow_loopback, expected",
    [
        (socket.AF_INET, ("1.2.3.4", 80), False, True),
        (socket.AF_INET, ("127.0.0.1", 80), False, True),
        (socket.AF_INET, ("127.0.0.1", 80), True, False),
        (socket.AF_INET, ("localhost", 80), True, False),
        (socket.AF_INET6, ("::1", 80, 0, 0), True, False),
        (socket.AF_INET6, ("2001:db8::1", 80, 0, 0), True, True),
        (socket.AF_INET, "not-a-tuple", True, True),
    ],
    ids=[
        "inet_remote",
        "inet_loopback_strict_default",
        "inet_loopback_allowed",
        "inet_localhost_allowed",
        "inet6_loopback_allowed",
        "inet6_remote",
        "inet_unparseable_fails_closed",
    ],
)
def test_is_blocked_matrix(
    family: int, address: Any, allow_loopback: bool, expected: bool
) -> None:
    assert _is_blocked(family, address, allow_loopback) == expected


def test_unknown_family_passes_through() -> None:
    # The guard is about IP egress. Refusing families nobody has reasoned about would
    # cause confusing failures in unrelated code.
    af_netlink = getattr(socket, "AF_NETLINK", 999)
    assert _is_blocked(af_netlink, ("x", 1), False) is False
