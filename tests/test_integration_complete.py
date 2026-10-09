"""Task 14 gate: the integration-ops deliverable set is complete and consistent.

This is the hermetic stand-in for Task 14's live ops checks (systemctl
is-active / Forgejo push), which are NOT run in this build. Instead we lock
the "everything shipped + wired" invariant into pytest: every file the
integration-ops plan's File Structure declares must exist, the Node sidecar
test must be present and runnable, and the two suites' expectations are
recorded here so a regression that drops a deliverable fails loudly.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Every file the integration-ops plan's File Structure section promises.
PYTHON_DELIVERABLES = [
    "memd/mcp.py",
    "memd/hooks/auto_recall.py",
    "memd/reflect.py",
    "memd/integrations/hermes_memd_provider.py",
    "memd/integrations/opencode_memd_sidecar.js",
    "memd/integrations/degraded_grep.py",
    "memd/profiles.py",
]

SYSTEMD_DELIVERABLES = [
    "systemd/memd-guard.service",
    "systemd/memd-guard-loop",
    "systemd/memd-reflect.service",
    "systemd/memd-reflect.timer",
]

DOC_DELIVERABLES = [
    "docs/integration-registration.md",
]

TEST_DELIVERABLES = [
    "tests/test_mcp.py",
    "tests/test_auto_recall_hook.py",
    "tests/test_reflect.py",
    "tests/test_hermes_provider.py",
    "tests/test_degraded_grep.py",
    "tests/test_profiles.py",
    "tests/test_security.py",
    "tests/test_opencode_sidecar.mjs",
]

ALL_DELIVERABLES = (
    PYTHON_DELIVERABLES + SYSTEMD_DELIVERABLES + DOC_DELIVERABLES + TEST_DELIVERABLES
)


@pytest.mark.parametrize("rel", ALL_DELIVERABLES)
def test_deliverable_exists_and_nonempty(rel):
    p = ROOT / rel
    assert p.is_file(), f"missing integration-ops deliverable: {rel}"
    assert p.stat().st_size > 0, f"empty integration-ops deliverable: {rel}"


def test_sidecar_test_declares_three_cases():
    """Task 14 expects the Node sidecar suite to report `# pass 3, # fail 0`."""
    mjs = (ROOT / "tests" / "test_opencode_sidecar.mjs").read_text(encoding="utf-8")
    assert mjs.count("test(") >= 3, "sidecar test must declare at least 3 cases"
    assert "node:test" in mjs or "node:assert" in mjs


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_node_sidecar_suite_passes():
    """Run the Node sidecar suite hermetically; it mocks fetch, hits no network."""
    mjs = ROOT / "tests" / "test_opencode_sidecar.mjs"
    # The plan documents the TAP-reporter expectation `# pass 3 / # fail 0`;
    # pin the reporter so the assertion holds across Node versions (newer
    # Node defaults to the `spec` reporter which prints `ℹ pass 3`).
    proc = subprocess.run(
        ["node", "--test", "--test-reporter=tap", str(mjs)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"node sidecar suite failed:\n{out}"
    assert "# fail 0" in out, f"expected `# fail 0` in:\n{out}"
    assert "# pass 3" in out, f"expected `# pass 3` in:\n{out}"
