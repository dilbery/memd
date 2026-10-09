"""Nightly PROPOSE-ONLY self-tidy (design §8).

Builds a dedup + staleness report and a draft of tidied notes, writes the draft
to a FRESH Forgejo branch, opens a PR, and sends a Pushover summary for morning
human review. It NEVER merges and NEVER deletes the source of truth. staleness
is reported, never an auto-delete trigger. A buggy run can at worst open a noisy
PR that a human closes.
"""
from __future__ import annotations

import datetime as dt
import difflib
import os
import subprocess
from pathlib import Path

from memd.store import (
    CARVED_INDEX_FILES,
    clone_lock,
    clone_lock_path,
    parse_note,
)


def _load_notes() -> list[dict]:
    """Read all notes from memd's dedicated clone as {slug,title,body,last_used}.

    Uses ``rglob`` + the shared legacy-tolerant parser (``store.parse_note``) so
    the propose-only tidy sees exactly the recall/save corpus:

      * it ``rglob``s, so notes filed under a SUBDIRECTORY are NOT omitted (the
        old non-recursive ``glob('*.md')`` dropped them — recall/save already
        rglob via ``store.list_notes``);
      * it derives the slug via the shared parser (slugify(title) / H1 / legacy
        ``name``), so the dedup/staleness report keys on the SAME slug recall
        uses. A typical imported underscore-named corpus has no ``slug:`` field, so the old
        ``f.stem`` fallback keyed on the filename instead of the title;
      * it skips the carved index artifacts (MEMORY.md / MEMORY-full.md /
        README.md) via the SHARED ``CARVED_INDEX_FILES`` set, like before.
    """
    clone = Path(os.environ["MEMD_CLONE"]).expanduser()
    # The draft report names notes in plaintext on a pushed branch.
    from memd.codec import refuse_encrypted
    refuse_encrypted(clone, "reflect")
    notes: list[dict] = []
    for f in sorted(clone.rglob("*.md")):
        if f.name in CARVED_INDEX_FILES:
            continue
        n = parse_note(f)
        notes.append({
            "slug": n.slug,
            "title": n.title,
            "body": n.body,
            "last_used": n.last_used or "",
        })
    return notes


