"""mem-nightly: run the background memory jobs in order and report once.

Steps run in this order; choose them with --steps or MEMD_NIGHTLY_STEPS
(comma-separated). The default is facts,summarize,health.

  facts      mem-facts: extract and close time-bounded facts (patterns always,
             the chat model too when MEMD_LLM_URL is set)
  summarize  mem-summarize: propose current-state summaries on a review branch
             (skipped, not failed, when MEMD_LLM_URL is unset)
  verify     mem-verify: run the notes' read-only probes and propose results.
             Opt-in: the probes go out from whichever host runs this job.
  forget     mem-forget: propose archiving old, unused, low-importance notes
             on a review branch. Opt-in: a store should be reviewed by hand
             (mem-forget --dry-run) before its archive proposals are routine.
  backup     mem-backup create: write one encrypted bundle of every store to
             MEMD_BACKUP_DIR and prune to MEMD_BACKUP_KEEP. Opt-in: it needs
             MEMD_BACKUP_DIR and MEMD_BACKUP_KEY_FILE.
  drill      mem-backup drill: restore the newest bundle into scratch space,
             check it against its manifest and run a sample recall. Opt-in.
  health     mem-health: print the memory health report

Each step runs even when an earlier one failed. The exit status is 1 when any
step failed, so a timer unit shows the failure; findings (stale notes, failed
probes) are reports, not failures.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable

ORDER = ("facts", "summarize", "verify", "forget", "backup", "drill", "health")
DEFAULT_STEPS = ("facts", "summarize", "health")
# Flags each step's own CLI accepts, so --dry-run/--push are only passed where they mean something.
DRY_RUN = {"facts", "summarize", "verify", "forget"}
PUSH = {"summarize", "verify", "forget"}


def _main(step: str) -> Callable[[list[str]], int]:
    if step == "facts":
        from memd.facts import main
    elif step == "summarize":
        from memd.summarize import main
    elif step == "verify":
        from memd.verify import main
    elif step == "forget":
        from memd.forget import main
    elif step in ("backup", "drill"):
        from memd.backup import main as backup_main
        command = "create" if step == "backup" else "drill"
        return lambda argv: backup_main([command, *argv])
    else:
        from memd.insights import main
    return main


def parse_steps(raw: str | None) -> tuple[str, ...]:
    """Validated steps in run order; ValueError names an unknown step."""
    if not raw or not raw.strip():
        return DEFAULT_STEPS
    wanted = {s.strip().lower() for s in raw.split(",") if s.strip()}
    unknown = wanted - set(ORDER)
    if unknown:
        raise ValueError(f"unknown step(s): {', '.join(sorted(unknown))}; choose from {', '.join(ORDER)}")
    return tuple(s for s in ORDER if s in wanted)


def _skip_reason(step: str, dry_run: bool) -> str | None:
    if step == "summarize" and not dry_run:
        from memd.config import Config
        from memd.llm import enabled
        if not enabled(Config.from_env()):
            return "MEMD_LLM_URL is not set"
    return None


def run(steps: tuple[str, ...], *, dry_run: bool = False, push: bool = False,
        runner: Callable[[str], Callable[[list[str]], int]] | None = None) -> list[dict]:
    runner = runner or _main
    results = []
    for step in steps:
        argv = (["--dry-run"] if dry_run and step in DRY_RUN else []) + \
               (["--push"] if push and not dry_run and step in PUSH else [])
        print(f"== mem-nightly: {step} {' '.join(argv)}".rstrip(), flush=True)
        started = time.monotonic()
        reason = _skip_reason(step, dry_run)
        if reason:
            print(f"   skipped: {reason}", flush=True)
            results.append({"step": step, "status": "skipped", "detail": reason, "seconds": 0.0})
            continue
        try:
            code = runner(step)(argv)
            status, detail = ("ok", "") if not code else ("failed", f"exit {code}")
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            status, detail = ("ok", "") if not code else ("failed", f"exit {code}")
        except Exception as exc:   # one broken job must not stop the rest
            status, detail = "failed", f"{type(exc).__name__}: {exc}"
        results.append({"step": step, "status": status, "detail": detail,
                        "seconds": round(time.monotonic() - started, 1)})
        sys.stdout.flush()
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mem-nightly", description=__doc__.split("\n\n")[0])
    ap.add_argument("--steps", default=None,
                    help=f"comma-separated subset of {','.join(ORDER)} "
                         f"(default MEMD_NIGHTLY_STEPS or {','.join(DEFAULT_STEPS)})")
    ap.add_argument("--dry-run", action="store_true", help="pass --dry-run to every step that has one")
    ap.add_argument("--push", action="store_true", help="push the summarize/verify/forget review branches")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        steps = parse_steps(args.steps if args.steps is not None else os.environ.get("MEMD_NIGHTLY_STEPS"))
    except ValueError as exc:
        print(f"mem-nightly: {exc}", file=sys.stderr)
        return 2
    results = run(steps, dry_run=args.dry_run, push=args.push)
    print("== mem-nightly summary")
    for r in results:
        print(f"   {r['step']:<10} {r['status']:<8} {r['seconds']:>6}s {r['detail']}".rstrip())
    return 1 if any(r["status"] == "failed" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
