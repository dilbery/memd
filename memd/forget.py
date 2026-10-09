"""mem-forget: propose archiving notes nothing uses any more (PROPOSE-ONLY).

Keeps a store lean without ever deleting on its own. Candidates are chosen
conservatively and every one is explained; the proposal is a commit on a REVIEW
BRANCH (memd/summarize's write_proposals: a throwaway index under the clone
lock, the checkout untouched, envelopes for an encrypted store). Merging the
branch approves; nothing is archived until then.

A candidate must pass every gate:
  * live: not superseded, retracted or already archived, and committed;
  * old: its last activity (the newest of verified_at, observed_at, the date in
    its title or slug, and the last commit touching its file) is more than
    `days` (180) ago, so any edit, re-verification or re-save keeps a note;
  * unprotected: not pinned, not core-eligible (importance 4+), not a
    current-state summary, not a source cited by a live summary, not the
    replacement another note was superseded by (or names in `supersedes`), not
    the source of a note published from it (`published_from`);
  * unimportant: importance <= `max_importance` (2, at most 3);
  * and at least one signal that it is dead weight:
      - never recalled (and never read) in the usage window, when the usage log
        covers at least MIN_USAGE_DAYS and the note existed before it began;
      - stale by memd.staleness (changeable state past its freshness window, or
        a recent failed mem-verify check);
      - every fact it states was closed by newer facts from other notes
        (memd.facts supersede candidates).

Archiving is frontmatter, not a move: the proposal adds ``archived: <date>`` and
``archived_reason`` to the note in place. The file keeps its path, so ``git log``
on it reads as one unbroken history (a move to an archive/ directory would make
every archive a rename, and an encrypted store's opaque file names carry no
directory at all); restoring is deleting two keys (``mem-forget restore
<slug>``, a save to the note, or reverting the merge). Archived notes are
excluded from recall, the core index and the health report's live counts,
recall's ``include_archived`` finds them again, and ``read`` marks them.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from memd.config import Config
from memd.staleness import STALE_AFTER_DAYS, _parse_date, as_of, failed_verification, is_stale
from memd.store import (ARCHIVE_KEYS, RETRACTED, Note, dump_note, is_archived, list_notes,
                        parse_text)

GENERATOR = "mem-forget"
DEFAULT_BRANCH = "memd/forget"      # MEMD_FORGET_BRANCH overrides
DEFAULT_DAYS = 180                  # MEMD_FORGET_DAYS overrides
DEFAULT_MAX_IMPORTANCE = 2          # MEMD_FORGET_MAX_IMPORTANCE overrides
IMPORTANCE_CEILING = 3              # 4+ is core-eligible and never archived
DEFAULT_MAX = 25                    # proposals per run
MIN_DAYS = 30
MIN_USAGE_DAYS = 30.0               # a usage log younger than this cannot call a note unused
GIT_TIMEOUT_S = 30


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, "") or default)))
    except ValueError:
        return default


def branch_name() -> str:
    return os.environ.get("MEMD_FORGET_BRANCH", "").strip() or DEFAULT_BRANCH


def default_days() -> int:
    return _env_int("MEMD_FORGET_DAYS", DEFAULT_DAYS, MIN_DAYS, 36500)


def default_max_importance() -> int:
    return _env_int("MEMD_FORGET_MAX_IMPORTANCE", DEFAULT_MAX_IMPORTANCE, 1, IMPORTANCE_CEILING)


def _rel(clone: Path, path: str) -> str:
    p = Path(path)
    return p.relative_to(clone).as_posix() if p.is_absolute() else p.as_posix()


def _git(clone: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True,
                          check=check, timeout=GIT_TIMEOUT_S)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def last_commits(clone: Path) -> dict[str, dt.date]:
    """Path -> UTC date of the newest commit on HEAD that touched it ({} without Git)."""
    try:
        out = _git(clone, "-c", "core.quotePath=false", "log", "--no-renames",
                   "--format=@%ct", "--name-only", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    dates: dict[str, dt.date] = {}
    when = None
    for line in out.stdout.splitlines():
        if line.startswith("@") and line[1:].isdigit():
            when = dt.datetime.fromtimestamp(int(line[1:]), dt.timezone.utc).date()
        elif line and when is not None:
            dates.setdefault(line, when)
    return dates


def last_activity(note: Note, committed: dt.date | None) -> tuple[dt.date | None, str]:
    """The newest sign of life of a note, and what it was."""
    data = note.to_dict()
    seen = [(_parse_date(note.verified_at), "verified"), (_parse_date(note.observed_at), "observed"),
            (as_of(data), "dated"), (committed, "last commit")]
    seen = [(d, basis) for d, basis in seen if d is not None]
    if not seen:
        return None, ""
    return max(seen, key=lambda s: s[0])


def _usage(cfg: Config, db_path: Path | None, clone: Path, live: list[Note],
           now: float) -> tuple[dict, set[str]]:
    """(usage evidence, slugs never recalled nor read in a long enough window)."""
    from memd.insights import _older_than, _usage as usage_section
    if db_path is None:
        return {"available": False, "reason": "No index configured."}, set()
    section, shown, reads = usage_section(cfg, Path(db_path), now)
    info = {k: section.get(k) for k in ("available", "reason", "recalls", "since", "window_days")}
    if not section.get("available"):
        return info, set()
    if section["window_days"] < MIN_USAGE_DAYS:
        info.update(available=False,
                    reason=f"The usage log covers {section['window_days']} days; "
                           f"at least {MIN_USAGE_DAYS:g} are needed to call a note unused.")
        return info, set()
    old, basis = _older_than(live, clone, section["start"])
    info["basis"] = basis
    return info, {n.slug for n in old if not shown.get(n.slug) and not reads.get(n.slug)}


def _facts(db_path: Path | None) -> tuple[dict, dict[str, dict]]:
    """(facts evidence, slug -> supersede candidate) from the index's facts."""
    from memd.insights import _ro, _tables
    reason = "No facts extracted yet; run mem-facts to derive them."
    if db_path is None or not Path(db_path).exists():
        return {"available": False, "reason": reason}, {}
    conn = _ro(Path(db_path))
    try:
        if "facts" not in _tables(conn) or not conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]:
            return {"available": False, "reason": reason}, {}
        from memd.facts import supersede_candidates
        found = {c["slug"]: c for c in supersede_candidates(conn)}
    finally:
        conn.close()
    return {"available": True, "reason": None}, found


