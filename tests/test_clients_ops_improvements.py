"""Hermetic deployment/client contracts: no Docker, remote HTTP or user config."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_pi_personal_token_does_not_send_legacy_store_override(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for Pi integration')
    script = tmp_path/'personal.mjs'
    script.write_text('''
const {default: extension} = await import(process.env.PI_EXTENSION);
let recall, remember; const requests=[];
extension({on(name, fn) {if(name==='before_agent_start')recall=fn;},registerTool(t){remember=t;}});
globalThis.fetch=async (url, request)=>{requests.push({url,headers:request.headers,body:JSON.parse(request.body)});return {ok:true,json:async()=>({context:'',saved:true,slug:'test',revision:'abc'})};};
await recall({prompt:'my personal context'});
await remember.execute('id',{title:'Personal preference',body:'A durable preference'});
if(requests.length!==2 || requests.some(r=>'profile' in r.body || r.headers.Authorization!=='Bearer synthetic-personal-token'))throw new Error('Personal token configuration did not reach both operations');
''')
    env={**os.environ,'MEMD_TOKEN':'synthetic-personal-token','PI_EXTENSION':(ROOT/'clients/pi-memd.ts').as_uri()}
    env.pop('MEMD_PROFILE',None)
    result=subprocess.run([node,'--experimental-strip-types',str(script)],env=env,text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr


def load_script(name, relative):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / relative))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def bridge():
    return load_script("bridge_ops_review", "clients/memd-mcp-bridge")


@pytest.mark.parametrize("kind", ["malformed", "rpc-error", "missing-save", "valid-long", "sse"])
def test_selftest_checks_complete_discovery(bridge, monkeypatch, capsys, kind):
    reply = {"jsonrpc": "2.0", "id": "selftest", "result": {"tools": [
        {"name": "recall", "description": "x" * 1000}, {"name": "save"}, {"name": "read"},
    ]}}
    if kind == "rpc-error":
        reply.pop("result")
        reply["error"] = {"code": -32603, "message": "failed"}
    elif kind == "missing-save":
        reply["result"]["tools"] = [{"name": "recall"}]
    body = json.dumps(reply).encode()
    if kind == "malformed":
        body = b"<html>bad proxy</html>"
    elif kind == "sse":
        body = b"event: message\ndata: " + body + b"\n\n"
    monkeypatch.setattr(bridge, "post_raw", lambda *a: (200, body))
    assert bridge.selftest("http://invalid", "test-token", "test") == (
        0 if kind in {"valid-long", "sse"} else 1
    )
    if kind in {"valid-long", "sse"}:
        assert "tools=recall,save,read" in capsys.readouterr().out


@pytest.mark.parametrize("tool, expected", [("save", 1), ("unknown", 1), ("read", 3), ("recall", 3)])
def test_only_read_operations_retry_ambiguous_network_errors(bridge, monkeypatch, tool, expected):
    calls = []
    monkeypatch.setattr(bridge.time, "sleep", lambda _: None)
    def fail(*args):
        calls.append(args)
        return -1, b""
    monkeypatch.setattr(bridge, "post_once", fail)
    request = json.dumps({"id": 1, "method": "tools/call", "params": {"name": tool}})
    assert bridge.post_raw("http://invalid", request, "test", 0.1)[0] == -1
    assert len(calls) == expected


@pytest.mark.parametrize("body", [b"not-json", b'{"jsonrpc":"2.0","id":99,"result":{}}', b""])
def test_bad_upstream_replies_become_matching_rpc_errors(bridge, monkeypatch, capsys, body):
    monkeypatch.setattr(bridge, "post_raw", lambda *a: (200, body))
    bridge.handle_line('{"jsonrpc":"2.0","id":7,"method":"tools/list"}', "http://invalid", "T", 1)
    reply = json.loads(capsys.readouterr().out)
    assert reply["id"] == 7 and "error" in reply


def test_bridge_converts_sse_to_stdio_json(bridge, monkeypatch, capsys):
    body = b'event: message\ndata: {"jsonrpc":"2.0","id":7,"result":{}}\n\n'
    monkeypatch.setattr(bridge, "post_raw", lambda *a: (200, body))
    bridge.handle_line('{"jsonrpc":"2.0","id":7,"method":"tools/list"}', "http://invalid", "T", 1)
    assert json.loads(capsys.readouterr().out) == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_bridge_reads_onboarding_export_file(bridge, tmp_path, monkeypatch):
    env_file = tmp_path / "client.env"
    env_file.write_text("export MEMD_TOKEN='example-token'\n")
    monkeypatch.setenv("MEMD_ENV_FILE", str(env_file))
    assert bridge.token_from_env_file() == "example-token"


@pytest.mark.parametrize("in_sync, expected", [(True, "PASS"), (False, "FAIL"), (None, "FAIL")])
def test_doctor_rejects_stale_lexical_index(monkeypatch, in_sync, expected):
    doctor = load_script("doctor_ops_review", "maint/memd-doctor")
    body = {"ok": True, "checks": {"git": {"ok": True, "in_sync": in_sync}, "index": {"ok": True, "notes": 1}}}
    monkeypatch.setattr(doctor, "http_json", lambda *a, **k: (200, body, ""))
    status, detail = doctor.server_health("http://invalid")
    assert status == expected
    if not in_sync:
        assert "git.in_sync" in detail


def executable(path, code):
    path.write_text("#!" + sys.executable + "\n" + code)
    path.chmod(0o700)


@pytest.mark.parametrize("health_kind, success", [("healthy", True), ("degraded", True), ("wrong-commit", False), ("bad-json", False), ("stale", False), ("false-ok", False)])
def test_deploy_uses_running_rollback_and_checks_commit(tmp_path, health_kind, success):
    stack = tmp_path / "stack"
    stack.mkdir()
    shutil.copy(ROOT / "deploy/apphost/deploy.sh", stack / "deploy.sh")
    (stack / "compose.yml").write_text("services: {}\n")
    bins = tmp_path / "bin"
    bins.mkdir()
    log = tmp_path / "calls.jsonl"
    common = "import json, os, sys\nfrom pathlib import Path\nargs=sys.argv[1:]\n"
    executable(bins / "docker", common + '''
with open(os.environ["PROBE_LOG"], "a") as f:
    f.write(json.dumps({"args": args, "compose_file": os.environ.get("COMPOSE_FILE")}) + "\\n")
if args[:3] == ["compose", "ps", "-q"]:
    print("container-" + args[3])
elif args[0] == "inspect":
    print("sha256:previous-" + args[-1])
''')
    executable(bins / "git", common + 'if "rev-parse" in args: print("expected-sha")\n')
    executable(bins / "curl", common + '''
kind=os.environ["HEALTH_KIND"]
if kind == "bad-json":
    print("not json")
else:
    print(json.dumps({"ok": kind != "false-ok", "app_commit": "other-sha" if kind == "wrong-commit" else "expected-sha", "status": "degraded" if kind == "degraded" else "ok", "checks": {"index": {"ok": True}, "git": {"ok": True, "in_sync": kind != "stale"}, "rerank": {"ok": kind != "degraded"}}}))
''')
    executable(bins / "sleep", "pass\n")
    env = dict(os.environ, PATH=str(bins) + os.pathsep + os.environ["PATH"], PROBE_LOG=str(log), HEALTH_KIND=health_kind)
    env.pop("COMPOSE_FILE", None)
    run = subprocess.run(["/bin/sh", str(stack / "deploy.sh")], env=env, capture_output=True, text=True, timeout=30)
    assert (run.returncode == 0) is success, run.stdout + run.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    commands = [call["args"] for call in calls]
    assert ["tag", "sha256:previous-container-memd", "memd:rollback"] in commands
    assert commands.index(["tag", "sha256:previous-container-memd", "memd:rollback"]) < next(i for i, args in enumerate(commands) if args[:2] == ["compose", "build"])
    rollbacks = [call for call in calls if call["compose_file"]]
    assert bool(rollbacks) is not success
    if not success:
        assert "Deployed and verified" not in run.stdout


@pytest.mark.parametrize("failure", ["probe", "download", "codex", "none"])
def test_onboarding_fails_truthfully_and_preserves_codex_config(tmp_path, failure):
    home = tmp_path / "home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    config = codex_home / "config.toml"
    original = 'model = "existing-model"\n[mcp_servers.other]\ncommand = "keep-me"\n'
    config.write_text(original)
    bins = tmp_path / "bin"
    bins.mkdir()
    common = "import json, os, sys\nfrom pathlib import Path\nargs=sys.argv[1:]\n"
    executable(bins / "curl", common + '''
mode = os.environ["FAILURE"]
if "-d" in args:
    if mode == "probe": print('{"jsonrpc":"2.0","id":1,"error":{"code":-1}}\\n200')
    else: print('{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"recall"},{"name":"save"}]}}\\n200')
elif any("/clients/" in a for a in args):
    if mode == "download": sys.exit(22)
    Path(args[args.index("-o") + 1]).write_text("#!/bin/sh\\nexit 0\\n")
''')
    executable(bins / "codex", common + '''
config = Path(os.environ["HOME"]) / ".codex/config.toml"
if os.environ["FAILURE"] == "codex":
    config.write_text("damaged by failing CLI")
    sys.exit(1)
assert args[:3] == ["mcp", "add", "memd"]
assert not any(a.startswith("MEMD_TOKEN=") for a in args)
with config.open("a") as out: out.write("\\n[mcp_servers.memd]\\ncommand = 'bridge'\\n")
''')
    env = dict(os.environ, HOME=str(home), PATH=str(bins) + os.pathsep + os.environ["PATH"], FAILURE=failure)
    env.pop("CODEX_HOME", None)
    run = subprocess.run(["bash", str(ROOT / "clients/onboard.sh"), "mem_amber_test-token", "https://invalid.test"], env=env, capture_output=True, text=True, timeout=20)
    assert (run.returncode == 0) is (failure == "none"), run.stdout + run.stderr
    if failure != "none":
        assert "Onboarding complete." not in run.stdout
        assert config.read_text() == original
    else:
        assert config.read_text().startswith(original)
        assert (home / ".config/memd/client.env").stat().st_mode & 0o777 == 0o600
    if failure == "probe":
        assert not (home / ".config/memd/client.env").exists()


@pytest.mark.parametrize("receipt, expected_error, expected_text", [
    ({"error": "could not commit"}, True, "not confirmed"),
    ({}, True, "not confirmed"),
    ({"slug": "fact", "revision": "abc", "saved": True, "synced": False, "indexed": False, "lexical_indexed": True}, False, "remote Git sync pending"),
    ({"slug": "fact", "revision": "abc", "saved": True, "synced": True, "indexed": True, "lexical_indexed": True}, False, "Saved fact"),
])
def test_pi_save_reports_receipt_truthfully(tmp_path, receipt, expected_error, expected_text):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the pi extension integration test")
    major = int(subprocess.check_output([node, "--version"], text=True).lstrip("v").split(".")[0])
    if major < 22:
        pytest.skip("Node 22+ is required to execute TypeScript without dependencies")
    script = tmp_path / "probe.mjs"
    script.write_text('''
const {default: extension} = await import(process.env.PI_EXTENSION);
let remember;
extension({on() {}, registerTool(tool) {if (tool.name === "remember") remember = tool;}});
globalThis.fetch = async () => ({ok: true, json: async () => JSON.parse(process.env.RECEIPT)});
console.log(JSON.stringify(await remember.execute("id", {title:"test", body:"fact"})));
''')
    env = dict(os.environ, MEMD_TOKEN="test-token", PI_EXTENSION=(ROOT / "clients/pi-memd.ts").as_uri(), RECEIPT=json.dumps(receipt))
    run = subprocess.run([node, "--experimental-strip-types", str(script)], env=env, capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert bool(result.get("isError")) is expected_error
    assert expected_text in result["content"][0]["text"]


@pytest.mark.parametrize("response, expected", [
    ({"context": "Canonical bounded context", "notes": [{"slug": "ignored", "body": "legacy payload"}]}, "Canonical bounded context"),
    ({"context": "", "notes": [{"slug": "ignored", "body": "legacy payload"}]}, None),
    ({"notes": [{"slug": "important-match", "body": "FULL_BODY", "importance": 5, "matched": True}]}, "### important-match\nFULL_BODY"),
])
def test_pi_prefers_canonical_context_and_preserves_match_provenance(tmp_path, response, expected):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for pi integration test")
    major = int(subprocess.check_output([node, "--version"], text=True).lstrip("v").split(".")[0])
    if major < 22:
        pytest.skip("Node 22+ is required for TypeScript execution")
    script = tmp_path / "recall.mjs"
    script.write_text('''
const {default: extension} = await import(process.env.PI_EXTENSION);
let recall;
extension({on(name, fn) {if (name === "before_agent_start") recall = fn;}, registerTool() {}});
globalThis.fetch = async (_, request) => {
    if (JSON.parse(request.body).format !== "context") throw new Error("not requesting canonical context");
    return {ok: true, json: async () => JSON.parse(process.env.RESPONSE)};
};
const result = await recall({prompt: "test query"});
console.log(JSON.stringify(result?.message?.content ?? null));
''')
    env = dict(os.environ, MEMD_TOKEN="test-token", PI_EXTENSION=(ROOT / "clients/pi-memd.ts").as_uri(), RESPONSE=json.dumps(response))
    run = subprocess.run([node, "--experimental-strip-types", str(script)], env=env, capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    actual = json.loads(run.stdout)
    if expected is None:
        assert actual is None
    else:
        assert expected in actual


def test_doctor_accepts_committed_lexical_save_with_vectors_pending(monkeypatch):
    doctor = load_script("doctor_receipt_review", "maint/memd-doctor")
    monkeypatch.setattr(doctor, "read_token", lambda _: "test-token")
    receipt = {"saved": True, "revision": "revision", "lexical_indexed": True, "indexed": False,
               "synced": False, "slug": doctor.PROBE_SLUG}
    monkeypatch.setattr(doctor, "mcp_call", lambda *a: (200, json.dumps(receipt)))
    status, detail = doctor.c5()
    assert status == "PASS"
    assert "indexed=False synced=False" in detail
    assert doctor.STATE["nonce_a"]


def test_doctor_fails_when_the_probe_save_forks_a_new_note(monkeypatch):
    doctor = load_script("doctor_probe_fork", "maint/memd-doctor")
    monkeypatch.setattr(doctor, "read_token", lambda _: "test-token")
    receipt = {"saved": True, "revision": "r", "lexical_indexed": True,
               "slug": doctor.PROBE_SLUG + "-4f2a9c1"}
    monkeypatch.setattr(doctor, "mcp_call", lambda *a: (200, json.dumps(receipt)))
    status, detail = doctor.c5()
    assert status == "FAIL" and "forked" in detail


def test_deploy_preserves_explicit_git_ssh_command(tmp_path):
    stack = tmp_path / 'stack'
    stack.mkdir()
    shutil.copy(ROOT / 'deploy/apphost/deploy.sh', stack / 'deploy.sh')
    bins = tmp_path / 'bin'
    bins.mkdir()
    captured = tmp_path / 'git-env.json'
    executable(bins / 'docker', 'pass\n')
    executable(bins / 'git', '''
import json, os, sys
from pathlib import Path
Path(os.environ['CAPTURE']).write_text(json.dumps({k:os.environ.get(k) for k in ('GIT_SSH_COMMAND','GIT_TERMINAL_PROMPT')}))
sys.exit(1)
''')
    explicit = 'ssh -F /dev/null -i /path/to/verified-deploy-key'
    env = dict(os.environ, PATH=str(bins) + os.pathsep + os.environ['PATH'], CAPTURE=str(captured), GIT_SSH_COMMAND=explicit)
    run = subprocess.run(['/bin/sh', str(stack / 'deploy.sh')], env=env, capture_output=True, text=True, timeout=10)
    assert run.returncode != 0
    assert json.loads(captured.read_text()) == {'GIT_SSH_COMMAND': explicit, 'GIT_TERMINAL_PROMPT': '0'}


def test_concurrent_deploy_cannot_overwrite_rollback_tags(tmp_path):
    import fcntl
    stack = tmp_path / 'stack'
    stack.mkdir()
    shutil.copy(ROOT / 'deploy/apphost/deploy.sh', stack / 'deploy.sh')
    bins = tmp_path / 'bin'
    bins.mkdir()
    invoked = tmp_path / 'docker-was-invoked'
    executable(bins / 'docker', "import os\nfrom pathlib import Path\nPath(os.environ['INVOKED']).touch()\n")
    env = dict(os.environ, PATH=str(bins) + os.pathsep + os.environ['PATH'], INVOKED=str(invoked))
    with (stack / '.deploy.lock').open('w') as first_deploy:
        fcntl.flock(first_deploy, fcntl.LOCK_EX)
        run = subprocess.run(['/bin/sh', str(stack / 'deploy.sh')], env=env, capture_output=True, text=True, timeout=10)
    assert run.returncode != 0
    assert 'another memd deployment is running' in run.stderr
    assert not invoked.exists()


@pytest.mark.parametrize('existing', [True, False])
def test_failed_claude_registration_restores_previous_config(tmp_path, existing):
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    config = home / '.claude.json'
    original = json.dumps({'mcpServers': {'memd': {'url': 'http://old-valid'}, 'other': {'command': 'keep-me'}}, 'otherSetting': 7})
    if existing:
        config.write_text(original)
    # Ensure no installed real Codex binary can be selected: provide a harmless
    # stand-in and fixture config for the unrelated onboarding step.
    (home / '.codex').mkdir()
    (home / '.codex/config.toml').write_text('model="fixture"\n')
    bins = tmp_path / 'bin'
    bins.mkdir()
    executable(bins / 'curl', '''
import sys
from pathlib import Path
args=sys.argv[1:]
if '-d' in args:
    print('{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"recall"},{"name":"save"}]}}\\n200')
elif any('/clients/' in a for a in args):
    Path(args[args.index('-o') + 1]).write_text('#!/bin/sh\\nexit 0\\n')
''')
    executable(bins / 'codex', 'pass\n')
    executable(bins / 'claude', '''
import json, os, sys
from pathlib import Path
config=Path(os.environ['HOME']) / '.claude.json'
data=json.loads(config.read_text()) if config.exists() else {'mcpServers': {}}
if 'remove' in sys.argv:
    data['mcpServers'].pop('memd', None)
    config.write_text(json.dumps(data))
else:
    config.write_text('{"partial": "failed new registration"}')
    sys.exit(1)
''')
    env = dict(os.environ, HOME=str(home), PATH=str(bins) + os.pathsep + os.environ['PATH'])
    env.pop('CODEX_HOME', None)
    run = subprocess.run(['bash', str(ROOT / 'clients/onboard.sh'), 'mem_amber_test-token', 'https://invalid.test'], env=env, capture_output=True, text=True, timeout=20)
    assert run.returncode != 0
    assert 'Restored previous Claude MCP configuration.' in run.stdout
    if existing:
        assert config.read_text() == original
    else:
        assert not config.exists()