def _parse_iso(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


_DUP_RATIO = 0.90


def build_report(notes: list[dict], now: dt.datetime, stale_days: int = 120) -> dict:
    # Duplicate detection is inherently O(n^2) pairs, but it does NOT need an
    # O(len(a)*len(b)) diff per pair. Measured on a realistic corpus (278 notes,
    # 3821-char mean body): 5.41 ms/pair x 38,503 pairs = 208 s of CPU every night,
    # growing quadratically (~45 min at 1000 notes) with no cap and no timeout.
    #
    # Two cheap gates, each a STRICT UPPER BOUND on ratio(), so neither can change
    # which pairs are reported:
    #   1. length ratio       — ratio() <= 2*min/(la+lb); pure arithmetic;
    #   2. real_quick_ratio() — O(1), bounds on lengths only.
    # set_seq2 is hoisted so difflib builds its b2j index once per i rather than
    # once per pair, which was the other half of the cost.
    #
    # quick_ratio() is deliberately NOT used: it is O(n) and builds a Counter, and
    # measured on a realistic length spread it cost more than it pruned
    # (233 ms with it vs 143 ms without, against 2528 ms naive — 17.6x). Both
    # variants returned identical duplicate sets.
    duplicates: list[tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(None)
    for i in range(len(notes)):
        a = notes[i]["body"]
        matcher.set_seq2(a)
        la = len(a)
        for j in range(i + 1, len(notes)):
            b = notes[j]["body"]
            lb = len(b)
            if la + lb == 0:
                continue
            if 2.0 * min(la, lb) / (la + lb) < _DUP_RATIO:
                continue
            matcher.set_seq1(b)
            if matcher.real_quick_ratio() < _DUP_RATIO:
                continue
            if matcher.ratio() >= _DUP_RATIO:
                duplicates.append((notes[i]["slug"], notes[j]["slug"]))

    stale: list[str] = []
    cutoff = now - dt.timedelta(days=stale_days)
    for n in notes:
        ts = _parse_iso(n.get("last_used", ""))
        if ts is not None and ts < cutoff:
            stale.append(n["slug"])

    return {
        "generated": now.isoformat(),
        "n_notes": len(notes),
        "duplicates": duplicates,   # PROPOSED merges — human decides
        "stale": stale,             # REPORTED only — never auto-deleted
    }


def render_draft(report: dict) -> str:
    lines = [
        "# memd reflect — proposed tidy (PROPOSE-ONLY, nothing applied)",
        "",
        f"_generated {report['generated']} over {report['n_notes']} notes_",
        "",
        "## Suspected duplicates (review + merge by hand if correct)",
    ]
    for a, b in report["duplicates"]:
        lines.append(f"- `{a}` <-> `{b}`")
    lines += ["", "## Stale (last_used older than threshold) — review, NOT auto-deleted"]
    for s in report["stale"]:
        lines.append(f"- `{s}`")
    return "\n".join(lines) + "\n"


def _lock_path(clone: Path) -> Path:
    """The shared clone flock file (same lock save() takes). See store.clone_lock_path."""
    return clone_lock_path(clone)


def _default_branch(clone: Path) -> str:
    """The repo's default branch, detected via origin/HEAD; 'main' as fallback.

    Never hardcodes 'main': resolves `refs/remotes/origin/HEAD` (e.g.
    `refs/remotes/origin/trunk` -> `trunk`). Off-remote or unresolved, falls
    back to the literal default so a detached/local clone still works.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(clone), "symbolic-ref", "refs/remotes/origin/HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if out:
            return out.rsplit("/", 1)[-1]
    except subprocess.CalledProcessError:
        pass
    return "main"


def _current_branch(clone: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _clone_lock(clone: Path):
    """The shared clone flock (same lock save() takes). See store.clone_lock."""
    return clone_lock(clone)


def make_branch(report: dict, draft: str) -> str:
    """Commit the draft report to a FRESH branch in memd's clone, then RESTORE the
    repo to its default branch — under an flock so a concurrent save() cannot
    interleave. No merge, no force, no delete of any existing note.

    Hardening (fix-group 4a):
      * flock the whole git sequence (serialize vs save()).
      * detect the default branch via origin/HEAD (never hardcoded 'main').
      * restore the ORIGINAL branch in a finally even if push fails, so a failed
        run never strands the shared clone on the reflect/ branch.
    """
    clone = Path(os.environ["MEMD_CLONE"]).expanduser()
    from memd.codec import refuse_encrypted
    refuse_encrypted(clone, "reflect")
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    branch = f"reflect/{today}"

    with _clone_lock(clone):
        original = _current_branch(clone)
        restore_to = _default_branch(clone)
        try:
            subprocess.run(["git", "-C", str(clone), "checkout", "-B", branch], check=True, capture_output=True)
            (clone / "REFLECT-REPORT.md").write_text(draft, encoding="utf-8")
            subprocess.run(["git", "-C", str(clone), "add", "REFLECT-REPORT.md"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(clone), "commit", "-m", f"reflect: proposed tidy {today}"],
                check=True, capture_output=True,
            )
            subprocess.run(["git", "-C", str(clone), "push", "--force", "-u", "origin", branch], check=True, capture_output=True)
        finally:
            # Always leave the shared clone on a stable branch: prefer the
            # detected default; fall back to the branch we started on. HEAD must
            # never be stranded on reflect/<date> for the next save().
            for target in (restore_to, original):
                if not target or target == branch:
                    continue
                r = subprocess.run(
                    ["git", "-C", str(clone), "checkout", target],
                    capture_output=True,
                )
                if r.returncode == 0:
                    break
    return branch


def _forgejo_token() -> str:
    """Forgejo token for opening the tidy PR.

    Prefer env FORGEJO_TOKEN (legacy/explicit), else fall back to the standing
    admin token file ~/.config/forgejo/token. Env tokens may be short-lived and
    rotated; the file is the durable source. Raise if neither yields a value.
    """
    tok = os.environ.get("FORGEJO_TOKEN")
    if tok and tok.strip():
        return tok.strip()
    path = Path("~/.config/forgejo/token").expanduser()
    if path.is_file():
        val = path.read_text().strip()
        if val:
            return val
    raise RuntimeError("no Forgejo token: set FORGEJO_TOKEN or ~/.config/forgejo/token")


def _pr_url_for_branch(pulls: list, branch: str) -> str | None:
    """Return the html_url of the PR whose head ref == branch, else None."""
    for p in pulls:
        if p.get("head", {}).get("ref") == branch:
            return p.get("html_url")
    return None


def open_pr(branch: str, report: dict) -> str:
    """Open a Forgejo PR via the API. Returns the PR URL. Does NOT merge it."""
    return open_review_pr(
        branch,
        title=f"reflect: nightly tidy proposal ({report['generated'][:10]})",
        body=f"{len(report['duplicates'])} suspected dup pairs, {len(report['stale'])} stale notes. "
             f"PROPOSE-ONLY — review and merge by hand.",
    )


def open_review_pr(branch: str, *, title: str, body: str) -> str:
    """Open (or find the already-open) Forgejo PR for a proposal branch.

    Shared by reflect and mem-summarize. Returns the PR URL; never merges.
    """
    import json
    import urllib.request

    base = os.environ.get("FORGEJO_API", "https://git.example.com/api/v1")
    repo = os.environ.get("MEMD_REPO_PATH", "svcuser/amber-memory")
    token = _forgejo_token()
    # PR base = the repo's ACTUAL default branch (origin/HEAD), never hardcoded.
    base_branch = _default_branch(Path(os.environ["MEMD_CLONE"]).expanduser())
    payload = json.dumps({
        "head": branch,
        "base": base_branch,
        "title": title,
        "body": body,
    }).encode()
    import urllib.error
    req = urllib.request.Request(
        f"{base}/repos/{repo}/pulls",
        data=payload,
        headers={"Authorization": f"token {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()).get("html_url", "")
    except urllib.error.HTTPError as e:
        if e.code not in (409, 422):
            raise
        # A PR for this branch already exists — fetch open PRs and return it (idempotent).
        list_req = urllib.request.Request(
            f"{base}/repos/{repo}/pulls?state=open&limit=50",
            headers={"Authorization": f"token {token}"},
        )
        with urllib.request.urlopen(list_req, timeout=15) as lr:
            pulls = json.loads(lr.read())
        url = _pr_url_for_branch(pulls, branch)
        if url:
            return url
        raise


def send_pushover(summary: str) -> None:
    """Best-effort morning ping. A missing/failing `pushover` must NEVER fail the
    reflect run — the PR is the real output; the notification is a bonus.
    (check=False suppresses non-zero exits but NOT FileNotFoundError when the
    binary isn't on PATH, so the whole call is wrapped.)"""
    try:
        subprocess.run(["pushover", "-t", "memd reflect", summary],
                       check=False, capture_output=True)
    except Exception:
        pass


def reflect() -> str:
    notes = _load_notes()
    now = dt.datetime.now(dt.timezone.utc)
    report = build_report(notes, now=now)
    draft = render_draft(report)
    branch = make_branch(report, draft)
    url = open_pr(branch, report)
    summary = (
        f"Opened tidy PR: {len(report['duplicates'])} dup pairs, "
        f"{len(report['stale'])} stale. Review: {url}"
    )
    send_pushover(summary)
    return url


def main() -> int:
    print(reflect())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
