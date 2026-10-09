"""In-process Prometheus metrics (GET /metrics), stdlib only.

Counters and histograms are recorded on the request path with one lock and a
dict update each; gauges about stores (notes, pending vectors, head age, inbox)
are computed at scrape time from cheap read-only queries and cached briefly.

Cardinality is bounded by construction: every label of every family has a fixed
set of allowed values, and anything else is recorded as ``other``. The only
open-ended label is ``store`` on the scrape-time gauges; store names are chosen
by the operator, capped at MAX_STORES and only rendered for stores the caller
may see (memd.server decides which). Nothing here ever receives a query, a
slug, note text, a token or a caller label.

Contract:
  inc(name, value=1, **labels)           counter
  observe(name, seconds, **labels)       histogram
  timer(name, **labels)                  context manager around observe
  backend_call(backend)                  context manager: count, error kind, latency
  render(stores, store_gauges)           Prometheus text exposition format 0.0.4
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
MAX_STORES = 100
GAUGE_TTL = 15.0
START_TIME = time.time()

ROUTES = ("recall", "read", "save", "propose", "ask", "publish", "timeline", "mcp", "health",
          "stats", "metrics", "insights", "entities", "inbox", "handoff", "reindex", "ui",
          "admin", "clients", "other")
TOOLS = ("recall", "read", "save", "propose", "ask", "publish", "timeline", "other")
STATUS = ("1xx", "2xx", "3xx", "4xx", "5xx")
BACKENDS = ("embed", "rerank", "chat")
ERROR_KINDS = ("timeout", "connect", "http_4xx", "http_5xx", "bad_response", "other")
ARMS = ("embed", "vector", "bm25", "fresh", "rerank", "total")
FALLBACKS = ("embed_timeout", "embed_failed", "vector_empty", "rerank_skipped", "rerank_failed")
ORDERS = ("rerank", "vector", "bm25", "core_only")

# name -> (type, help, {label: allowed values})
_FAMILIES: dict[str, tuple[str, str, dict[str, tuple[str, ...]]]] = {
    "memd_http_requests_total": (
        "counter", "HTTP requests by route and status class.",
        {"route": ROUTES, "status": STATUS}),
    "memd_http_request_duration_seconds": (
        "histogram", "HTTP request latency by route.", {"route": ROUTES}),
    "memd_mcp_tool_calls_total": (
        "counter", "MCP tool calls by tool and outcome.",
        {"tool": TOOLS, "outcome": ("ok", "error")}),
    "memd_mcp_tool_duration_seconds": (
        "histogram", "MCP tool call latency by tool.", {"tool": TOOLS}),
    "memd_recall_arm_duration_seconds": (
        "histogram", "Time spent in each recall stage.", {"arm": ARMS}),
    "memd_recall_fallbacks_total": (
        "counter", "Recall degradations: embed timeout/failure, empty vector arm, rerank skipped/failed.",
        {"reason": FALLBACKS}),
    "memd_recall_order_total": (
        "counter", "Which ordering produced a recall's matches.", {"order": ORDERS}),
    "memd_backend_calls_total": (
        "counter", "Model backend calls by backend and outcome.",
        {"backend": BACKENDS, "outcome": ("ok", "error")}),
    "memd_backend_errors_total": (
        "counter", "Model backend call failures by kind.",
        {"backend": BACKENDS, "kind": ERROR_KINDS}),
    "memd_backend_call_duration_seconds": (
        "histogram", "Model backend call latency.", {"backend": BACKENDS}),
    "memd_saves_total": (
        "counter", "Saves by outcome (saved, or error when save raised).",
        {"outcome": ("saved", "not_saved", "error")}),
    "memd_save_receipts_total": (
        "counter", "Saved receipts reporting each completed stage.",
        {"stage": ("lexical_indexed", "indexed", "synced")}),
    "memd_save_conflicts_found_total": (
        "counter", "Advisory conflicts reported by saves.", {}),
    "memd_usage_log_events_total": (
        "counter", "Usage log writes by kind and result.",
        {"kind": ("recall", "read"), "result": ("logged", "failed")}),
}

_lock = threading.Lock()
_counters: dict[tuple, float] = {}
_histograms: dict[tuple, list] = {}     # key -> [bucket counts..., sum, count]


def _key(name: str, labels: dict) -> tuple:
    spec = _FAMILIES[name][2]
    return (name,) + tuple(labels.get(k) if labels.get(k) in allowed else "other"
                           for k, allowed in spec.items())


def inc(name: str, value: float = 1.0, **labels) -> None:
    """Add to a counter. Never raises: metrics must not fail a request."""
    try:
        key = _key(name, labels)
        with _lock:
            _counters[key] = _counters.get(key, 0.0) + value
    except Exception:
        pass


def observe(name: str, seconds: float, **labels) -> None:
    try:
        key = _key(name, labels)
        with _lock:
            h = _histograms.get(key)
            if h is None:
                h = _histograms[key] = [0] * len(BUCKETS) + [0.0, 0]
            for i, bound in enumerate(BUCKETS):
                if seconds <= bound:
                    h[i] += 1
            h[-2] += seconds
            h[-1] += 1
    except Exception:
        pass


@contextmanager
def timer(name: str, **labels):
    start = time.perf_counter()
    try:
        yield
    finally:
        observe(name, time.perf_counter() - start, **labels)


def error_kind(exc: BaseException) -> str:
    """A fixed error kind for a model backend failure (never its message)."""
    import httpx
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        return "connect"
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_5xx" if exc.response.status_code >= 500 else "http_4xx"
    cause = exc.__cause__
    if isinstance(cause, httpx.HTTPError):
        return error_kind(cause)
    # Adapters wrap failures in their own errors; only their fixed prefixes are read.
    text = str(exc).lower()[:80]
    if " 4xx" in text or "http 4" in text:
        return "http_4xx"
    if "http 5" in text:
        return "http_5xx"
    if "deadline" in text or "timeout" in text or "timed out" in text:
        return "timeout"
    if "unreachable" in text:
        return "connect"
    if isinstance(exc, (ValueError, KeyError, TypeError, RuntimeError)):
        return "bad_response"
    return "other"


class _Call:
    """Handle for backend_call: mark a call that returned but was unusable."""
    def __init__(self):
        self.failed: str | None = None

    def fail(self, kind: str) -> None:
        self.failed = kind


@contextmanager
def backend_call(backend: str):
    """Count one backend call, its failure kind and its latency. Re-raises."""
    call = _Call()
    start = time.perf_counter()
    try:
        yield call
    except BaseException as exc:
        call.failed = error_kind(exc)
        raise
    finally:
        observe("memd_backend_call_duration_seconds", time.perf_counter() - start, backend=backend)
        inc("memd_backend_calls_total", backend=backend, outcome="error" if call.failed else "ok")
        if call.failed:
            inc("memd_backend_errors_total", backend=backend, kind=call.failed)


def record_save(receipt) -> None:
    """Save outcome counters from a SaveResult (or its dict)."""
    get = receipt.get if isinstance(receipt, dict) else (lambda k, d=None: getattr(receipt, k, d))
    saved = bool(get("saved", False))
    inc("memd_saves_total", outcome="saved" if saved else "not_saved")
    if saved:
        for stage in ("lexical_indexed", "indexed", "synced"):
            if get(stage, False):
                inc("memd_save_receipts_total", stage=stage)
    conflicts = get("conflicts", None) or []
    if conflicts:
        inc("memd_save_conflicts_found_total", float(len(conflicts)))


def reset() -> None:
    """Forget recorded samples (tests)."""
    with _lock:
        _counters.clear()
        _histograms.clear()
    with _gauge_lock:
        _gauge_cache.clear()


# --------------------------------------------------------------------------- routes


_ROUTE_PREFIXES = {
    "recall": "recall", "read": "read", "save": "save", "propose": "propose", "ask": "ask",
    "publish": "publish", "timeline": "timeline", "mcp": "mcp", "health": "health",
    "stats": "stats", "metrics": "metrics", "insights": "insights", "entities": "entities",
    "inbox": "inbox", "handoff": "handoff", "reindex": "reindex", "ui": "ui", "admin": "admin",
    "api": "admin", "memories": "ui", "sso": "ui", "clients": "clients", "help": "ui",
}


def route_for(path: str, method: str, accept: str = "") -> str:
    """A fixed route label for a request path; the raw path is never a label."""
    path = path or "/"
    if path == "/":
        # The bare root is MCP unless it is a browser GET (memd.server.RootMcpDispatch).
        return "mcp" if method != "GET" or "text/event-stream" in accept.lower() else "ui"
    first = path.lstrip("/").split("/", 1)[0]
    return _ROUTE_PREFIXES.get(first, "other")


class MetricsMiddleware:
    """ASGI middleware: count and time every HTTP request by route and status class."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        accept = ""
        for name, value in scope.get("headers") or []:
            if name.lower() == b"accept":
                accept = value.decode("latin-1")
                break
        route = route_for(scope.get("path", ""), scope.get("method", "GET"), accept)
        status = {"code": 500}
        start = time.perf_counter()

        async def capture(message):
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status", 500))
            await send(message)
        try:
            await self.app(scope, receive, capture)
        finally:
            observe("memd_http_request_duration_seconds", time.perf_counter() - start, route=route)
            inc("memd_http_requests_total", route=route, status=f"{status['code'] // 100}xx")


