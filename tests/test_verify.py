"""mem-verify: probe declarations, the allowlist fence, each probe kind, proposals,
save round-trip of `verify`, and recall's stale labelling of failed checks.

Hermetic: temp git clones, a fake resolver, respx for HTTP, and real listening
sockets on 127.0.0.1 reached through a loopback-only connector (the ambient
netguard refuses every IP connect, loopback included).
"""
import _socket
import datetime as dt
import json
import socket
import subprocess

import httpx
import pytest
import respx

import memd.save as save_mod
from memd import verify as vf
from memd.config import Config
from memd.normalize import NormalizeError, normalize_fact
from memd.render import render_result
from memd.staleness import failed_verification, is_stale, label
from memd.store import dump_note, list_notes, parse_text, read_note, Note

TODAY = dt.date(2026, 9, 28)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def loopback_connect(address, port, timeout):
    """Dial only 127.0.0.1, beneath the netguard (the base class is unpatched)."""
    assert address == "127.0.0.1", address
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((address, port))
    finally:
        sock.close()


@pytest.fixture
def listener():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    yield server.getsockname()[1]
    server.close()


@pytest.fixture
def closed_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


NAMES = {
    "gpuhost": ["127.0.0.1"],
    "vmhost": ["10.10.1.11"],
    "svc.example.com": ["10.10.1.10"],
    "public.example.com": ["203.0.113.7"],
    "mixed.example.com": ["10.10.1.12", "203.0.113.8"],
    "memd.example.com": ["203.0.113.9"],
}


def fake_resolver(name, timeout):
    if name not in NAMES:
        raise OSError(f"{name} does not resolve")
    return list(NAMES[name])


def prober(**kw):
    kw.setdefault("resolver", fake_resolver)
    kw.setdefault("connect", loopback_connect)
    kw.setdefault("timeout", 1.0)
    return vf.Prober(**kw)


# --------------------------------------------------------------------------- parsing


def test_parse_each_kind_and_canonical_form():
    probes = vf.parse_verify([
        {"tcp": "gpuhost:8077"},
        {"http": "https://memd.example.com/health", "status": 200, "json": "status=ok"},
        {"dns": "host.example.com -> 10.10.1.10"},
        {"command": "some-binary"},
        {"path": "/etc/foo"},
        "tcp: [::1]:22",
    ])
    assert [p.kind for p in probes] == ["tcp", "http", "dns", "command", "path", "tcp"]
    assert (probes[0].host, probes[0].port) == ("gpuhost", 8077)
    assert (probes[1].status, probes[1].json_key, probes[1].json_value) == (200, "status", "ok")
    assert (probes[2].host, probes[2].addresses) == ("host.example.com", ("10.10.1.10",))
    assert (probes[5].host, probes[5].port) == ("::1", 22)
    assert vf.canonical(["tcp: gpuhost:8077"]) == [{"tcp": "gpuhost:8077"}]
    assert vf.canonical([probes[1].to_frontmatter()]) == [
        {"http": "https://memd.example.com/health", "status": 200, "json": "status=ok"}]


def test_frontmatter_yaml_parses_to_probes():
    note = parse_text("---\ntitle: t\nverify:\n- tcp: gpuhost:8077\n"
                      "- dns: host.example.com -> 10.10.1.10\n"
                      "- http: https://memd.example.com/health\n  status: 204\n---\nbody\n", path="t.md")
    assert [p.describe() for p in vf.parse_verify(note.metadata["verify"])] == [
        "tcp: gpuhost:8077", "dns: host.example.com -> 10.10.1.10",
        "http: https://memd.example.com/health (status 204)"]


