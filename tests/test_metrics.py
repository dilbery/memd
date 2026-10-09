"""GET /metrics: Prometheus exposition, its auth, and that it never leaks content.

Hermetic: a temp Git store, model URLs on a closed loopback port (so every embed
and rerank call fails fast and is counted as a failure), and a token registry in
tmp_path for the monitoring-token cases.
"""
import math
import re
import subprocess
import uuid

import pytest
from fastapi.testclient import TestClient

import memd.server as server_mod
from memd import actor, metrics

TOKEN = "m" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MARKER = "zqxmarker7731"

_SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})? (\S+)$')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"(,|$)')


def parse(text: str) -> dict:
    """A small strict parser of the text format: {family: {type, samples: [(name, labels, value)]}}."""
    assert text.endswith("\n")
    families: dict[str, dict] = {}
    current = None
    seen = set()
    for line in text.splitlines():
        assert line, "blank line in exposition"
        if line.startswith("# HELP "):
            name = line.split(" ", 3)[2]
            assert name not in families, f"family {name} declared twice"
            families[name] = {"type": None, "samples": []}
            current = name
            continue
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ")
            assert name == current and kind in {"counter", "gauge", "histogram"}
            families[name]["type"] = kind
            continue
        assert not line.startswith("#"), line
        m = _SAMPLE.match(line)
        assert m, f"malformed sample: {line!r}"
        name, _, raw_labels, value = m.groups()
        labels = {}
        if raw_labels:
            pos = 0
            while pos < len(raw_labels):
                lm = _LABEL.match(raw_labels, pos)
                assert lm, f"malformed labels: {raw_labels!r}"
                labels[lm.group(1)] = lm.group(2)
                pos = lm.end()
        number = math.inf if value == "+Inf" else float(value)
        family = families[current]
        base = name
        if family["type"] == "histogram":
            base = re.sub(r"_(bucket|sum|count)$", "", name)
        assert base == current, f"sample {name} outside its family {current}"
        if family["type"] == "counter":
            assert name.endswith("_total") and number >= 0
        key = (name, tuple(sorted(labels.items())))
        assert key not in seen, f"duplicate series {key}"
        seen.add(key)
        family["samples"].append((name, labels, number))
    return families


def value(families, name, **labels) -> float:
    family = re.sub(r"_(bucket|sum|count)$", "", name) if name not in families else name
    for n, lab, v in families.get(family, {"samples": []})["samples"]:
        if n == name and all(lab.get(k) == w for k, w in labels.items()):
            return v
    return 0.0