def _stale_reason(note: Note, today: dt.date) -> str | None:
    data = note.to_dict()
    if not is_stale(data, today):
        return None
    failed = failed_verification(data, today)
    if failed:
        return f"verification failed {failed['checked_at'].isoformat()}: {failed['probe']}"
    d = as_of(data)
    return (f"stale: {note.volatility} note past its {STALE_AFTER_DAYS[note.volatility]}-day "
            f"freshness window (as of {d.isoformat() if d else 'unknown'})")


def _protections(notes: list[Note], profile: str) -> dict[str, str]:
    """slug -> why other notes still depend on it."""
    from memd.summarize import SUMMARY_KIND, _sources
    out: dict[str, str] = {}
    for n in sorted(notes, key=lambda n: n.slug):
        live = not n.superseded_by and not is_archived(n)
        if n.superseded_by and n.superseded_by != RETRACTED:
            out.setdefault(n.superseded_by, f"replaces superseded note {n.slug}")
        if not live:
            continue
        named = n.metadata.get("supersedes")
        for slug in named if isinstance(named, list) else [named]:
            if isinstance(slug, str) and slug and slug != n.slug:
                out.setdefault(slug, f"named in supersedes by {n.slug}")
        if n.metadata.get("kind") == SUMMARY_KIND:
            for slug in _sources(n):
                if slug != n.slug:
                    out.setdefault(slug, f"a source cited by summary {n.slug}")
        origin = n.metadata.get("published_from")
        if isinstance(origin, dict) and isinstance(origin.get("slug"), str) \
                and origin.get("store") in (None, "", profile) and origin["slug"] != n.slug:
            out.setdefault(origin["slug"], f"the source of published note {n.slug}")
    return out


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def select(notes: list[Note], *, clone: Path, today: dt.date, days: int, max_importance: int,
           profile: str, never_recalled: set[str], closed: dict[str, dict],
           usage: dict, committed: dict[str, dt.date]) -> tuple[list[dict], list[dict]]:
    """(candidates, protected): each with its reasons, oldest activity first."""
    from memd.summarize import SUMMARY_KIND
    max_importance = max(1, min(IMPORTANCE_CEILING, int(max_importance)))
    depends = _protections(notes, profile)
    cutoff = today - dt.timedelta(days=days)
    candidates: list[dict] = []
    protected: list[dict] = []
    for n in sorted(notes, key=lambda n: n.slug):
        if n.superseded_by or n.slug == RETRACTED or is_archived(n):
            continue
        signals = []
        if n.slug in never_recalled:
            signals.append(f"never recalled or read in the usage window since {usage.get('since')} "
                           f"({usage.get('recalls')} recalls)")
        stale = _stale_reason(n, today)
        if stale:
            signals.append(stale)
        if n.slug in closed:
            c = closed[n.slug]
            signals.append(f"every fact ({c['facts']}) was closed by newer notes: "
                           + ", ".join(c["closed_by"]))
        if not signals:
            continue
        path = _rel(clone, n.path)
        when = committed.get(path)
        if when is None:
            continue                      # not committed yet: brand new to the store
        seen, basis = last_activity(n, when)
        if seen is None or seen > cutoff:
            continue
        ref = {"slug": n.slug, "title": n.title, "path": path, "importance": n.importance,
               "last_activity": seen.isoformat(), "basis": basis, "signals": signals}
        guard = ("pinned" if n.pinned
                 else f"core-eligible (importance {n.importance})" if n.importance >= 4
                 else "a current-state summary (mem-summarize maintains it)"
                 if n.metadata.get("kind") == SUMMARY_KIND
                 else depends.get(n.slug))
        if guard:
            protected.append({**ref, "reason": guard})
            continue
        if n.importance > max_importance:
            continue
        ref["reasons"] = [f"importance {n.importance} (at most {max_importance}), not pinned",
                          f"no activity since {seen.isoformat()} ({basis}), over {days} days"] + signals
        candidates.append(ref)
    candidates.sort(key=lambda c: (c["last_activity"], c["slug"]))
    return candidates, protected