@pytest.mark.parametrize("item, message", [
    ({"exec": "rm -rf /"}, "unknown probe kind 'exec'; expected one of: command, dns, http, path, tcp"),
    ("shell: ls", "unknown probe kind 'shell'"),
    ({"tcp": "gpuhost:8077", "http": "http://x/"}, "exactly one kind"),
    ({"tcp": "gpuhost"}, "host:port"),
    ({"tcp": "gpuhost:0"}, "port between 1 and 65535"),
    ({"tcp": "bad_host!:80"}, "valid host"),
    ({"tcp": "gpuhost:80", "status": 200}, "tcp probe does not accept status"),
    ({"http": "ftp://x/"}, "http:// or https://"),
    ({"http": "https://user:pw@svc.example.com/"}, "credentials"),
    ({"http": "https://svc.example.com/", "method": "POST"}, "does not accept method"),
    ({"http": "https://svc.example.com/", "body": "x"}, "does not accept body"),
    ({"http": "https://svc.example.com/", "status": "abc"}, "status must be"),
    ({"http": "https://svc.example.com/", "json": "a b"}, "dotted key"),
    ({"dns": "host.example.com -> not-an-ip"}, "not an IP address"),
    ({"dns": "host.example.com ->"}, "expected an address"),
    ({"command": "ls -la"}, "bare command name"),
    ({"command": "/usr/bin/ls"}, "bare command name"),
    ({"command": "a;reboot"}, "bare command name"),
    ({"path": "etc/foo"}, "absolute path"),
    ({"path": 3}, "text target"),
    ("just words", "'kind: target'"),
    (["tcp", "x"], "must be a mapping"),
])
def test_parse_rejects_with_a_clear_message(item, message):
    with pytest.raises(vf.ProbeError) as err:
        vf.parse_verify([item])
    assert message in str(err.value)
    assert str(err.value).startswith("verify[1]: ")


def test_parse_caps_probes_per_note_and_rejects_non_lists():
    with pytest.raises(vf.ProbeError, match="at most 10"):
        vf.parse_verify([{"command": "ls"}] * 11)
    with pytest.raises(vf.ProbeError, match="list of probes"):
        vf.parse_verify(42)
    assert vf.parse_verify(None) == []


# --------------------------------------------------------------------------- allowlist


def test_default_allowlist_is_private_addresses_only():
    allow = vf.Allowlist.parse(None)
    for ok in ("127.0.0.1", "10.1.2.3", "172.16.5.5", "10.10.1.10", "::1", "fd00::1",
               "::ffff:10.10.1.10"):
        assert allow.address_allowed(ok), ok
    for bad in ("203.0.113.7", "169.254.169.254", "0.0.0.0", "172.32.0.1", "2001:db8::1", "fe80::1"):
        assert not allow.address_allowed(bad), bad
    assert not allow.name_allowed("svc.example.com")


def test_configured_allowlist_replaces_the_default():
    allow = vf.Allowlist.parse("203.0.113.0/24, *.example.com 10.10.1.10")
    assert allow.configured
    assert allow.address_allowed("203.0.113.7") and allow.address_allowed("10.10.1.10")
    assert not allow.address_allowed("127.0.0.1") and not allow.address_allowed("10.10.1.11")
    assert allow.name_allowed("example.com") and allow.name_allowed("a.b.example.com")
    assert not allow.name_allowed("badexample.com")
    with pytest.raises(ValueError, match="not an address, CIDR or domain"):
        vf.Allowlist.parse("bad_entry!")


def test_check_target_resolves_names_against_the_fence():
    allow = vf.Allowlist()
    assert vf.check_target("svc.example.com", allow, fake_resolver, 1) == ["10.10.1.10"]
    with pytest.raises(vf.NotAllowed, match="resolves to 203.0.113.7"):
        vf.check_target("public.example.com", allow, fake_resolver, 1)
    with pytest.raises(vf.NotAllowed, match="203.0.113.8"):   # one outside address is enough
        vf.check_target("mixed.example.com", allow, fake_resolver, 1)
    with pytest.raises(vf.NotAllowed):
        vf.check_target("203.0.113.7", allow, fake_resolver, 1)
    with pytest.raises(OSError):
        vf.check_target("nowhere.example.com", allow, fake_resolver, 1)
    domain = vf.Allowlist.parse("example.com")
    assert vf.check_target("public.example.com", domain, fake_resolver, 1) == ["203.0.113.7"]


# --------------------------------------------------------------------------- probe kinds


def _one(spec, **kw):
    return prober(**kw).run(vf.parse_probe(spec))