# --------------------------------------------------------------------------- scrape-time gauges


_gauge_lock = threading.Lock()
_gauge_cache: dict[str, tuple[float, dict]] = {}


def _ro(path: Path) -> sqlite3.Connection:
    # Read-only: a scrape must never create, migrate or lock an index for writing.
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)


def _commit_time(clone: Path, sha: str) -> float | None:
    if not sha or not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        return None
    try:
        out = subprocess.run(["git", "-C", str(clone), "show", "-s", "--format=%ct", sha],
                             capture_output=True, text=True, timeout=2.0)
        return float(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def store_gauges(store: str, clone: Path | None, db: Path | None, *, fresh: bool = False) -> dict:
    """Numbers about one store's index and inbox; {} when it has no index yet.

    Cached for GAUGE_TTL seconds per store so a busy scraper costs a few queries.
    """
    now = time.monotonic()
    if not fresh:
        with _gauge_lock:
            cached = _gauge_cache.get(store)
        if cached and now - cached[0] < GAUGE_TTL:
            return cached[1]
    out: dict[str, float] = {}
    if db is not None and Path(db).exists():
        try:
            conn = _ro(db)
            try:
                out["notes"] = conn.execute(
                    "SELECT COUNT(*) FROM notes WHERE superseded_by IS NULL").fetchone()[0]
                out["pending_vectors"] = conn.execute(
                    "SELECT COUNT(*) FROM notes WHERE superseded_by IS NULL AND "
                    "(vector_blob IS NULL OR vector_blob != git_blob)").fetchone()[0]
                out["pending_chunks"] = conn.execute(
                    "SELECT COUNT(*) FROM notes WHERE superseded_by IS NULL AND "
                    "(chunk_blob IS NULL OR chunk_blob != git_blob)").fetchone()[0]
                row = conn.execute("SELECT value FROM meta WHERE key='lexical_head'").fetchone()
                indexed_head = row[0] if row else ""
            finally:
                conn.close()
            out["size_bytes"] = Path(db).stat().st_size
            if clone is not None:
                from memd.store import git_head_sha
                head = git_head_sha(clone)
                out["in_sync"] = 1.0 if head and head == indexed_head else 0.0
                when = _commit_time(clone, indexed_head)
                if when is not None:
                    out["head_age_seconds"] = max(0.0, time.time() - when)
        except Exception:
            out = {}
        inbox = Path(db).with_name(Path(db).stem + ".inbox.db")
        if out:
            out["inbox_pending"] = 0     # no inbox file yet: nothing was ever proposed
        if inbox.exists():
            try:
                conn = _ro(inbox)
                try:
                    out["inbox_pending"] = conn.execute(
                        "SELECT COUNT(*) FROM candidates WHERE status='pending'").fetchone()[0]
                finally:
                    conn.close()
            except Exception:
                pass
    with _gauge_lock:
        _gauge_cache[store] = (now, out)
    return out


_STORE_GAUGES = {
    "notes": ("memd_index_notes", "Live (not superseded) notes in the store's index."),
    "pending_vectors": ("memd_index_pending_vectors", "Live notes without a current whole-note vector."),
    "pending_chunks": ("memd_index_pending_chunks", "Live notes without current chunk vectors."),
    "in_sync": ("memd_index_in_sync", "1 when the lexical index is at the clone's Git HEAD."),
    "head_age_seconds": ("memd_index_head_age_seconds",
                         "Seconds since the commit the lexical index is at was made."),
    "size_bytes": ("memd_index_size_bytes", "Size of the store's index database file."),
    "inbox_pending": ("memd_inbox_pending", "Review inbox candidates waiting for a decision."),
}


# --------------------------------------------------------------------------- exposition


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(pairs) -> str:
    pairs = [(k, v) for k, v in pairs]
    if not pairs:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in pairs) + "}"


