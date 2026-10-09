"""The suite must not inherit the developer's memd configuration.

Regression guard for a leak found while investigating six failures that only
ever appeared on a machine where memd is installed.

On any machine where memd is actually installed, the secrets manager and the
shell profile export ``MEMD_REMOTE`` / ``MEMD_TOKEN`` /
``MEMD_URL`` into every shell, so pytest inherits them. That flipped
``memd.hooks.auto_recall.build_context()`` onto its ``if remote:`` branch, where
it never called the ``_core_recall`` the tests monkeypatch and instead made
**live authenticated POSTs to https://memd.example.com/recall with the developer's
real token** — pulling production notes into test assertions.
Confirmed against memd's own access log during a run.

The consequence is the bad kind: the suite was green on a fresh checkout and on
CI, and red only on the machines where memd actually runs. `conftest.py` already
had a hermeticity floor for the env *file* and missed the process *env* — the
same bug class one layer up.

``_AMBIENT_MEMD`` is captured at import time, which pytest does during
collection, before any fixture runs. So it sees the real inherited environment
even though the autouse floor will have cleared it by the time a test body runs.
That snapshot is what makes ``test_ambient_memd_env_is_cleared`` a real check
rather than a tautology.
"""

import os

# Captured at collection time -- before conftest's autouse floor has run.
_AMBIENT_MEMD = {k: v for k, v in os.environ.items() if k.startswith("MEMD_")}
_FIXTURE_MEMD = {"MEMD_BACKGROUND_REFRESH": "0", "MEMD_STARTUP_REFRESH": "0", "MEMD_USAGE_LOG": "off"}


def test_no_memd_env_visible_to_tests():
    """No MEMD_* may reach a test body from the ambient environment.

    Trivially true on a machine that has never configured memd; the point is
    that it fails loudly on one that has, naming the leak directly instead of
    surfacing as six confusing assertion errors elsewhere in the suite.
    """
    assert {k: os.environ.get(k) for k in _FIXTURE_MEMD} == _FIXTURE_MEMD
    leaked = sorted(k for k in os.environ if k.startswith("MEMD_") and k not in _FIXTURE_MEMD)
    assert leaked == [], (
        f"conftest's hermeticity floor is not clearing {leaked}. Tests that "
        f"read MEMD_REMOTE will take the remote branch and POST to the real "
        f"memd server with the real token."
    )


def test_ambient_memd_env_is_cleared():
    """Every var actually inherited from the developer's shell must be gone.

    This is the assertion that would have caught the original bug. It only has
    teeth on a configured box -- which is exactly where the leak existed and
    where the six failures showed up.
    """
    if not _AMBIENT_MEMD:
        # Nothing was inherited, so there is nothing to prove here. Not a skip:
        # a clean environment is a pass, and skips get ignored in CI summaries.
        return
    still_set = sorted(k for k in _AMBIENT_MEMD if k in os.environ and k not in _FIXTURE_MEMD)
    assert {k: os.environ.get(k) for k in _FIXTURE_MEMD} == _FIXTURE_MEMD
    assert still_set == [], (
        f"these were inherited from the shell and survived the floor: "
        f"{still_set} (inherited: {sorted(_AMBIENT_MEMD)})"
    )


def test_auto_recall_takes_the_local_branch_by_default():
    """With the floor in place, the hook must not choose its remote path.

    build_context() branches on MEMD_REMOTE. If the floor regresses, this flips
    to the HTTP path and the suite starts talking to production -- so assert the
    branch condition directly rather than inferring it from downstream output.
    """
    import memd.hooks.auto_recall as ar

    assert os.environ.get("MEMD_REMOTE", "") == "", (
        "MEMD_REMOTE is visible to tests; build_context() will take its remote "
        "HTTP branch and bypass every _core_recall monkeypatch in the suite."
    )
    # Guard the attribute the branch reads, so a rename of the env var without
    # updating this file shows up here rather than as silent live traffic.
    assert hasattr(ar, "_remote_recall") and hasattr(ar, "_core_recall")