def archived_note(note: Note, *, today: dt.date, reason: str) -> Note:
    metadata = {k: v for k, v in note.metadata.items() if k not in ARCHIVE_KEYS}
    metadata.update(archived=today.isoformat(), archived_reason=reason)
    return dataclasses.replace(note, metadata=metadata, saved_by=GENERATOR)


def _unarchived(note: Note) -> Note:
    return dataclasses.replace(note, metadata={k: v for k, v in note.metadata.items()
                                               if k not in ARCHIVE_KEYS})


def _same_note(a: Note, b: Note) -> bool:
    """True when a and b differ at most in their archive marker and writer."""
    return dump_note(dataclasses.replace(_unarchived(a), saved_by="")) == \
        dump_note(dataclasses.replace(_unarchived(b), saved_by=""))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run(cfg: Config, *, dry_run: bool = False, days: int | None = None,
        max_importance: int | None = None, max_proposals: int = DEFAULT_MAX,
        branch: str | None = None, now: float | None = None) -> dict:
    """Select archive candidates and (unless dry_run) propose them on the review branch."""
    from memd.summarize import pending_text, write_proposals
    clone = Path(cfg.clone)
    branch = branch or branch_name()
    now = time.time() if now is None else now
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    days = default_days() if days is None else max(MIN_DAYS, int(days))
    max_importance = default_max_importance() if max_importance is None \
        else max(1, min(IMPORTANCE_CEILING, int(max_importance)))
    db_path = Path(cfg.db) if cfg.db is not None else None

    notes = list_notes(clone)
    live = [n for n in notes if not n.superseded_by and n.slug != RETRACTED and not is_archived(n)]
    usage, never = _usage(cfg, db_path, clone, live, now)
    facts, closed = _facts(db_path)
    candidates, protected = select(
        notes, clone=clone, today=today, days=days, max_importance=max_importance,
        profile=cfg.profile, never_recalled=never, closed=closed, usage=usage,
        committed=last_commits(clone))

    by_slug = {n.slug: n for n in notes}
    todo = candidates[:max(0, int(max_proposals))]
    texts: dict[str, str] = {}
    for c in candidates:
        c["deferred"] = c not in todo
        c["reused"] = False
        if c["deferred"]:
            continue
        note = by_slug[c["slug"]]
        waiting = pending_text(clone, branch, c["path"])
        if waiting is not None:
            prior = parse_text(waiting, path=c["path"])
            if is_archived(prior) and prior.slug == note.slug and _same_note(prior, note):
                texts[c["path"]] = waiting      # unchanged: keep its date and reasons
                c["reused"] = True
                continue
        texts[c["path"]] = dump_note(archived_note(note, today=today, reason="; ".join(c["signals"])))

    report: dict = {
        "dry_run": dry_run, "branch": None, "commit": None, "today": today.isoformat(),
        "settings": {"days": days, "max_importance": max_importance, "max": max_proposals},
        "evidence": {"usage": usage, "facts": facts},
        "archived": sum(1 for n in notes if is_archived(n) and not n.superseded_by),
        "proposed": len(texts), "candidates": candidates, "protected": protected,
        "_texts": texts,
    }
    if texts and not dry_run:
        lines = [f"- {c['slug']}: {'; '.join(c['signals'])}" for c in todo]
        message = (f"forget: propose archiving {len(texts)} note(s) {today.isoformat()}\n\n"
                   + "\n".join(lines) + f"\n\nProposed-By: {GENERATOR}")
        report.update(branch=branch, commit=write_proposals(clone, texts, branch=branch, message=message))
    return report