def test_tcp_pass_and_fail_on_local_sockets(listener, closed_port):
    ok = _one({"tcp": f"127.0.0.1:{listener}"})
    assert ok.outcome == "pass" and "connected" in ok.detail
    assert _one({"tcp": f"gpuhost:{listener}"}).outcome == "pass"   # name -> 127.0.0.1
    bad = _one({"tcp": f"127.0.0.1:{closed_port}"})
    assert bad.outcome == "fail" and f"127.0.0.1:{closed_port}" in bad.detail


def test_tcp_outside_the_fence_is_blocked_and_never_dialled():
    dialled = []
    record = lambda a, p, t: dialled.append(a)
    assert _one({"tcp": "203.0.113.7:443"}, connect=record).outcome == "blocked"
    res = _one({"tcp": "public.example.com:443"}, connect=record)
    assert res.outcome == "blocked" and "outside the verify allowlist" in res.detail
    assert dialled == []
    assert _one({"tcp": "nowhere.example.com:80"}, connect=record).outcome == "fail"


def test_tcp_placeholder_roles_map_through_host_names(monkeypatch, listener):
    monkeypatch.setenv("MEMD_HOST_NAMES", "vmhost=gpuhost")
    assert _one({"tcp": f"vmhost:{listener}"}).outcome == "pass"


