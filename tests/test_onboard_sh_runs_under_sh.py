"""onboard.sh must survive being started by a POSIX /bin/sh.

Regression guard for onboarding a fresh Debian/Ubuntu dev VM. The one-liner printed by
memd's own index page -- and repeated in the apphost design doc and in the
script's own usage text -- is:

    curl -sO https://memd.example.com/clients/onboard.sh && sh onboard.sh <token>

On Debian/Ubuntu /bin/sh is dash, but onboard.sh is bash: it uses arrays
(`missing=()`), substring expansion (`${TOKEN:0:8}`), `[[ ... =~ ... ]]` with
BASH_REMATCH, and `$'\\n'`. dash aborts while *parsing*, before running a single
line, so the documented command failed on every Debian-family machine with:

    onboard.sh: 54: Syntax error: "(" unexpected

Nothing caught it because the developer boxes run zsh/bash as /bin/sh, and the
one place the command is written down is prose, not code.

The fix is a POSIX-only re-exec guard at the top of onboard.sh that hands off to
bash. Re-execing beats rewriting the script as POSIX (it keeps the bash the rest
of the script is written in) and beats changing the docs to say `bash` (the
command people already have in their scrollback keeps working).

Two checks here:

* ``test_reexec_guard_precedes_every_bashism`` is the load-bearing one and runs
  everywhere. The guard is only worth anything if it sits *before* the first
  bash-only token -- a guard placed after `missing=()` would parse-fail exactly
  like the original bug, and a runtime test would never catch it on a box whose
  /bin/sh is already bash.
* ``test_runs_under_posix_sh`` actually executes the script under a real POSIX
  shell, and skips when none is installed (gpuhost, the usual dev box, has no
  dash/busybox at all). It asserts only that parsing succeeded -- the script is
  expected to exit non-zero on the bogus token, which is itself proof it got
  past the arrays to the token check.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ONBOARD = Path(__file__).resolve().parents[1] / "clients" / "onboard.sh"

# Constructs dash cannot parse. Each is present in onboard.sh today; the point
# is not to forbid them but to require the guard to come first.
BASHISMS = (
    (r"\w+=\(", "array assignment"),
    (r"\+=\(", "array append"),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*:[0-9]+:[0-9]+\}", "substring expansion"),
    (r"\[\[", "[[ ... ]] test"),
    (r"\$'", "$'...' ANSI-C quoting"),
    (r"BASH_REMATCH", "BASH_REMATCH"),
)

# The guard's own explanatory comment names the bashisms it protects, so scan
# only code. onboard.sh has no trailing `#` comments after code on these lines.
def _code_lines():
    for n, raw in enumerate(ONBOARD.read_text().splitlines(), start=1):
        if not raw.lstrip().startswith("#"):
            yield n, raw


def test_reexec_guard_precedes_every_bashism():
    guard_line = None
    for n, line in _code_lines():
        if re.search(r'exec\s+bash\s+"\$0"', line):
            guard_line = n
            break
    assert guard_line is not None, (
        "onboard.sh has no `exec bash \"$0\"` re-exec guard. Without it, "
        "`sh onboard.sh` -- the command memd's index page prints -- dies with a "
        "syntax error on any box where /bin/sh is dash."
    )

    for n, line in _code_lines():
        for pattern, label in BASHISMS:
            if re.search(pattern, line):
                assert n > guard_line, (
                    f"onboard.sh line {n} uses {label}, which dash cannot parse, "
                    f"but the re-exec guard is at line {guard_line}. dash fails at "
                    f"PARSE time, so anything bash-only above the guard breaks the "
                    f"script before the guard can run."
                )
                break


@pytest.mark.parametrize("shell", ["dash", "busybox", "posh", "ash"])
def test_runs_under_posix_sh(shell):
    """Start the script with a real POSIX shell; it must parse and run."""
    exe = shutil.which(shell)
    if exe is None:
        pytest.skip(f"{shell} not installed")
    argv = [exe, "sh", str(ONBOARD)] if shell == "busybox" else [exe, str(ONBOARD)]

    # No token, stdin not a tty -> the usage branch. Reaching usage means the
    # whole file parsed, which is the entire point.
    proc = subprocess.run(
        argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60
    )
    combined = proc.stdout + proc.stderr
    assert "Syntax error" not in combined, (
        f"{shell} could not parse onboard.sh -- the re-exec guard is missing or "
        f"too late in the file:\n{combined}"
    )
    assert "Usage:" in combined, f"expected usage text under {shell}, got:\n{combined}"