def pending(clone: Path | None, branch: str | None = None) -> int | None:
    """Notes proposed on the forget branch and not merged yet; 0 without a branch, None if Git fails."""
    if clone is None or not Path(clone).exists():
        return None
    branch = branch or branch_name()
    try:
        exists = _git(Path(clone), "rev-parse", "-q", "--verify", f"refs/heads/{branch}^{{commit}}")
        if exists.returncode != 0:
            return 0
        diff = _git(Path(clone), "diff", "--name-only", "-z", f"HEAD...refs/heads/{branch}")
    except (OSError, subprocess.SubprocessError):
        return None
    if diff.returncode != 0:
        return None
    return len([p for p in diff.stdout.split("\0") if p])


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def restore(cfg: Config, slug: str) -> dict:
    """Bring one archived note back, committed directly like a save (it is a human's call)."""
    from memd.verify import apply_changes
    clone = Path(cfg.clone)
    note = next((n for n in list_notes(clone) if n.slug == slug), None)
    if note is None:
        return {"ok": False, "slug": slug, "restored": False, "err": f"no note with slug {slug!r}"}
    if not is_archived(note):
        waiting = pending_proposal(clone, note)
        err = f"{slug} is not archived"
        if waiting:
            err += (f"; it is only proposed on {branch_name()}. Leave that branch unmerged, or pin the "
                    "note or raise its importance so the next mem-forget run drops it.")
        return {"ok": False, "slug": slug, "restored": False, "err": err}
    after = dataclasses.replace(_unarchived(note), saved_by=GENERATOR)
    out = apply_changes(cfg, [(note, after)], message=f"memd: restore {slug} from the archive",
                        generator=GENERATOR, changed="note changed while restoring; run it again")
    restored = slug in out["applied"]
    return {"ok": restored, "slug": slug, "restored": restored, "commit": out["commit"],
            "skipped": out["skipped"], "warnings": out.get("warnings", [])}


