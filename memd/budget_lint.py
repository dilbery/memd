"""Byte-budget lint: exit non-zero if MEMORY.md is at or over the budget.

Wired into a memory-sync script as a pre-commit gate so a silent over-budget
commit can never re-introduce the loader-truncation bug.
"""
from __future__ import annotations

import sys
from pathlib import Path

from memd.config import MEMORY_BUDGET_BYTES


def check_budget(path, budget: int = MEMORY_BUDGET_BYTES) -> tuple[bool, int]:
    data = Path(path).read_bytes()
    size = len(data)
    return (size < budget, size)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = args[0] if args else "MEMORY.md"
    try:
        ok, size = check_budget(path)
    except FileNotFoundError:
        print(f"budget-lint: {path} not found (skipping)", file=sys.stderr)
        return 0
    if ok:
        print(f"budget-lint OK: {path} = {size} / {MEMORY_BUDGET_BYTES} bytes")
        return 0
    print(
        f"budget-lint FAIL: {path} = {size} bytes >= budget {MEMORY_BUDGET_BYTES}. "
        f"Re-run mem-carve to shrink MEMORY.md (full index lives in MEMORY-full.md).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
