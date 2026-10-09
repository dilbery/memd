"""`mem-sweep`: one-shot over the corpus — backfill host:, ground, report dedup.

Default dry-run; --write persists host:/grounding: frontmatter. Advisory only:
never deletes, never blocks, never merges duplicates (it only reports them).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from memd.dedup import find_duplicates
from memd.ground import ground, local_host_checker
from memd.infer_host import infer_host
from memd.store import StoreUnavailable, dump_note, iter_notes


def run_sweep(store, *, write: bool, host_checker=local_host_checker) -> dict:
    store = Path(store)
    notes = list(iter_notes(store))

    report_notes = []
    for note in notes:
        note.host = infer_host(note)               # backfill scope from content
        note.grounding = ground(note, host_checker=host_checker)
        report_notes.append({
            "path": note.path,
            "slug": note.slug,
            "host": note.host,
            "grounding": note.grounding,
        })
        if write:
            (store / note.path).write_text(dump_note(note), encoding="utf-8")

    dups = [(a.slug, b.slug) for a, b in find_duplicates(notes)]
    return {"notes": report_notes, "duplicates": dups}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mem-sweep")
    ap.add_argument("store", nargs="?", default=".")
    ap.add_argument("--write", action="store_true",
                    help="persist backfilled host: and grounding: frontmatter")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        report = run_sweep(args.store, write=args.write)
    except StoreUnavailable as exc:   # an encrypted store: --write would store plaintext
        print(f"mem-sweep: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report))
    else:
        n_local = sum(1 for n in report["notes"] if n["grounding"] == "unverified-local")
        print(f"mem-sweep: {len(report['notes'])} notes, "
              f"{len(report['duplicates'])} duplicate pairs, "
              f"{n_local} unverified-local (flag for review). "
              f"{'WROTE' if args.write else 'dry-run (use --write)'}.")
        for a, b in report["duplicates"]:
            print(f"  dup: {a}  <->  {b}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
