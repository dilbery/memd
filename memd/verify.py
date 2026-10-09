"""Re-verify changeable facts with declared, read-only probes (PROPOSE-ONLY by default).

A note that states something checkable (a port is open, a health endpoint
answers, a name resolves to an address, a binary is installed, a file exists)
can declare how to check it in its frontmatter:

    verify:
    - tcp: gpuhost:8077
    - http: https://memd.example.com/health
      status: 200
      json: status=ok
    - dns: host.example.com -> 10.10.1.10
    - command: some-binary
    - path: /etc/foo

Probes are declared, never inferred from prose, and every kind is read-only:
``tcp`` only opens and closes a connection, ``http`` sends one GET with no body
and no credentials, ``dns`` asks the system resolver, ``command`` looks the name
up on PATH (never runs it) and ``path`` tests existence. Anything else is
rejected with a message naming the accepted kinds.

Network probes are fenced by an allowlist (``MEMD_VERIFY_ALLOW``: addresses,
CIDRs and domains). Without one, only loopback, RFC 1918 and ULA addresses, and
names that resolve only to them, may be probed. HTTP redirects are followed
manually and each hop is checked again, so a redirect cannot leave the fence.
Timeouts are bounded and a run probes at most ``MEMD_VERIFY_MAX_PROBES``.

``mem-verify`` runs the probes on the machine it runs on and proposes, for each
note, either ``verified_at: <today>`` (every probe passed) or a
``verification: {status: failed, ...}`` marker naming the failing probes. The
proposals are committed to a review branch exactly like mem-summarize (a private
index under the clone lock; checkout, HEAD and concurrent saves untouched);
``--apply`` instead commits them through the save path's write and commit steps.
Recall labels a note with a recent failed verification as possibly stale
(memd.staleness).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from memd.config import Config, host_name
from memd.staleness import _parse_date
from memd.store import (Note, assert_readable_tree, clone_lock, dump_note,
                        list_notes)

GENERATOR = "mem-verify"
DEFAULT_BRANCH = "memd/verify"      # MEMD_VERIFY_BRANCH overrides
KINDS = ("command", "dns", "http", "path", "tcp")
MAX_PER_NOTE = 10                   # probes one note may declare
MAX_PROBES = 50                     # probes per run (MEMD_VERIFY_MAX_PROBES, --max-probes)
DEFAULT_TIMEOUT_S = 3.0             # per probe step (MEMD_VERIFY_TIMEOUT_S), clamped below
TIMEOUT_RANGE = (0.5, 10.0)
MAX_REDIRECTS = 3
MAX_BODY_BYTES = 65536              # JSON bodies read for a `json` expectation
MAX_TARGET_CHARS = 512

# Without MEMD_VERIFY_ALLOW: loopback, RFC 1918 and IPv6 ULA only. Link-local
# (cloud metadata lives there), public and unspecified addresses are refused.
DEFAULT_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7"))

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*\.?$")
_COMMAND = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+-]{0,127}$")
_JSON_KEY = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")


class ProbeError(ValueError):
    """A `verify` declaration that is not a valid, safe probe."""


class NotAllowed(Exception):
    """A probe target outside the allowlist; the probe is not attempted."""


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    kind: str
    target: str                     # as declared (canonical text)
    host: str = ""                  # tcp/dns: the name or address
    port: int = 0                   # tcp
    addresses: tuple[str, ...] = () # dns: expected addresses
    status: int | None = None       # http: expected status (default any 2xx)
    json_key: str | None = None     # http: dotted key that must be present
    json_value: str | None = None   # http: ... and equal to this (as text)

    def describe(self) -> str:
        text = f"{self.kind}: {self.target}"
        if self.status is not None:
            text += f" (status {self.status})"
        if self.json_key:
            text += f" (json {self.json_key}{'=' + self.json_value if self.json_value is not None else ''})"
        return text

    def to_frontmatter(self) -> dict:
        out: dict[str, Any] = {self.kind: self.target}
        if self.status is not None:
            out["status"] = self.status
        if self.json_key:
            out["json"] = self.json_key + (f"={self.json_value}" if self.json_value is not None else "")
        return out


def _split_host_port(text: str) -> tuple[str, int]:
    if text.startswith("["):
        host, sep, rest = text[1:].partition("]")
        if not sep or not rest.startswith(":"):
            raise ProbeError(f"tcp target {text!r} must be host:port ([v6]:port for IPv6)")
        port_text = rest[1:]
    else:
        host, sep, port_text = text.rpartition(":")
        if not sep or not host:
            raise ProbeError(f"tcp target {text!r} must be host:port")
        if ":" in host:
            raise ProbeError(f"tcp target {text!r}: write IPv6 addresses as [addr]:port")
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise ProbeError(f"tcp target {text!r} needs a port between 1 and 65535")
    return _valid_host(host, text), int(port_text)


def _valid_host(host: str, context: str) -> str:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if not _HOSTNAME.match(host) or len(host) > 253:
        raise ProbeError(f"{context!r} does not name a valid host")
    return host.rstrip(".").casefold()


def _http_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ProbeError(f"http target {url!r} is not a valid URL: {exc}") from exc
    if parts.scheme not in ("http", "https"):
        raise ProbeError(f"http target {url!r} must be an http:// or https:// URL")
    if parts.username is not None or parts.password is not None:
        raise ProbeError(f"http target {url!r} must not carry credentials")
    if not parts.hostname:
        raise ProbeError(f"http target {url!r} has no host")
    _valid_host(parts.hostname, url)
    if port is not None and not 0 < port < 65536:
        raise ProbeError(f"http target {url!r} has an invalid port")
    return url


def parse_probe(item: Any) -> Probe:
    """One declaration -> Probe. Accepts ``{kind: target, ...}`` or ``"kind: target"``."""
    if isinstance(item, str):
        kind, sep, target = item.partition(":")
        if not sep:
            raise ProbeError(f"probe {item!r} must be written as 'kind: target' "
                             f"with kind one of {', '.join(KINDS)}")
        item = {kind.strip(): target.strip()}
    if not isinstance(item, dict) or not item:
        raise ProbeError(f"probe must be a mapping like {{tcp: host:port}}, got {type(item).__name__}")
    kinds = [k for k in item if k in KINDS]
    if len(kinds) != 1:
        unknown = [str(k) for k in item if k not in KINDS and k not in ("status", "json")]
        if unknown and not kinds:
            raise ProbeError(f"unknown probe kind {unknown[0]!r}; expected one of: {', '.join(KINDS)}")
        raise ProbeError(f"probe {item!r} must name exactly one kind of: {', '.join(KINDS)}")
    kind = kinds[0]
    options = {k: v for k, v in item.items() if k != kind}
    allowed_options = {"status", "json"} if kind == "http" else set()
    extra = sorted(str(k) for k in options if k not in allowed_options)
    if extra:
        raise ProbeError(f"{kind} probe does not accept {', '.join(extra)}"
                         + ("; http accepts status and json" if kind == "http" else ""))
    target = item[kind]
    if not isinstance(target, str) or not target.strip():
        raise ProbeError(f"{kind} probe needs a text target")
    target = target.strip()
    if len(target) > MAX_TARGET_CHARS or "\0" in target or any(c in target for c in "\r\n"):
        raise ProbeError(f"{kind} probe target is too long or contains control characters")

    if kind == "tcp":
        host, port = _split_host_port(target)
        return Probe(kind, target, host=host, port=port)
    if kind == "dns":
        name, arrow, expected = target.partition("->")
        name = _valid_host(name.strip(), target)
        addresses: list[str] = []
        for text in [t for t in expected.split(",") if t.strip()] if arrow else []:
            try:
                addresses.append(str(ipaddress.ip_address(text.strip())))
            except ValueError as exc:
                raise ProbeError(f"dns probe {target!r}: {text.strip()!r} is not an IP address") from exc
        if arrow and not addresses:
            raise ProbeError(f"dns probe {target!r}: expected an address after '->'")
        return Probe(kind, target, host=name, addresses=tuple(addresses))
    if kind == "command":
        if not _COMMAND.match(target):
            raise ProbeError(f"command probe {target!r} must be a bare command name "
                             "(no paths, arguments or shell syntax)")
        return Probe(kind, target)
    if kind == "path":
        if not os.path.isabs(target):
            raise ProbeError(f"path probe {target!r} must be an absolute path")
        return Probe(kind, target)
    # http
    _http_url(target)
    status = options.get("status")
    if status is not None:
        if isinstance(status, bool) or not isinstance(status, (int, str)) or not str(status).isdigit() \
                or not 100 <= int(status) <= 599:
            raise ProbeError(f"http probe {target!r}: status must be an HTTP status code")
        status = int(status)
    json_key = json_value = None
    if "json" in options:
        spec = options["json"]
        if not isinstance(spec, str) or not spec.strip():
            raise ProbeError(f"http probe {target!r}: json must be 'key' or 'key=value'")
        key, eq, value = spec.strip().partition("=")
        if not _JSON_KEY.match(key.strip()):
            raise ProbeError(f"http probe {target!r}: json key {key.strip()!r} must be a dotted key")
        json_key, json_value = key.strip(), (value.strip() if eq else None)
    return Probe(kind, target, status=status, json_key=json_key, json_value=json_value)


def parse_verify(value: Any) -> list[Probe]:
    """A note's `verify` frontmatter -> probes; raises ProbeError naming the bad entry."""
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        raise ProbeError("verify must be a list of probes")
    if len(value) > MAX_PER_NOTE:
        raise ProbeError(f"verify declares {len(value)} probes; at most {MAX_PER_NOTE} per note")
    probes = []
    for number, item in enumerate(value, 1):
        try:
            probes.append(parse_probe(item))
        except ProbeError as exc:
            raise ProbeError(f"verify[{number}]: {exc}") from None
    return probes


