"""`mem-carve`: write a budget-fitting MEMORY.md + the complete MEMORY-full.md."""
from __future__ import annotations

import sys
from pathlib import Path

from memd.carve import render_core, render_full, select_core
from memd.config import MEMORY_BUDGET_BYTES
from memd.store import StoreUnavailable, iter_notes


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    core_only = "--core" in args          # tiny always-on core; the rest recall-only
    args = [a for a in args if a != "--core"]
    store = Path(args[0]) if args else Path.cwd()

    try:
        notes = list(iter_notes(store))
    except StoreUnavailable as exc:   # an encrypted store: MEMORY.md would be plaintext
        print(f"mem-carve ERROR: {exc}", file=sys.stderr)
        return 2
    core = select_core(notes, MEMORY_BUDGET_BYTES, core_only=core_only)

    core_text = render_core(core)
    full_text = render_full(notes)

    core_bytes = len(core_text.encode("utf-8"))
    if core_bytes >= MEMORY_BUDGET_BYTES:
        print(f"mem-carve ERROR: core {core_bytes} >= {MEMORY_BUDGET_BYTES}",
              file=sys.stderr)
        return 1

    (store / "MEMORY.md").write_text(core_text, encoding="utf-8")
    (store / "MEMORY-full.md").write_text(full_text, encoding="utf-8")
    print(
        f"mem-carve OK: MEMORY.md = {core_bytes} bytes "
        f"({len(core)}/{len(notes)} notes), MEMORY-full.md = {len(notes)} notes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
