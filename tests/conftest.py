"""Shared fixtures: real-note corpus + injectable host checkers (no live services).

Also provides hermetic test fixtures: a temp git repo seeded with markdown notes
(so stale-HEAD and commit paths work) plus a Config pointed at them. The
embedding/rerank backend fakes live in their own tests via respx.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

NOTE_TUNING = """\
---
title: gpuhost inference tuning
slug: gpuhost-inference-tuning
profile: amber
host: gpuhost
importance: 4
tags: [lemonade, gpu, ctx]
grounding: ok
---
Lemonade tuning: ub 1024 Vulkan sweet spot. Main model: a 35B-A3B MoE with MTP.
"""

NOTE_VMHOST = """\
---
title: vmhost Proxmox VM
slug: vmhost-proxmox-vm
profile: amber
host: vmhost
importance: 2
tags: [proxmox, trackr]
grounding: unverified-remote
---
Trackr host 10.10.1.11 is Proxmox VM 210; onboot OFF.
"""

NOTE_CORE = """\
---
title: Repo hosting policy
slug: repo-hosting-policy
profile: amber
host: any
importance: 5
tags: [git, policy]
grounding: ok
---
ALL new repos go to Forgejo only (svcuser/ on 10.10.1.10:3000); never GitHub.
"""


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def corpus_dir(tmp_path: Path) -> Path:
    """A writable copy of the fixture corpus (so --write tests don't touch the source)."""
    dst = tmp_path / "memory"
    dst.mkdir()
    for md in FIXTURES.glob("*.md"):
        shutil.copy(md, dst / md.name)
    return dst


@pytest.fixture
def deny_all_checker():
    """Host-checker stub: every command/path is MISSING (no live which/exists)."""
    return lambda kind, target: False


@pytest.fixture
def allow_all_checker():
    """Host-checker stub: every command/path EXISTS."""
    return lambda kind, target: True


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def git_clone(tmp_path: Path) -> Path:
    """A real git repo seeded with 3 notes; acts as MEMD_CLONE."""
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@memd")
    _git(repo, "config", "user.name", "memd-test")
    for name, content in [
        ("gpuhost-inference-tuning.md", NOTE_TUNING),
        ("vmhost-proxmox-vm.md", NOTE_VMHOST),
        ("repo-hosting-policy.md", NOTE_CORE),
    ]:
        (repo / name).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed notes")
    return repo


@pytest.fixture
def git_head(git_clone: Path):
    def _head() -> str:
        return _git(git_clone, "rev-parse", "HEAD")
    return _head


@pytest.fixture(autouse=True)
def _never_read_the_real_env_file(monkeypatch, tmp_path_factory):
    """Hermeticity floor: no test may inherit the developer's memd config.

    Two layers leak it, and the suite needs BOTH shut.

    1. The env FILE. Config.from_env() layers ~/.config/memd/env under the
       process env, so a box-local MEMD_EMBED_URL silently decides what the
       suite talks to — green here, broken on a fresh checkout. Repointing the
       module default covers both resolution paths: `from_env()` reading
       os.environ, and `from_env({...})` passed an explicit dict (which never
       sees an env var we could set).

    2. The process ENV itself — the hole this fixture originally missed.
       On any machine where memd is actually installed, the secrets manager and
       a shell profile snippet export MEMD_REMOTE / MEMD_TOKEN / MEMD_URL into
       every shell, so pytest inherits them. That flipped
       `auto_recall.build_context()` onto its `if remote:` branch, where it
       never called the `_core_recall` those tests monkeypatch, and instead
       made LIVE authenticated POSTs to https://memd.example.com/recall with the
       developer's real token — pulling production notes into test
       assertions. Several tests failed on an installed machine and passed on a
       fresh checkout, i.e. the suite was dead exactly where it mattered.

    Enumerate by prefix rather than deleting a fixed list, so the next MEMD_
    var someone exports does not quietly reopen this. A test that genuinely
    wants remote mode sets MEMD_REMOTE in its own body, which runs after this
    autouse fixture and therefore still wins.
    """
    import memd.config as _config
    absent = tmp_path_factory.mktemp("no-env-file") / "absent.env"
    monkeypatch.setattr(_config, "DEFAULT_ENV_FILE", absent)
    for key in [k for k in os.environ if k.startswith("MEMD_")]:
        monkeypatch.delenv(key)
    # Background workers outlive request fixtures; ordinary tests exercise
    # deferred scheduling without launching threads against restored mocks.
    monkeypatch.setenv("MEMD_BACKGROUND_REFRESH", "0")
    monkeypatch.setenv("MEMD_STARTUP_REFRESH", "0")
    # Some tests serve recalls from a default profile's store; the usage log
    # would otherwise write next to that index. tests/test_usage.py turns it on.
    monkeypatch.setenv("MEMD_USAGE_LOG", "off")


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch):
    """Hermeticity ceiling: no test may open an outbound IP connection.

    Kept separate from the env floor above because it guards a different thing.
    The floor is a blocklist — it stops the leak we know about (MEMD_* steering
    auto_recall onto its HTTP branch). This is the structural guarantee: a future
    code path that hardcodes a URL, reads a different variable, or grows a new
    default endpoint still cannot reach the network from inside a test.

    Rationale and the incident it prevents are in tests/netguard.py.
    """
    from tests.netguard import install_socket_guard
    install_socket_guard(monkeypatch)


@pytest.fixture(autouse=True)
def _httpx2_through_respx(monkeypatch):
    """Route httpx2's async transport through httpx so respx still sees it.

    mcp 2.x depends on httpx2, and once it is installed Authlib's
    AsyncOAuth2Client (the browser consoles' token exchange) is built on httpx2
    instead of httpx. respx only patches httpx's transport stack, so without
    this the token POST would escape every respx router and hit the socket
    guard. Each request is replayed byte for byte through a plain httpx
    transport and the response handed back as an httpx2 one; nothing about
    what the test asserts changes.
    """
    try:
        import httpx2
    except ImportError:
        return
    import httpx

    async def handle_async_request(self, request):
        legacy = httpx.Request(request.method, str(request.url),
                               headers=request.headers.multi_items(), content=await request.aread())
        async with httpx.AsyncHTTPTransport() as transport:
            response = await transport.handle_async_request(legacy)
            body = b"".join([chunk async for chunk in response.aiter_raw()])
        return httpx2.Response(response.status_code, headers=response.headers.multi_items(),
                               content=body, request=request)

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", handle_async_request)


@pytest.fixture
def config(git_clone: Path, tmp_path: Path, monkeypatch):
    from memd.config import Config
    monkeypatch.setenv("MEMD_CLONE", str(git_clone))
    monkeypatch.setenv("MEMD_DB", str(tmp_path / "memd.db"))
    monkeypatch.setenv("MEMD_PROFILE", "amber")
    monkeypatch.setenv("MEMD_TOKEN", "test-token")
    # env_file=None keeps the suite hermetic: from_env() otherwise layers the
    # developer's real ~/.config/memd/env underneath, so a box-local
    # MEMD_EMBED_URL would silently steer tests at a live backend.
    return Config.from_env(env_file=None)