def pending_proposal(clone: Path, note: Note) -> bool:
    from memd.summarize import pending_text
    waiting = pending_text(clone, branch_name(), _rel(clone, note.path))
    return waiting is not None and is_archived(parse_text(waiting, path=_rel(clone, note.path)))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_report(report: dict, *, show_text: bool) -> None:
    for c in report["candidates"]:
        flag = " (deferred: over --max)" if c["deferred"] else " (unchanged proposal kept)" if c["reused"] else ""
        print(f"archive  {c['slug']}  importance {c['importance']}, last activity "
              f"{c['last_activity']} ({c['basis']}){flag}")
        for reason in c["reasons"]:
            print(f"    - {reason}")
    for p in report["protected"]:
        print(f"keep     {p['slug']}  {p['reason']} (despite: {'; '.join(p['signals'])})")
    for name, ev in report["evidence"].items():
        if not ev.get("available"):
            print(f"note: no {name} signal: {ev.get('reason')}")
    if show_text:
        for path, text in report["_texts"].items():
            print(f"\n===== {path} =====\n{text}", end="")
    if report["dry_run"]:
        print(f"\ndry-run: {report['proposed']} note(s) would be proposed for archiving; nothing written.")
    elif report["commit"]:
        print(f"\nproposed archiving {report['proposed']} note(s) on branch {report['branch']} "
              f"({report['commit'][:12]}). Review, then merge it into the store's branch to approve; "
              "`mem-forget restore <slug>` brings one back later.")
    else:
        print("\nnothing to propose.")


def _restore_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="mem-forget restore",
                                 description="Bring one archived note back into recall (commits directly).")
    ap.add_argument("slug")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args(argv)
    cfg = Config.from_env()
    if cfg.clone is None:
        print("mem-forget: no clone configured (MEMD_CLONE / MEMD_PROFILE)", file=sys.stderr)
        return 2
    out = restore(cfg, args.slug)
    if args.json:
        print(json.dumps(out))
    elif out["restored"]:
        print(f"restored {args.slug} ({(out['commit'] or '')[:12]}); recall finds it again.")
    else:
        reason = out.get("err") or "; ".join(s["reason"] for s in out.get("skipped", []))
        print(f"mem-forget: {reason}", file=sys.stderr)
    return 0 if out["restored"] else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "restore":
        return _restore_main(argv[1:])
    ap = argparse.ArgumentParser(
        prog="mem-forget",
        description="Propose archiving old, unused, low-importance notes on a review branch "
                    "(never deletes; merging approves). `mem-forget restore SLUG` brings one back.")
    ap.add_argument("--dry-run", action="store_true", help="print the candidates and proposals; write nothing")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    ap.add_argument("--days", type=int, default=None,
                    help=f"minimum days since a note's last activity (default MEMD_FORGET_DAYS or {DEFAULT_DAYS})")
    ap.add_argument("--max-importance", type=int, default=None, choices=range(1, IMPORTANCE_CEILING + 1),
                    help=f"highest importance archived (default MEMD_FORGET_MAX_IMPORTANCE or {DEFAULT_MAX_IMPORTANCE})")
    ap.add_argument("--max", type=int, default=DEFAULT_MAX, help=f"proposals per run (default {DEFAULT_MAX})")
    ap.add_argument("--branch", default=None, help=f"review branch (default {DEFAULT_BRANCH})")
    ap.add_argument("--push", action="store_true", help="push the review branch to origin")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    if cfg.clone is None:
        print("mem-forget: no clone configured (MEMD_CLONE / MEMD_PROFILE)", file=sys.stderr)
        return 2
    report = run(cfg, dry_run=args.dry_run, days=args.days, max_importance=args.max_importance,
                 max_proposals=args.max, branch=args.branch)
    if report["commit"] and args.push:
        from memd.summarize import push_branch
        push_branch(Path(cfg.clone), report["branch"])
    if args.json:
        print(json.dumps({k: v for k, v in report.items() if k != "_texts"}))
    else:
        _print_report(report, show_text=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