def test_command_and_path_never_execute(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
    assert _one({"command": "git"}, which=lambda n: "/usr/bin/git").outcome == "pass"
    assert _one({"command": "nope"}, which=lambda n: None).detail == "not found on PATH"
    (tmp_path / "present").write_text("x")
    assert _one({"path": str(tmp_path / "present")}).outcome == "pass"
    assert _one({"path": str(tmp_path / "absent")}).outcome == "fail"
    assert ran == []


def test_dns_expected_address_and_fence():
    assert _one({"dns": "svc.example.com -> 10.10.1.10"}).outcome == "pass"
    moved = _one({"dns": "vmhost -> 10.10.1.10"})
    assert moved.outcome == "fail" and "resolves to 10.10.1.11" in moved.detail
    assert _one({"dns": "svc.example.com"}).outcome == "pass"
    assert _one({"dns": "public.example.com"}).outcome == "blocked"      # resolves outside
    assert _one({"dns": "public.example.com -> 203.0.113.7"}).outcome == "blocked"
    allowed = vf.Allowlist.parse("example.com")
    assert _one({"dns": "public.example.com -> 203.0.113.7"}, allow=allowed).outcome == "pass"
    assert _one({"dns": "gone.example.com -> 10.10.1.10"}).outcome == "fail"


@respx.mock
def test_http_get_status_and_json():
    # Requests go to the checked address with the name in Host (no second lookup).
    route = respx.get("http://10.10.1.10/health").respond(
        200, json={"status": "ok", "components": {"index": True}})
    assert _one({"http": "http://svc.example.com/health"}).outcome == "pass"
    assert _one({"http": "http://svc.example.com/health", "json": "components.index=true"}).outcome == "pass"
    assert _one({"http": "http://svc.example.com/health", "json": "status"}).outcome == "pass"
    wrong = _one({"http": "http://svc.example.com/health", "json": "status=degraded"})
    assert wrong.outcome == "fail" and "expected 'degraded'" in wrong.detail
    assert "'ok'" not in wrong.detail     # the actual value is never echoed into frontmatter
    missing = _one({"http": "http://svc.example.com/health", "json": "commit"})
    assert missing.outcome == "fail" and "no 'commit'" in missing.detail
    status = _one({"http": "http://svc.example.com/health", "status": 204})
    assert status.outcome == "fail" and "HTTP 200, expected 204" in status.detail
    for call in route.calls:
        assert call.request.method == "GET" and call.request.content == b""
        assert "authorization" not in call.request.headers
        assert call.request.headers["host"] == "svc.example.com"


@respx.mock
def test_http_server_error_and_connection_error_fail():
    respx.get("http://10.10.1.10/down").respond(503)
    respx.get("http://10.10.1.10/refused").mock(side_effect=httpx.ConnectError("refused"))
    assert "HTTP 503, expected 2xx" in _one({"http": "http://svc.example.com/down"}).detail
    refused = _one({"http": "http://svc.example.com/refused"})
    assert refused.outcome == "fail" and "ConnectError" in refused.detail


def test_http_outside_the_fence_is_blocked_before_any_request():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get("https://203.0.113.9/health").respond(200)
        assert _one({"http": "https://memd.example.com/health"}).outcome == "blocked"
        assert _one({"http": "https://203.0.113.9/health"}).outcome == "blocked"
        assert not route.called
        ok = _one({"http": "https://memd.example.com/health"},
                  allow=vf.Allowlist.parse("memd.example.com"))
        assert ok.outcome == "pass" and route.called
        sent = route.calls.last.request
        assert sent.headers["host"] == "memd.example.com"
        assert sent.extensions["sni_hostname"] == "memd.example.com"   # certificate checked for the name


def test_http_redirects_are_rechecked_at_every_hop():
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://10.10.1.10/a").respond(302, headers={"location": "/b"})
        mock.get("http://10.10.1.10/b").respond(301, headers={"location": "http://vmhost/c"})
        mock.get("http://10.10.1.11/c").respond(200, json={"ok": True})
        ok = _one({"http": "http://svc.example.com/a", "json": "ok=true"})
        assert ok.outcome == "pass" and "after redirect to http://vmhost/c" in ok.detail

        mock.get("http://10.10.1.10/out").respond(302, headers={"location": "https://public.example.com/x"})
        mock.get("http://10.10.1.10/ip").respond(307, headers={"location": "http://169.254.169.254/"})
        escape = mock.get("https://203.0.113.7/x").respond(200)
        metadata = mock.get("http://169.254.169.254/").respond(200)
        off = _one({"http": "http://svc.example.com/out"})
        assert off.outcome == "blocked" and "public.example.com resolves to 203.0.113.7" in off.detail
        assert _one({"http": "http://svc.example.com/ip"}).outcome == "blocked"
        assert not escape.called and not metadata.called

        mock.get("http://10.10.1.10/moved").respond(301, headers={"location": "/new"})
        assert _one({"http": "http://svc.example.com/moved", "status": 301}).outcome == "pass"

        mock.get("http://10.10.1.10/loop").respond(302, headers={"location": "/loop"})
        assert "more than 3 redirects" in _one({"http": "http://svc.example.com/loop"}).detail


def test_http_json_body_is_capped():
    with respx.mock:
        respx.get("http://10.10.1.10/big").respond(200, content=b"x" * (vf.MAX_BODY_BYTES + 10))
        big = _one({"http": "http://svc.example.com/big", "json": "a"})
    assert big.outcome == "fail" and "larger than" in big.detail


def test_http_connects_to_the_checked_address_not_a_second_lookup():
    """DNS rebinding: an allowed first answer must not let a later lookup reach elsewhere."""
    answers = iter([["10.10.1.10"], ["169.254.169.254"], ["127.0.0.1"]])
    lookups = []

    def rebinding(name, timeout):
        lookups.append(name)
        return next(answers)

    with respx.mock(assert_all_called=False) as mock:
        checked = mock.get("http://10.10.1.10/latest").respond(200, json={"k": "v"})
        metadata = mock.get("http://169.254.169.254/latest").respond(200, json={"k": "secret"})
        result = _one({"http": "http://rebind.example.com/latest", "json": "k=x"}, resolver=rebinding)
    assert lookups == ["rebind.example.com"]            # one lookup per hop, then the pinned address
    assert checked.called and not metadata.called
    assert result.outcome == "fail" and "secret" not in result.detail and "'v'" not in result.detail


# --------------------------------------------------------------------------- the batch job


def _md(title, *, verify=None, extra=""):
    fm = [f"title: {title}", "volatility: state", "observed_at: '2026-06-01'"]
    if verify is not None:
        fm.append("verify:")
        fm += [f"- {line}" for line in verify]
    if extra:
        fm.append(extra)
    return "---\n" + "\n".join(fm) + "\n---\n" + f"{title} body.\n"


@pytest.fixture
def clone(tmp_path, listener, closed_port):
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@memd")
    _git(repo, "config", "user.name", "memd-test")
    notes = {
        "service_port.md": _md("Service port", verify=[f"tcp: 127.0.0.1:{listener}", "command: git"]),
        "old_port.md": _md("Old port", verify=[f"tcp: 127.0.0.1:{closed_port}"]),
        "public_api.md": _md("Public API", verify=["http: https://public.example.com/health"]),
        "bad_probe.md": _md("Bad probe", verify=["exec: reboot"]),
        "plain.md": _md("Plain note"),
    }
    for name, text in notes.items():
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _cfg(clone):
    return Config(clone=clone, db=clone.parent / "memd.db", profile="amber")


def _prober():
    return prober(which=lambda name: f"/usr/bin/{name}")


def _by_slug(report):
    return {row["slug"]: row for row in report["notes"]}


def test_run_proposes_verified_and_failed_on_a_review_branch(clone):
    head, status = _git(clone, "rev-parse", "HEAD"), _git(clone, "status", "--porcelain")
    report = vf.run(_cfg(clone), today=TODAY, prober=_prober())
    rows = _by_slug(report)
    assert rows["service-port"]["status"] == "verified"
    assert rows["old-port"]["status"] == "failed"
    assert rows["public-api"]["status"] == "blocked"     # not attempted, not a failure
    assert rows["bad-probe"]["status"] == "invalid" and "unknown probe kind 'exec'" in rows["bad-probe"]["error"]
    assert "plain-note" not in rows
    assert report["proposed"] == 2 and report["branch"] == "memd/verify"
    # checkout, HEAD and the working tree are untouched
    assert _git(clone, "rev-parse", "HEAD") == head and _git(clone, "status", "--porcelain") == status
    assert _git(clone, "rev-parse", "memd/verify^") == head
    files = _git(clone, "diff", "--name-only", head, "memd/verify").split()
    assert sorted(files) == ["old_port.md", "service_port.md"]

    ok = parse_text(_git(clone, "show", "memd/verify:service_port.md"), path="service_port.md")
    assert ok.verified_at == "2026-09-28" and "verification" not in ok.metadata
    assert ok.metadata["verify"][1] == {"command": "git"}
    assert ok.body == "Service port body." and ok.saved_by == "mem-verify"
    bad = parse_text(_git(clone, "show", "memd/verify:old_port.md"), path="old_port.md")
    marker = bad.metadata["verification"]
    assert marker["status"] == "failed" and marker["checked_at"] == "2026-09-28"
    assert marker["since"] == "2026-09-28" and marker["failed"][0].startswith("tcp: 127.0.0.1:")
    assert bad.verified_at is None
    assert "Proposed-By: mem-verify" in _git(clone, "log", "-1", "--format=%B", "memd/verify")


def test_dry_run_probes_but_writes_nothing(clone):
    report = vf.run(_cfg(clone), dry_run=True, today=TODAY, prober=_prober())
    assert report["proposed"] == 2 and report["commit"] is None
    assert "verified_at: '2026-09-28'" in report["_texts"]["service_port.md"]
    assert subprocess.run(["git", "-C", str(clone), "rev-parse", "-q", "--verify", "memd/verify"],
                          capture_output=True).returncode != 0


def test_max_probes_defers_and_orders_least_recently_checked_first(clone):
    report = vf.run(_cfg(clone), dry_run=True, today=TODAY, prober=_prober(), max_probes=2)
    rows = _by_slug(report)
    # service-port needs 2 probes; slug order puts old-port (1 probe) first
    assert rows["old-port"]["status"] == "failed"
    assert rows["service-port"]["status"] == "deferred"
    assert rows["public-api"]["status"] == "blocked"


def test_rerun_after_merge_is_quiet_and_keeps_since(clone):
    vf.run(_cfg(clone), today=TODAY, prober=_prober())
    _git(clone, "merge", "-q", "--ff-only", "memd/verify")
    again = vf.run(_cfg(clone), today=TODAY, prober=_prober())
    rows = _by_slug(again)
    assert again["proposed"] == 0 and rows["service-port"]["unchanged"] and rows["old-port"]["unchanged"]
    later = vf.run(_cfg(clone), dry_run=True, today=TODAY + dt.timedelta(days=3), prober=_prober())
    text = later["_texts"]["old_port.md"]
    marker = parse_text(text, path="old_port.md").metadata["verification"]
    assert marker["since"] == "2026-09-28" and marker["checked_at"] == "2026-10-01"


def test_a_passing_recheck_clears_the_failed_marker(clone, closed_port, listener):
    vf.run(_cfg(clone), today=TODAY, prober=_prober())
    _git(clone, "merge", "-q", "--ff-only", "memd/verify")
    path = clone / "old_port.md"
    path.write_text(path.read_text().replace(f"127.0.0.1:{closed_port}", f"127.0.0.1:{listener}"))
    _git(clone, "commit", "-qam", "port fixed")
    report = vf.run(_cfg(clone), dry_run=True, today=TODAY + dt.timedelta(days=1), prober=_prober())
    fixed = parse_text(report["_texts"]["old_port.md"], path="old_port.md")
    assert "verification" not in fixed.metadata and fixed.verified_at == "2026-09-29"


def test_apply_commits_through_the_save_path(clone, monkeypatch):
    monkeypatch.setattr(save_mod, "_pull_rebase_push", lambda clone: None)
    head = _git(clone, "rev-parse", "HEAD")
    report = vf.run(_cfg(clone), apply=True, today=TODAY, prober=_prober())
    assert report["applied"] and report["commit"] != head
    assert _git(clone, "status", "--porcelain") == ""
    assert "Saved-By: mem-verify" in _git(clone, "log", "-1", "--format=%B")
    assert read_note(clone, "service-port").verified_at == "2026-09-28"
    assert read_note(clone, "old-port").metadata["verification"]["status"] == "failed"
    assert subprocess.run(["git", "-C", str(clone), "rev-parse", "-q", "--verify", "memd/verify"],
                          capture_output=True).returncode != 0


def test_apply_skips_a_note_changed_since_it_was_probed(clone, monkeypatch):
    monkeypatch.setattr(save_mod, "_pull_rebase_push", lambda clone: None)
    before = read_note(clone, "service-port")
    after = Note(**{**before.__dict__, "verified_at": "2026-09-28"})
    (clone / "service_port.md").write_text((clone / "service_port.md").read_text() + "edit\n")
    _git(clone, "commit", "-qam", "concurrent edit")
    out = vf.apply_changes(_cfg(clone), [(before, after)], message="memd: verify")
    assert out["applied"] == [] and out["skipped"][0]["slug"] == "service-port"


def test_main_json_dry_run(clone, monkeypatch, capsys):
    monkeypatch.setenv("MEMD_CLONE", str(clone))
    monkeypatch.setenv("MEMD_DB", str(clone.parent / "memd.db"))
    monkeypatch.setattr(vf, "system_resolver", fake_resolver)
    monkeypatch.setattr(vf.Prober, "__init__", _patched_init(vf.Prober.__init__))
    assert vf.main(["--dry-run", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] and out["proposed"] == 2 and "_texts" not in out
    monkeypatch.setenv("MEMD_VERIFY_ALLOW", "bad_entry!")
    assert vf.main(["--dry-run"]) == 2


def _patched_init(original):
    def init(self, *args, **kw):
        kw.setdefault("resolver", fake_resolver)
        kw.setdefault("connect", loopback_connect)
        original(self, *args, **kw)
    return init


# --------------------------------------------------------------------------- save


@pytest.fixture
def store(tmp_path, monkeypatch):
    repo = tmp_path / "store"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@memd")
    _git(repo, "config", "user.name", "test")
    _git(repo, "commit", "--allow-empty", "-qm", "seed")
    monkeypatch.setattr(save_mod, "_pull_rebase_push", lambda clone: None)
    return Config(clone=repo, db=tmp_path / "index.db", profile="amber")


def test_normalize_validates_verify():
    out = normalize_fact({"title": "t", "body": "b", "verify": ["tcp: gpuhost:8077"]})
    assert out["verify"] == [{"tcp": "gpuhost:8077"}]
    assert normalize_fact({"title": "t", "body": "b", "verify": None})["verify"] == []
    with pytest.raises(NormalizeError, match="verify: verify\\[1\\]: unknown probe kind 'exec'"):
        normalize_fact({"title": "t", "body": "b", "verify": [{"exec": "reboot"}]})


def test_save_round_trips_verify_and_resets_the_marker(store):
    probes = [{"tcp": "gpuhost:8077"}, {"http": "https://memd.example.com/health", "json": "status"}]
    res = save_mod.save({"title": "Memd port", "body": "memd listens on 8077", "verify": probes},
                        cfg=store)
    note = read_note(store.clone, res.slug)
    assert note.metadata["verify"] == probes
    assert "verify:\n- tcp: gpuhost:8077" in (store.clone / note.path).read_text()

    # an update without `verify` keeps it (and any marker mem-verify wrote)
    path = store.clone / note.path
    text = path.read_text().replace("verify:", "verification:\n  status: failed\n"
                                    "  checked_at: '2026-09-27'\nverify:", 1)
    path.write_text(text)
    _git(store.clone, "commit", "-qam", "marker")
    save_mod.save({"slug": res.slug, "title": "Memd port", "body": "memd listens on 8077 still"}, cfg=store)
    note = read_note(store.clone, res.slug)
    assert note.metadata["verify"] == probes and note.metadata["verification"]["status"] == "failed"

    # new probes invalidate the old failure; an empty list removes the declaration
    save_mod.save({"slug": res.slug, "title": "Memd port", "body": "moved", "verify": ["tcp: gpuhost:8078"]},
                  cfg=store)
    note = read_note(store.clone, res.slug)
    assert note.metadata["verify"] == [{"tcp": "gpuhost:8078"}] and "verification" not in note.metadata
    save_mod.save({"slug": res.slug, "title": "Memd port", "body": "moved", "verify": []}, cfg=store)
    assert "verify" not in read_note(store.clone, res.slug).metadata


def test_mcp_save_schema_advertises_verify():
    import asyncio
    from memd import mcp
    save_tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "save")
    assert "verify" in save_tool.input_schema["properties"]


# --------------------------------------------------------------------------- recall labelling


FAILED = {"status": "failed", "checked_at": "2026-09-27", "since": "2026-09-20",
          "failed": ["tcp: gpuhost:8077: connection refused"]}


def test_recent_failed_verification_labels_the_note_stale():
    note = {"slug": "memd-port", "observed_at": "2026-09-26", "volatility": "durable",
            "metadata": {"verification": FAILED}}
    assert failed_verification(note, TODAY)["probe"] == "tcp: gpuhost:8077: connection refused"
    assert is_stale(note, TODAY)
    text = label(note, TODAY)
    assert "verification failed 2026-09-27: tcp: gpuhost:8077: connection refused" in text
    assert "verify live state" in text and "as of 2026-09-26" in text
    # the top-level form (unknown frontmatter flattened) works too, even undated
    assert "verification failed" in label({"slug": "x", "verification": FAILED}, TODAY)


def test_old_or_superseded_failures_do_not_label():
    old = {"slug": "x", "metadata": {"verification": {**FAILED, "checked_at": "2026-07-01"}}}
    assert failed_verification(old, TODAY) is None and not is_stale(old, TODAY)
    rechecked = {"slug": "x", "verified_at": "2026-09-28", "metadata": {"verification": FAILED}}
    assert failed_verification(rechecked, TODAY) is None
    assert label(rechecked, TODAY) == "  (as of 2026-09-28)"
    assert failed_verification({"slug": "x", "metadata": {"verification": "failed"}}, TODAY) is None


def test_rendered_recall_surfaces_the_failed_check():
    checked = (dt.date.today() - dt.timedelta(days=1)).isoformat()   # render dates against today
    note = Note(title="Memd port", slug="memd-port", path="memd_port.md", body="memd listens on 8077",
                metadata={"verify": [{"tcp": "gpuhost:8077"}],
                          "verification": {**FAILED, "checked_at": checked}})
    hit = note.to_dict() | {"matched": True}
    out = render_result([hit, {**hit, "slug": "other"}], query="memd port")
    assert f"### memd-port  (verification failed {checked}: tcp: gpuhost:8077" in out["text"]
    assert "2 of these notes" in out["text"]


def test_dump_note_keeps_verify_through_maintenance_round_trips():
    note = parse_text(_md("Svc", verify=["tcp: gpuhost:8077"]), path="svc.md")
    again = parse_text(dump_note(note), path="svc.md")
    assert again.metadata["verify"] == [{"tcp": "gpuhost:8077"}]