def canonical(value: Any) -> list[dict]:
    """Validated, canonical frontmatter for a `verify` value (save's normalizer)."""
    return [p.to_frontmatter() for p in parse_verify(value)]


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    ip = ipaddress.ip_address(text.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


@dataclass(frozen=True)
class Allowlist:
    networks: tuple = DEFAULT_NETWORKS
    domains: tuple[str, ...] = ()
    configured: bool = False

    @classmethod
    def parse(cls, raw: str | None) -> "Allowlist":
        """``MEMD_VERIFY_ALLOW``: comma/space separated addresses, CIDRs and domains.

        A domain entry allows that name and its subdomains (``*.`` is optional).
        When set, it replaces the private-address default entirely.
        """
        entries = [e.strip() for e in re.split(r"[,\s]+", raw or "") if e.strip()]
        if not entries:
            return cls()
        networks, domains = [], []
        for entry in entries:
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
                continue
            except ValueError:
                pass
            name = entry.removeprefix("*.").lstrip(".").rstrip(".").casefold()
            if not name or not _HOSTNAME.match(name):
                raise ValueError(f"MEMD_VERIFY_ALLOW entry {entry!r} is not an address, CIDR or domain")
            domains.append(name)
        return cls(tuple(networks), tuple(domains), True)

    def address_allowed(self, text: str) -> bool:
        try:
            ip = _address(text)
        except ValueError:
            return False
        return any(ip.version == n.version and ip in n for n in self.networks)

    def name_allowed(self, name: str) -> bool:
        name = name.rstrip(".").casefold()
        return any(name == d or name.endswith("." + d) for d in self.domains)

    def describe(self) -> str:
        if not self.configured:
            return "loopback, RFC 1918 and ULA addresses (MEMD_VERIFY_ALLOW unset)"
        return ", ".join([*map(str, self.networks), *self.domains])


Resolver = Callable[[str, float], list[str]]


def system_resolver(name: str, timeout: float) -> list[str]:
    """Addresses for ``name`` from the system resolver, bounded by ``timeout``."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(socket.getaddrinfo, name, None, 0, socket.SOCK_STREAM)
        infos = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise OSError(f"resolving {name} timed out after {timeout:g}s") from None
    except socket.gaierror as exc:
        raise OSError(f"{name} does not resolve: {exc.strerror or exc}") from None
    finally:
        pool.shutdown(wait=False)
    out: list[str] = []
    for info in infos:
        address = str(_address(info[4][0]))
        if address not in out:
            out.append(address)
    return out


def check_target(host: str, allow: Allowlist, resolver: Resolver, timeout: float) -> list[str]:
    """Addresses of an allowed target; raises NotAllowed, or OSError if it does not resolve.

    An address literal must be inside the allowlist. A name is allowed when it
    is an allowlisted domain, or when every address it resolves to is allowed.
    """
    try:
        ip = _address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not allow.address_allowed(str(ip)):
            raise NotAllowed(f"{ip} is outside the verify allowlist ({allow.describe()})")
        return [str(ip)]
    addresses = resolver(host, timeout)
    if not addresses:
        raise OSError(f"{host} does not resolve")
    if allow.name_allowed(host):
        return addresses
    outside = [a for a in addresses if not allow.address_allowed(a)]
    if outside:
        raise NotAllowed(f"{host} resolves to {', '.join(outside)}, outside the verify "
                         f"allowlist ({allow.describe()})")
    return addresses


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

PASS, FAIL, BLOCKED = "pass", "fail", "blocked"


@dataclass
class Result:
    probe: Probe
    outcome: str        # pass | fail | blocked (not attempted: outside the allowlist)
    detail: str = ""

    def to_dict(self) -> dict:
        return {"probe": self.probe.describe(), "outcome": self.outcome, "detail": self.detail}


def _connect(address: str, port: int, timeout: float) -> None:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect((address, port))


def _json_lookup(data: Any, key: str) -> tuple[bool, Any]:
    for part in key.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return False, None
    return True, data


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


@dataclass
class Prober:
    """Runs probes. Every side door (resolver, connect, HTTP client) is injectable."""

    allow: Allowlist = field(default_factory=Allowlist)
    timeout: float = DEFAULT_TIMEOUT_S
    resolver: Resolver = system_resolver
    connect: Callable[[str, int, float], None] = _connect
    which: Callable[[str], str | None] = shutil.which
    exists: Callable[[str], bool] = os.path.exists
    client: Any = None              # httpx.Client; built lazily

    def run(self, probe: Probe) -> Result:
        try:
            return getattr(self, f"_{probe.kind}")(probe)
        except NotAllowed as exc:
            return Result(probe, BLOCKED, str(exc))
        except Exception as exc:    # a probe never raises out of a run
            detail = str(exc) if isinstance(exc, OSError) else f"{type(exc).__name__}: {exc}"
            return Result(probe, FAIL, detail or type(exc).__name__)

    def _command(self, probe: Probe) -> Result:
        found = self.which(probe.target)
        return Result(probe, PASS, found) if found else Result(probe, FAIL, "not found on PATH")

    def _path(self, probe: Probe) -> Result:
        return (Result(probe, PASS, "exists") if self.exists(probe.target)
                else Result(probe, FAIL, "does not exist"))

    def _tcp(self, probe: Probe) -> Result:
        host = host_name(probe.host)   # placeholder roles map through MEMD_HOST_NAMES
        errors = []
        for address in check_target(host, self.allow, self.resolver, self.timeout):
            try:
                self.connect(address, probe.port, self.timeout)
                return Result(probe, PASS, f"connected to {address}:{probe.port}")
            except OSError as exc:
                errors.append(f"{address}:{probe.port} {exc.strerror or exc or 'unreachable'}")
        return Result(probe, FAIL, "; ".join(errors) or "no address")

    def _dns(self, probe: Probe) -> Result:
        # A lookup sends nothing to the target, but the fence still applies: the
        # name must be allowlisted, or the expected (else the actual) addresses inside it.
        if self.allow.name_allowed(probe.host):
            resolved = self.resolver(probe.host, self.timeout)
        elif probe.addresses:
            outside = [a for a in probe.addresses if not self.allow.address_allowed(a)]
            if outside:
                raise NotAllowed(f"{probe.host} -> {', '.join(outside)} is outside the "
                                 f"verify allowlist ({self.allow.describe()})")
            resolved = self.resolver(probe.host, self.timeout)
        else:
            resolved = check_target(probe.host, self.allow, self.resolver, self.timeout)
        if not resolved:
            return Result(probe, FAIL, f"{probe.host} does not resolve")
        missing = [a for a in probe.addresses if a not in resolved]
        if missing:
            return Result(probe, FAIL, f"{probe.host} resolves to {', '.join(resolved)}, "
                                       f"not {', '.join(missing)}")
        return Result(probe, PASS, f"{probe.host} -> {', '.join(resolved)}")

    def _http_client(self):
        if self.client is None:
            import httpx
            # No proxies from the environment: the fence applies to the target itself.
            self.client = httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False,
                                       headers={"User-Agent": GENERATOR})
        return self.client

    def _http(self, probe: Probe) -> Result:
        import httpx
        url = probe.target
        client = self._http_client()
        for hop in range(MAX_REDIRECTS + 1):
            _http_url(url)
            target = httpx.URL(url)
            address = check_target(target.host, self.allow, self.resolver, self.timeout)[0]
            # Connect to the address that was checked, not to whatever a second lookup
            # returns (DNS rebinding). The name still goes in Host, and in SNI and the
            # certificate check for https.
            extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
            deadline = time.monotonic() + 2 * self.timeout
            with client.stream("GET", target.copy_with(host=address),
                               headers={"Host": target.netloc.decode("ascii")},
                               extensions=extensions) as response:
                status = response.status_code
                if response.is_redirect and status != probe.status:
                    location = response.headers.get("location", "")
                    if hop == MAX_REDIRECTS:
                        return Result(probe, FAIL, f"more than {MAX_REDIRECTS} redirects")
                    url = str(target.join(location))
                    continue
                body = b""
                if probe.json_key:
                    # The client timeout bounds each read; this bounds the whole body.
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > MAX_BODY_BYTES:
                            return Result(probe, FAIL, f"response larger than {MAX_BODY_BYTES} bytes")
                        if time.monotonic() > deadline:
                            return Result(probe, FAIL, "response body too slow")
            break
        where = "" if url == probe.target else f" (after redirect to {url})"
        ok_status = status == probe.status if probe.status is not None else 200 <= status < 300
        if not ok_status:
            want = probe.status if probe.status is not None else "2xx"
            return Result(probe, FAIL, f"HTTP {status}, expected {want}{where}")
        if probe.json_key:
            try:
                data = json.loads(body)
            except ValueError:
                return Result(probe, FAIL, f"HTTP {status} but the body is not JSON{where}")
            found, value = _json_lookup(data, probe.json_key)
            if not found:
                return Result(probe, FAIL, f"JSON has no {probe.json_key!r}{where}")
            if probe.json_value is not None and _as_text(value) != probe.json_value:
                # Never echo the value: the detail is committed to the note's frontmatter.
                return Result(probe, FAIL, f"JSON {probe.json_key} is not the expected "
                                           f"{probe.json_value!r}{where}")
        return Result(probe, PASS, f"HTTP {status}{where}")

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


def _rel(clone: Path, path: str) -> str:
    p = Path(path)
    return p.relative_to(clone).as_posix() if p.is_absolute() else p.as_posix()


def last_checked(note: Note) -> dt.date | None:
    """When this note was last verified or found failing."""
    dates = [_parse_date(note.verified_at)]
    marker = note.metadata.get("verification")
    if isinstance(marker, dict):
        dates.append(_parse_date(marker.get("checked_at")))
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def propose(note: Note, results: list[Result], today: dt.date) -> Note | None:
    """The note as it should read after this check, or None when nothing changes."""
    metadata = dict(note.metadata)
    marker = metadata.pop("verification", None)
    failed = [f"{r.probe.describe()}: {r.detail}" for r in results if r.outcome == FAIL]
    if failed:
        since = marker.get("since") if isinstance(marker, dict) and marker.get("status") == "failed" else None
        metadata["verification"] = {
            "status": "failed", "checked_at": today.isoformat(),
            "since": str(since or today.isoformat()), "failed": failed,
        }
        if marker == metadata["verification"]:
            return None
        return dataclasses.replace(note, metadata=metadata, saved_by=GENERATOR)
    if any(r.outcome != PASS for r in results) or not results:
        return None
    if marker is None and str(note.verified_at or "") == today.isoformat():
        return None
    return dataclasses.replace(note, verified_at=today.isoformat(), metadata=metadata,
                               saved_by=GENERATOR)


def branch_name() -> str:
    return os.environ.get("MEMD_VERIFY_BRANCH", "").strip() or DEFAULT_BRANCH


def _env_number(name: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(os.environ.get(name, "") or default)))
    except ValueError:
        return default


def apply_changes(cfg: Config, changes: list[tuple[Note, Note]], *, message: str,
                  generator: str = GENERATOR,
                  changed: str = "note changed since it was probed") -> dict:
    """Commit proposals directly, through the save path's guarded write and commit.

    Holds the clone lock like save(); a note whose blob changed since it was
    probed is skipped (the same guard as save's expected_revision); a failed
    commit restores the files and index, and the commit is indexed and synced
    exactly as a save's. mem-forget restore uses it too (generator, changed).
    """
    from memd.save import _CommitFailure, _commit_and_push, _restore_bytes, _write_note
    clone = Path(cfg.clone)
    applied, skipped = [], []
    with clone_lock(clone):
        assert_readable_tree(clone)
        current = {n.slug: n for n in list_notes(clone)}
        index_path = clone / ".git" / "index"
        index_before = index_path.read_bytes() if index_path.exists() else None
        snapshots: dict[Path, bytes] = {}
        try:
            for before, after in changes:
                now = current.get(before.slug)
                if now is None or now.git_blob != before.git_blob:
                    skipped.append({"slug": before.slug, "reason": changed})
                    continue
                path = Path(now.path)
                snapshots[path] = path.read_bytes()
                _write_note(clone, dataclasses.asdict(after), path=now.path)
                applied.append(before.slug)
        except Exception:
            for target, data in snapshots.items():
                _restore_bytes(target, data)
            raise
        if not snapshots:
            return {"applied": [], "skipped": skipped, "commit": None, "warnings": []}
        try:
            receipt = _commit_and_push(cfg, f"{message}\n\nSaved-By: {generator}",
                                       paths=list(snapshots))
        except _CommitFailure as error:
            for target, data in snapshots.items():
                _restore_bytes(target, data)
            _restore_bytes(index_path, index_before)
            raise error.__cause__ from error
    from memd.store import git_head_sha
    return {"applied": applied, "skipped": skipped, "commit": git_head_sha(clone),
            "synced": receipt.synced, "warnings": receipt.warnings}


def run(cfg: Config, *, dry_run: bool = False, apply: bool = False,
        max_probes: int | None = None, branch: str | None = None,
        today: dt.date | None = None, prober: Prober | None = None) -> dict:
    """Probe every live note that declares `verify`, least recently checked first."""
    clone = Path(cfg.clone)
    branch = branch or branch_name()
    today = today or dt.datetime.now(dt.timezone.utc).date()
    if max_probes is None:
        max_probes = int(_env_number("MEMD_VERIFY_MAX_PROBES", MAX_PROBES, 0, 1000))
    own_prober = prober is None
    if prober is None:
        prober = Prober(allow=Allowlist.parse(os.environ.get("MEMD_VERIFY_ALLOW")),
                        timeout=_env_number("MEMD_VERIFY_TIMEOUT_S", DEFAULT_TIMEOUT_S, *TIMEOUT_RANGE))
    candidates = [n for n in list_notes(clone)
                  if "verify" in n.metadata and not n.superseded_by]
    candidates.sort(key=lambda n: (last_checked(n) or dt.date.min, n.slug))
    budget = max(0, max_probes)
    rows, changes = [], []
    try:
        for note in candidates:
            row: dict[str, Any] = {"slug": note.slug, "path": _rel(clone, note.path),
                                   "status": "", "probes": [], "error": None}
            rows.append(row)
            try:
                probes = parse_verify(note.metadata.get("verify"))
            except ProbeError as exc:
                row.update(status="invalid", error=str(exc))
                continue
            if not probes:
                row["status"] = "empty"
                continue
            if len(probes) > budget:
                row["status"] = "deferred"
                continue
            budget -= len(probes)
            results = [prober.run(p) for p in probes]
            row["probes"] = [r.to_dict() for r in results]
            if any(r.outcome == FAIL for r in results):
                row["status"] = "failed"
            elif any(r.outcome == BLOCKED for r in results):
                row["status"] = "blocked"
            else:
                row["status"] = "verified"
            after = propose(note, results, today)
            if after is None:
                row["unchanged"] = row["status"] in ("verified", "failed")
                continue
            changes.append((note, after, row))
    finally:
        if own_prober:
            prober.close()

    texts = {row["path"]: dump_note(after) for _, after, row in changes}
    for _, _, row in changes:
        row["proposed"] = True
    report: dict[str, Any] = {"dry_run": dry_run, "applied": False, "branch": None, "commit": None,
                              "proposed": len(texts), "notes": rows, "_texts": texts}
    verified = sum(1 for *_, r in changes if r["status"] == "verified")
    failed = len(changes) - verified
    message = f"verify: {verified} confirmed, {failed} failing ({today.isoformat()})"
    if not texts or dry_run:
        return report
    if apply:
        outcome = apply_changes(cfg, [(before, after) for before, after, _ in changes],
                                message=f"memd: {message}")
        report.update(applied=True, commit=outcome["commit"], skipped=outcome["skipped"],
                      warnings=outcome["warnings"])
        return report
    from memd.summarize import write_proposals
    report.update(branch=branch, commit=write_proposals(clone, texts, branch=branch,
                                                        message=f"{message}\n\nProposed-By: {GENERATOR}"))
    return report


def _print_report(report: dict, *, show_text: bool) -> None:
    for row in report["notes"]:
        line = f"{row['status']:<9} {row['slug']}"
        if row.get("unchanged"):
            line += " (already recorded)"
        if row["error"]:
            line += f": {row['error']}"
        print(line)
        for probe in row["probes"]:
            print(f"    {probe['outcome']:<7} {probe['probe']}" + (f" -- {probe['detail']}" if probe["detail"] else ""))
    if show_text:
        for path, text in report["_texts"].items():
            print(f"\n===== {path} =====\n{text}", end="")
    if report["dry_run"]:
        print(f"\ndry-run: {report['proposed']} change(s), nothing written.")
    elif report["applied"]:
        print(f"\napplied {report['proposed']} change(s) ({(report['commit'] or '')[:12]}).")
        for skip in report.get("skipped", []):
            print(f"    skipped {skip['slug']}: {skip['reason']}")
    elif report["commit"]:
        print(f"\nproposed {report['proposed']} change(s) on branch {report['branch']} "
              f"({report['commit'][:12]}). Review, then merge it into the store's branch to approve.")
    else:
        print("\nnothing to propose.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="mem-verify",
        description="Re-check notes' declared read-only probes from this machine and propose "
                    "verified_at / failed-verification updates on a review branch.")
    ap.add_argument("--dry-run", action="store_true", help="probe and print the changes; write nothing")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    ap.add_argument("--apply", action="store_true",
                    help="commit the changes to the store directly instead of proposing them")
    ap.add_argument("--max-probes", type=int, default=None,
                    help=f"probes per run (default MEMD_VERIFY_MAX_PROBES or {MAX_PROBES})")
    ap.add_argument("--branch", default=None, help=f"review branch (default {DEFAULT_BRANCH})")
    ap.add_argument("--push", action="store_true", help="push the review branch to origin")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if args.apply and args.dry_run:
        ap.error("--apply and --dry-run are mutually exclusive")

    cfg = Config.from_env()
    if cfg.clone is None:
        print("mem-verify: no clone configured (MEMD_CLONE / MEMD_PROFILE)", file=sys.stderr)
        return 2
    try:
        Allowlist.parse(os.environ.get("MEMD_VERIFY_ALLOW"))
    except ValueError as exc:
        print(f"mem-verify: {exc}", file=sys.stderr)
        return 2
    report = run(cfg, dry_run=args.dry_run, apply=args.apply, max_probes=args.max_probes,
                 branch=args.branch)
    if report["commit"] and report["branch"] and args.push:
        from memd.summarize import push_branch
        push_branch(Path(cfg.clone), report["branch"])
    if args.json:
        print(json.dumps({k: v for k, v in report.items() if k != "_texts"}))
    else:
        _print_report(report, show_text=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