def _num(value: float) -> str:
    if isinstance(value, float) and math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("memd")
    except Exception:
        from memd import __version__
        return __version__


def render(app_commit: str, stores: dict[str, dict]) -> str:
    """The exposition text: process info, recorded families, then per-store gauges."""
    lines: list[str] = []
    commit = app_commit if re.fullmatch(r"[0-9a-f]{7,64}|unknown", app_commit or "") else "unknown"
    lines += ["# HELP memd_build_info memd version and running application commit.",
              "# TYPE memd_build_info gauge",
              f"memd_build_info{_labels([('version', _version()), ('app_commit', commit)])} 1",
              "# HELP process_start_time_seconds Start time of the process since unix epoch in seconds.",
              "# TYPE process_start_time_seconds gauge",
              f"process_start_time_seconds {_num(round(START_TIME, 3))}"]
    with _lock:
        counters = dict(_counters)
        histograms = {k: list(v) for k, v in _histograms.items()}
    for name, (kind, help_text, spec) in _FAMILIES.items():
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
        names = list(spec)
        if kind == "counter":
            for key in sorted(k for k in counters if k[0] == name):
                lines.append(f"{name}{_labels(zip(names, key[1:]))} {_num(counters[key])}")
        else:
            for key in sorted(k for k in histograms if k[0] == name):
                h = histograms[key]
                base = list(zip(names, key[1:]))
                for bound, count in zip(BUCKETS, h):
                    lines.append(f"{name}_bucket{_labels(base + [('le', _num(bound))])} {count}")
                lines.append(f"{name}_bucket{_labels(base + [('le', '+Inf')])} {h[-1]}")
                lines.append(f"{name}_sum{_labels(base)} {_num(round(h[-2], 6))}")
                lines.append(f"{name}_count{_labels(base)} {h[-1]}")
    for field, (name, help_text) in _STORE_GAUGES.items():
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
        for store in sorted(stores)[:MAX_STORES]:
            value = stores[store].get(field)
            if value is not None:
                lines.append(f"{name}{_labels([('store', store)])} {_num(float(value))}")
    return "\n".join(lines) + "\n"


def public_enabled() -> bool:
    return (os.environ.get("MEMD_METRICS_PUBLIC") or "").strip().lower() in {"1", "true", "yes", "on"}