def check_histograms(families) -> None:
    for name, family in families.items():
        if family["type"] != "histogram":
            continue
        series: dict[tuple, dict] = {}
        for n, labels, v in family["samples"]:
            base = tuple(sorted((k, w) for k, w in labels.items() if k != "le"))
            entry = series.setdefault(base, {"buckets": [], "sum": None, "count": None})
            if n.endswith("_bucket"):
                le = labels["le"]
                entry["buckets"].append((math.inf if le == "+Inf" else float(le), v))
            elif n.endswith("_sum"):
                entry["sum"] = v
            else:
                entry["count"] = v
        for base, entry in series.items():
            bounds = [b for b, _ in entry["buckets"]]
            counts = [c for _, c in entry["buckets"]]
            assert bounds == sorted(bounds) and bounds[-1] == math.inf, (name, base)
            assert counts == sorted(counts), f"{name}{base} buckets are not cumulative"
            assert counts[-1] == entry["count"], f"{name}{base} +Inf bucket != count"
            assert entry["sum"] is not None and entry["sum"] >= 0


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def store(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "config", "user.name", "t")
    _git(clone, "config", "user.email", "t@t")
    (clone / f"{MARKER}-seed.md").write_text(
        f"---\ntitle: Seed {MARKER}\nslug: {MARKER}-seed\nprofile: amber\nhost: any\n"
        f"importance: 3\ntags: [ops]\n---\nThe {MARKER} gateway runs on port 8443.\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "seed")
    db = tmp_path / "m.db"
    for key, val in {"MEMD_AMBER_CLONE": str(clone), "MEMD_AMBER_DB": str(db), "MEMD_CLONE": str(clone),
                     "MEMD_DB": str(db), "MEMD_LOCAL_HOST": "any", "MEMD_PROFILE": "amber",
                     "MEMD_TOKEN": TOKEN, "MEMD_EMBED_URL": "http://127.0.0.1:9",
                     "MEMD_RERANK_URL": "http://127.0.0.1:9", "MEMD_STARTUP_REFRESH": "0",
                     "MEMD_BACKGROUND_REFRESH": "0", "MEMD_USAGE_LOG": "on",
                     "MEMD_COBALT_CLONE": str(tmp_path / "cobalt"),
                     "MEMD_COBALT_DB": str(tmp_path / "cobalt.db")}.items():
        monkeypatch.setenv(key, val)
    monkeypatch.delenv("MEMD_METRICS_PUBLIC", raising=False)
    actor.set_actor("")
    from memd.refresh import ensure_lexical
    from memd.config import Config
    ensure_lexical(Config.from_env())
    metrics.reset()
    import httpx
    import respx
    # Both model services are down: every call fails fast and is counted.
    with respx.mock(assert_all_called=False) as router:
        router.route(host="127.0.0.1").mock(side_effect=httpx.ConnectError("refused"))
        yield clone


def _scrape(client, headers=AUTH):
    response = client.get("/metrics", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    return response.text


def test_exposition_is_valid_and_histograms_are_consistent(store):
    with TestClient(server_mod.create_token_app()) as client:
        client.post("/recall", headers=AUTH, json={"query": "gateway port"})
        text = _scrape(client)
    families = parse(text)
    check_histograms(families)
    for name in ("memd_build_info", "process_start_time_seconds", "memd_http_requests_total",
                 "memd_recall_arm_duration_seconds", "memd_backend_errors_total", "memd_index_notes",
                 "memd_index_pending_vectors", "memd_index_pending_chunks", "memd_index_head_age_seconds",
                 "memd_inbox_pending", "memd_saves_total"):
        assert name in families, name
    (_, info, one), = families["memd_build_info"]["samples"]
    assert one == 1 and info["version"] and info["app_commit"]
    assert value(families, "memd_index_notes", store="amber") == 1
    assert value(families, "memd_index_pending_vectors", store="amber") == 1
    assert value(families, "memd_index_in_sync", store="amber") == 1
    assert value(families, "memd_index_head_age_seconds", store="amber") >= 0


def test_counters_move_after_recall_save_and_read(store):
    with TestClient(server_mod.create_token_app()) as client:
        before = parse(_scrape(client))
        recalled = client.post("/recall", headers=AUTH, json={"query": "gateway port 8443"})
        assert recalled.status_code == 200
        saved = client.post("/save", headers=AUTH, json={"title": "Proxy cache size",
                                                          "body": "The proxy cache is 2 GB."})
        assert saved.status_code == 200, saved.text
        read = client.post("/read", headers=AUTH, json={"slug": saved.json()["slug"]})
        assert read.status_code == 200
        client.post("/save", headers=AUTH, json={})             # 400
        after = parse(_scrape(client))
    check_histograms(after)

    def delta(name, **labels):
        return value(after, name, **labels) - value(before, name, **labels)
    assert delta("memd_http_requests_total", route="recall", status="2xx") == 1
    assert delta("memd_http_requests_total", route="save", status="2xx") == 1
    assert delta("memd_http_requests_total", route="save", status="4xx") == 1
    assert delta("memd_http_requests_total", route="read", status="2xx") == 1
    assert delta("memd_http_request_duration_seconds_count", route="recall") == 1
    assert delta("memd_recall_arm_duration_seconds_count", arm="total") == 1
    assert delta("memd_recall_arm_duration_seconds_count", arm="bm25") == 1
    # The embedding service is down: counted as a backend failure and a recall fallback.
    assert delta("memd_backend_calls_total", backend="embed", outcome="error") >= 1
    assert delta("memd_backend_errors_total", backend="embed", kind="connect") >= 1
    assert delta("memd_recall_fallbacks_total", reason="embed_failed") \
        + delta("memd_recall_fallbacks_total", reason="embed_timeout") == 1
    assert delta("memd_recall_order_total", order="rerank") + delta("memd_recall_order_total", order="bm25") == 1
    assert delta("memd_saves_total", outcome="saved") == 1
    assert delta("memd_save_receipts_total", stage="lexical_indexed") == 1
    assert delta("memd_usage_log_events_total", kind="recall", result="logged") == 1
    assert delta("memd_usage_log_events_total", kind="read", result="logged") == 1
    assert value(after, "memd_index_notes", store="amber") >= 1


def test_mcp_tool_calls_are_counted(store):
    with TestClient(server_mod.create_token_app()) as client:
        response = client.post("/mcp/", headers={**AUTH, "Content-Type": "application/json",
                                                  "Accept": "application/json, text/event-stream"},
                               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": "recall", "arguments": {"query": "gateway"}}})
        assert response.status_code == 200
        families = parse(_scrape(client))
    assert value(families, "memd_mcp_tool_calls_total", tool="recall", outcome="ok") == 1
    assert value(families, "memd_http_requests_total", route="mcp", status="2xx") == 1


def test_no_content_query_slug_or_token_leaks(store):
    with TestClient(server_mod.create_token_app()) as client:
        client.post("/recall", headers=AUTH, json={"query": f"{MARKER} gateway"})
        saved = client.post("/save", headers=AUTH, json={"title": f"Second {MARKER}",
                                                          "body": f"{MARKER} note body text"})
        client.post("/read", headers=AUTH, json={"slug": saved.json()["slug"]})
        client.post("/read", headers=AUTH, json={"slug": f"{MARKER}-missing"})
        client.get(f"/{MARKER}/unknown", headers=AUTH)
        text = _scrape(client)
    lowered = text.lower()
    assert MARKER not in lowered
    assert TOKEN not in text and "legacy" not in lowered     # neither the token nor its label
    assert "8443" not in text and "gateway" not in lowered


def test_unknown_routes_and_label_values_collapse(store):
    with TestClient(server_mod.create_token_app()) as client:
        for i in range(30):
            client.get(f"/no-such-route-{i}/x")
        metrics.inc("memd_mcp_tool_calls_total", tool="made-up-tool", outcome="weird")
        metrics.inc("memd_backend_errors_total", backend="elsewhere", kind="novel")
        families = parse(_scrape(client))
    routes = {lab["route"] for _, lab, _ in families["memd_http_requests_total"]["samples"]}
    assert routes <= set(metrics.ROUTES)
    assert value(families, "memd_http_requests_total", route="other", status="4xx") == 30
    assert value(families, "memd_mcp_tool_calls_total", tool="other", outcome="other") == 1
    assert value(families, "memd_backend_errors_total", backend="other", kind="other") == 1
    for family in families.values():
        for _, labels, _ in family["samples"]:
            assert "no-such-route" not in "".join(labels.values())


def test_route_for_is_a_fixed_vocabulary():
    assert metrics.route_for("/recall", "POST") == "recall"
    assert metrics.route_for("/mcp/", "POST") == "mcp"
    assert metrics.route_for("/", "POST") == "mcp"
    assert metrics.route_for("/", "GET", "text/event-stream") == "mcp"
    assert metrics.route_for("/", "GET", "text/html") == "ui"
    assert metrics.route_for("/entities/host/node-a", "GET") == "entities"
    assert metrics.route_for("/inbox/0123456789abcdef/approve", "POST") == "inbox"
    assert metrics.route_for("/../../etc/passwd", "GET") == "other"


def test_histogram_buckets_are_cumulative():
    metrics.reset()
    for seconds in (0.001, 0.02, 0.02, 0.3, 7.0, 99.0):
        metrics.observe("memd_backend_call_duration_seconds", seconds, backend="chat")
    families = parse(metrics.render("unknown", {}))
    check_histograms(families)
    name = "memd_backend_call_duration_seconds_bucket"
    assert value(families, name, backend="chat", le="0.005") == 1
    assert value(families, name, backend="chat", le="0.025") == 3
    assert value(families, name, backend="chat", le="10") == 5
    assert value(families, name, backend="chat", le="+Inf") == 6
    assert value(families, "memd_backend_call_duration_seconds_count", backend="chat") == 6
    assert value(families, "memd_backend_call_duration_seconds_sum", backend="chat") == pytest.approx(106.341)
    metrics.reset()


def test_backend_error_kinds():
    import httpx
    from memd.embed import EmbedBackendError
    from memd.llm import LLMError
    request = httpx.Request("POST", "http://127.0.0.1:9/")
    assert metrics.error_kind(httpx.ReadTimeout("t", request=request)) == "timeout"
    assert metrics.error_kind(httpx.ConnectError("c", request=request)) == "connect"
    status = httpx.HTTPStatusError("s", request=request, response=httpx.Response(503, request=request))
    assert metrics.error_kind(status) == "http_5xx"
    assert metrics.error_kind(EmbedBackendError("embeddings backend 4xx 400: too long")) == "http_4xx"
    assert metrics.error_kind(LLMError("chat backend HTTP 502: bad gateway")) == "http_5xx"
    assert metrics.error_kind(LLMError("chat backend exceeded the 5s deadline")) == "timeout"
    assert metrics.error_kind(LLMError("chat backend answered with invalid JSON")) == "bad_response"


# --------------------------------------------------------------------------- auth


def test_metrics_without_a_token_is_refused(store):
    with TestClient(server_mod.create_token_app()) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"Authorization": "Bearer " + "n" * 40}).status_code == 401


def test_public_mode_is_loopback_only(store, monkeypatch):
    app = server_mod.create_token_app()
    with TestClient(app, client=("127.0.0.1", 50000)) as local:
        assert local.get("/metrics").status_code == 401          # off by default
        monkeypatch.setenv("MEMD_METRICS_PUBLIC", "1")
        text = _scrape(local, headers={})
        assert 'memd_index_notes{store="amber"}' in text
        assert local.get("/metrics", headers={"X-Forwarded-For": "192.0.2.10"}).status_code == 401
        assert local.get("/metrics", headers={"Forwarded": "for=192.0.2.10"}).status_code == 401
    with TestClient(server_mod.create_token_app(), client=("192.0.2.10", 50000)) as remote:
        assert remote.get("/metrics").status_code == 401
        assert remote.get("/metrics", headers=AUTH).status_code == 200


def test_static_token_sees_only_its_own_store(store):
    with TestClient(server_mod.create_token_app()) as client:
        text = _scrape(client)
    assert 'store="amber"' in text
    assert 'store="cobalt"' not in text


@pytest.fixture
def registry(store, tmp_path, monkeypatch):
    from memd.registry import Registry, principal
    monkeypatch.setenv("MEMD_CONTROL_DB", str(tmp_path / "control" / "control.db"))
    monkeypatch.delenv("MEMD_TOKEN", raising=False)
    reg = Registry()
    reg.initialize()
    principal.set(None)

    def issue(operations, stores=("amber",)):
        return reg.issue(actor="a", operation_id=str(uuid.uuid4()), label="grafana", owner="Ops",
                         purpose="monitoring", stores=list(stores), operations=operations,
                         days=30)["secret"]
    return issue


def test_a_stats_only_token_can_scrape(registry):
    secret = registry(["stats"])
    with TestClient(server_mod.create_token_app()) as client:
        text = _scrape(client, headers={"Authorization": f"Bearer {secret}"})
    assert 'memd_index_notes{store="amber"} 1' in text
    assert 'store="cobalt"' not in text and "grafana" not in text


def test_a_token_without_stats_is_refused(registry):
    secret = registry(["read", "recall"])
    with TestClient(server_mod.create_token_app()) as client:
        response = client.get("/metrics", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 403


def test_a_multi_store_stats_token_sees_each_granted_store(registry):
    secret = registry(["stats"], stores=("amber", "cobalt"))
    with TestClient(server_mod.create_token_app()) as client:
        families = parse(_scrape(client, headers={"Authorization": f"Bearer {secret}"}))
    stores = {lab["store"] for _, lab, _ in families["memd_index_notes"]["samples"]}
    assert stores == {"amber"}          # cobalt has no index yet, so it has no gauges
    assert value(families, "memd_index_notes", store="amber") == 1


def test_inbox_pending_gauge(store):
    with TestClient(server_mod.create_token_app()) as client:
        proposed = client.post("/propose", headers=AUTH, json={"title": "Queued fact",
                                                                "body": "Waiting for review."})
        assert proposed.status_code == 200, proposed.text
        families = parse(_scrape(client))
    assert value(families, "memd_inbox_pending", store="amber") == 1
    assert value(families, "memd_http_requests_total", route="propose", status="2xx") == 1
