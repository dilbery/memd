"""Durable Git saves with explicit identities and advisory similarity checks.

The store decides identities and revisions. Only an explicit ``supersedes`` or
legacy ``conflict=true`` retires a note. Writes and keyword indexing continue
while embeddings are unavailable; receipts report remote sync and index status
separately. ``save_lint`` remains the maintenance tools' I/O-free helper.
"""
from __future__ import annotations

import dataclasses
import hashlib
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

import sqlite_vec

from memd.actor import get_actor
from memd.config import Config, local_host
from memd.dedup import best_match, build_bm25
from memd.embed import embed_with_deadline
from memd.ground import ground, local_host_checker
from memd.index import open_db
from memd.profiles import guard_paths
from memd.normalize import normalize_fact
from memd.slug import slugify, validate_slug
from memd.store import (Note, read_note, list_notes, dump_note, clone_lock,
                        git_head_sha, assert_readable_tree, StoreUnavailable)

# NOTE: the old magnitude-only STRONG_MATCH_BM25 = -2.0 probe was removed in
# fix-group 4b. Both write paths (this service save() and the carve/sweep
# save_lint) now dedup through dedup.best_match (STRONG_THRESHOLD -3.0 + the
# >=0.5 token-overlap backstop), so there is a SINGLE dedup threshold.
_BACKTICK_CMD = re.compile(r"`([a-zA-Z0-9_.\-/]+)")
_PATH_RE = re.compile(r"(/[\w.\-/]+)")
_LOCALHOST = re.compile(r"127\.0\.0\.1:(\d+)")


def note_filename(slug: str) -> str:
    """The on-disk filename a BRAND-NEW note's slug maps to (single source).

    The live corpus is underscore-named (e.g. ``project_foo_bar.md``), and
    ``save_lint`` already emits that convention. Every producer of a NEW note's
    filename (``save_lint``, ``_write_note``, ``_set_superseded``) MUST go through
    this one helper so a single note can never fragment into a hyphen file AND an
    underscore file. (Existing notes are rewritten at their REAL ``.path``, never
    re-derived from the slug — see ``_write_note``.)
    """
    name = slug.replace('-', '_')
    if len(name) > 220:
        name = f"{name[:180]}_{hashlib.sha256(slug.encode()).hexdigest()[:12]}"
    return f"{name}.md"


# Last post-commit index failure, for `mem doctor` / operator diagnosis. Set by
# _commit_and_push when a durable save could not be indexed.
LAST_INDEX_ERROR: str | None = None


@dataclass
class SaveResult:
    """Result of a save.

    Carries the union of the two call shapes that share this module:

    * ``save_lint`` populates ``note`` / ``action`` (new|upsert) /
      ``upsert_target`` / ``flagged_for_review``.
    * the service ``save`` populates ``slug`` / ``action``
      (created|updated|superseded) / ``grounding`` / ``flagged_for_review`` /
      ``path``.

    Every field defaults so each producer fills only its own subset.
    """

    slug: str = ""
    action: str = ""               # new|upsert (lint) | created|updated|superseded (save)
    flagged_for_review: bool = False
    grounding: str = ""
    path: str = ""
    note: Note | None = None
    upsert_target: str | None = None
    # Semantic index completeness; lexical_indexed separately reports immediate
    # keyword availability. saved means committed locally, synced means published.
    indexed: bool = True
    saved: bool = False
    synced: bool = False
    lexical_indexed: bool = False
    revision: str = ""
    related: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Advisory (memd.conflicts): existing notes this save appears to contradict
    # or update, each with the explicit supersede call. Never acted on here.
    conflicts: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize for the MCP/hook/server JSON surfaces (single path)."""
        return dataclasses.asdict(self)


class RevisionConflict(ValueError):
    """The requested update was based on a note revision that is no longer current."""


@dataclass
class _CommitReceipt:
    indexed: bool = False
    synced: bool = False
    lexical_indexed: bool = False
    warnings: list[str] = field(default_factory=list)


class _CommitFailure(RuntimeError):
    """Internal marker: file changes may be rolled back because commit failed."""


class CommitOutcomeUnknown(RuntimeError):
    """Git's outcome could not be verified; preserve files for recovery."""


# ---------------------------------------------------------------------------
# Lint helper (I/O-free): used by carve/sweep. Unchanged behavior.
# ---------------------------------------------------------------------------


def save_lint(fact: dict, existing: list, *, host_checker=local_host_checker) -> SaveResult:
    title = fact["title"]
    slug = slugify(title)
    note = Note(
        title=title,
        slug=slug,
        path=fact.get("path", note_filename(slug)),
        body=fact.get("body", ""),
        profile=fact.get("profile", "amber"),
        host=fact.get("host") or local_host(),  # write-path default = local host
        importance=int(fact.get("importance", 3)),
        tags=list(fact.get("tags", [])),
    )

    # dedup / upsert decision (slug collision OR strong BM25 near-match)
    target = None
    by_slug = {n.slug: n for n in existing}
    if note.slug in by_slug:
        target = note.slug
    elif existing:
        idx = build_bm25(existing)
        m = best_match(idx, existing, note)
        idx.close()
        if m is not None:
            target = m.slug

    # host-aware ADVISORY grounding (always sets a value, never raises/blocks)
    note.grounding = ground(note, host_checker=host_checker)

    return SaveResult(
        note=note,
        slug=note.slug,
        action="upsert" if target else "new",
        upsert_target=target,
        grounding=note.grounding,
        path=note.path,
        flagged_for_review=(note.grounding == "unverified-local"),
    )


# ---------------------------------------------------------------------------
# Service write path: Git identity, advisory similarity, explicit correction.
# ---------------------------------------------------------------------------


def _bm25_strong_match(cfg: Config, title: str, body: str) -> str | None:
    """Strongest dedup near-match for an incoming fact, or None.

    Unified on `dedup.best_match` (STRONG_THRESHOLD -3.0 with the >=0.5
    token-overlap backstop) so the service write path agrees with the
    `save_lint`/carve write path instead of running a SECOND, looser
    magnitude-only bm25 probe that silently missed near-dupes on a small corpus.
    """
    try:
        # Git is authoritative even while embeddings or the SQLite cache are down.
        existing = [note for note in list_notes(cfg.clone) if not note.superseded_by]
        if not existing:
            return None
        cand = Note(slug="", path="", title=title, body=body)
        idx = build_bm25(existing)
        try:
            m = best_match(idx, existing, cand)
        finally:
            idx.close()
        return m.slug if m is not None else None
    except Exception:
        return None


RELATED_COSINE = 0.93     # body cosines from small embedding models are compressed: a typical note's nearest neighbour is 0.89
NEAR_DUP_COSINE = 0.965   # measured on a sample corpus: >=0.97 pairs are restatements, 0.93-0.95 successive updates on one topic
VECTOR_PROBE_K = 5
# Save is not latency-critical, and a full note body takes ~1.5 s to embed on
# apphost's CPU; recall's 800 ms query deadline would skip most real notes.
VECTOR_PROBE_MS = 3000

def _vector_near_matches(cfg: Config, body: str) -> list[tuple[str, float]]:
    """Nearest indexed notes by body-embedding cosine, strongest first; [] on any failure."""
    try:
        qvec = embed_with_deadline(body, cfg=cfg, ms=VECTOR_PROBE_MS)
        if qvec is None:
            return []

        db = open_db(cfg.db)
        try:
            rows = db.execute(
                "SELECT v.slug, v.embedding FROM vec_notes v JOIN notes n ON n.slug = v.slug "
                "WHERE v.embedding MATCH ? AND k = ? AND n.superseded_by IS NULL "
                "AND n.vector_blob = n.git_blob ORDER BY v.distance",
                (sqlite_vec.serialize_float32(qvec), VECTOR_PROBE_K),
            ).fetchall()
        finally:
            db.close()

        q_norm = math.sqrt(sum(x * x for x in qvec))
        if q_norm == 0:
            return []
        results = []
        for slug, emb_bytes in rows:
            if not emb_bytes:
                continue
            vec = struct.unpack(f"{len(emb_bytes) // 4}f", emb_bytes)
            v_norm = math.sqrt(sum(x * x for x in vec))
            if v_norm == 0:
                continue
            cos = sum(a * b for a, b in zip(qvec, vec)) / (q_norm * v_norm)
            if cos >= RELATED_COSINE:
                results.append((slug, round(cos, 4)))
        results.sort(key=lambda x: x[1], reverse=True)
        return results
    except Exception:
        return []


def _ground_gpuhost(body: str) -> str:
    """Local existence checks for commands/paths/ports. Returns ok|unverified-local."""
    ok = True
    checked = False
    for cmd in _BACKTICK_CMD.findall(body):
        checked = True
        if shutil.which(cmd) is None:
            ok = False
    for path in _PATH_RE.findall(body):
        if path.count("/") >= 2:  # avoid flagging bare "/v1" fragments
            checked = True
            if not os.path.exists(path):
                ok = False
    for port in _LOCALHOST.findall(body):
        checked = True
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            if s.connect_ex(("127.0.0.1", int(port))) != 0:
                ok = False
        finally:
            s.close()
    if not checked:
        return "ok"  # nothing to verify -> not a failure
    return "ok" if ok else "unverified-local"


def _frontmatter(note: dict) -> str:
    """Use the same serializer as maintenance so every writer preserves metadata."""
    fields = {key: value for key, value in note.items()
              if key in Note.__dataclass_fields__}
    fields.update(path="", body="")
    return dump_note(Note(**fields))[:-1]


def _write_note(clone: Path, note: dict, *, path: str | None = None,
                create: bool = False) -> Path:
    """Atomically publish a note, exclusively creating new files.

    Existing paths must remain within clone and cannot contain symlinks. A new
    note uses link(2) rather than replace(2), so a concurrently created file or
    dangling symlink can never be overwritten.
    """
    clone = clone.resolve()
    # The codec (memd.codec) decides the stored bytes: Markdown for a plaintext
    # store, an authenticated envelope for an encrypted one. Resolving it first
    # means a misconfigured encrypted store refuses before anything is written.
    from memd.codec import codec_for
    codec = codec_for(clone)
    target = Path(path) if path else clone / codec.filename(note["slug"])
    replacing = path is not None and not create
    if not target.is_absolute():
        target = clone / target
    if not target.is_relative_to(clone) or not target.resolve().is_relative_to(clone):
        raise ValueError("note path escapes its memory clone")
    relative = target.relative_to(clone)
    if target.is_symlink() or any((clone / parent).is_symlink()
                                  for parent in relative.parents):
        raise ValueError("note paths must not contain symlinks")
    if replacing and not target.is_file():
        raise ValueError("existing note path is not a regular file")
    text = _frontmatter(note) + note["body"].strip() + "\n"
    rel = relative.as_posix()
    name = None
    if replacing and codec.encrypted:
        stored = target.read_bytes()
        if codec.unchanged(rel, stored, text):
            # A fresh nonce would change the blob of identical content; keep the
            # file so re-saving an unchanged fact still commits nothing.
            return target
        name = codec.decode(rel, stored)[1]
    data = codec.encode(rel, text, name=name or note_filename(note["slug"]))
    descriptor, temporary = tempfile.mkstemp(prefix=".memd-note-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            if replacing:
                os.fchmod(output.fileno(), target.stat().st_mode & 0o777)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if replacing:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def _new_note_path(directory: Path, slug: str, *, clone: Path | None = None) -> Path:
    """Allocate a filename independently of a new note's stable identity.

    ``directory`` is where the note goes (an Obsidian store's write folder);
    ``clone`` (default: ``directory``) selects the store's codec, which names
    the file (an opaque name in an encrypted store).
    """
    from memd.codec import codec_for
    return codec_for(clone or directory).new_note_path(directory, slug)


def _restore_bytes(path: Path, data: bytes | None) -> None:
    """Restore a transaction snapshot atomically after a failed write/commit."""
    if data is None:
        path.unlink(missing_ok=True)
        return
    fd, temporary = tempfile.mkstemp(prefix=".memd-rollback-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            if path.exists():
                os.fchmod(output.fileno(), path.stat().st_mode & 0o777)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _set_superseded(clone: Path, old_slug: str, new_slug: str) -> None:
    old = read_note(clone, old_slug)
    if old is None:
        return
    note = dataclasses.asdict(old)
    note["superseded_by"] = new_slug
    # Rewrite the matched note's REAL file in place (its .path), NOT a synthesized
    # <slug>.md — otherwise the original underscore-named note is never marked
    # superseded and a duplicate-slug orphan is created (live-corpus data loss).
    _write_note(clone, note, path=old.path)


def _commit(clone: Path, message: str, *, paths: list[Path]) -> bool:
    """Stage and commit. Returns False when there was nothing to commit.

    The update branch is byte-deterministic — it rebuilds the note from the
    existing frontmatter plus the incoming body, and `_frontmatter` is a
    fixed-order `yaml.safe_dump` — so re-saving an unchanged fact writes identical
    bytes, stages nothing, and `git commit` exits 1 ("nothing to commit"). With
    `check=True` that raised `CalledProcessError` out of `save()` and, with no
    handler on the route, became an HTTP 500. Re-asserting a known fact is the
    NORMAL path for a tool whose contract is "dedups/upserts by slug", and the
    retry could never converge because it hit the same crash.
    """
    relative = [str(path.relative_to(clone)) for path in paths]
    # An encrypted store's Git host sees every message: keep it generic there.
    from memd.codec import codec_for
    message = codec_for(clone).commit_message(message)
    subprocess.run(["git", "-C", str(clone), "add", "--", *relative], check=True,
                   capture_output=True, text=True, timeout=10)
    staged = subprocess.run(["git", "-C", str(clone), "diff", "--cached", "--quiet", "--", *relative],
                            capture_output=True, text=True, timeout=10)
    if staged.returncode == 0:
        return False  # nothing staged: identical bytes, so nothing to record
    if staged.returncode != 1:
        staged.check_returncode()
    subprocess.run(["git", "-C", str(clone), "commit", "--only", "-q", "-m", message,
                    "--", *relative], check=True, capture_output=True, text=True, timeout=10)
    return True


def _has_upstream(clone: Path, env: dict) -> bool:
    """True when the current branch tracks a remote branch."""
    return subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "--verify",
         "-q", "@{upstream}"],
        capture_output=True, text=True, timeout=15, env=env,
    ).returncode == 0


def _pull_rebase_push(clone: Path) -> None:  # pragma: no cover (network)
    """Publish the clone, integrating any remote commits first.

    Stores administered through memd.sources carry their own remote and
    credentials and are published through that module.

    THE FIRST PUSH IS A SEPARATE CASE, and missing it meant a new user's memory
    never left the host. A freshly provisioned store has an empty remote and no
    upstream, so `git pull --rebase` exits non-zero with "There is no tracking
    information for the current branch" and the push below never ran. Every
    save reported `synced: false`; nothing failed loudly.

    So: rebase only when there IS an upstream, and set one on the first push.
    The rebase is NOT skipped once linked, because correcting a note directly
    in the remote is a supported workflow and a blind push would drop it.
    """
    from memd.sources import settings_for_clone, git
    managed = settings_for_clone(clone)
    if managed:
        if managed["config"].get("repo_ssh"):
            git(managed["config"], "remote", "set-url", "origin", managed["config"]["repo_ssh"], clone=clone)
            git(managed["config"], "pull", "--rebase", "--autostash", clone=clone)
            git(managed["config"], "push", clone=clone)
        return
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if _has_upstream(clone, env):
        subprocess.run(["git", "-C", str(clone), "pull", "--rebase", "--autostash"],
                       check=True, capture_output=True, text=True, timeout=15, env=env)
        subprocess.run(["git", "-C", str(clone), "push"],
                       check=True, capture_output=True, text=True, timeout=15, env=env)
        return

    # First publication: -u links the branch so every later save takes the
    # rebase path above. Failure still raises, so save() reports synced=false
    # rather than claiming a sync that did not happen.
    branch = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True, capture_output=True, text=True, timeout=15, env=env,
    ).stdout.strip() or "main"
    subprocess.run(["git", "-C", str(clone), "push", "-u", "origin", branch],
                   check=True, capture_output=True, text=True, timeout=30, env=env)


def _short_rev(clone: Path) -> str:
    return git_head_sha(clone)[:7] or "initial"


def _commit_and_push(cfg: Config, message: str, *, paths: list[Path]) -> _CommitReceipt:
    """Commit durably, report sync errors, and refresh keyword search without AI.

    The returned receipt separates the durable Git write, remote publication,
    keyword availability and semantic indexing. An embedding outage never fails
    or delays a save. A failed pull/rebase is aborted to preserve the committed
    local note and leave subsequent saves usable.
    """
    global LAST_INDEX_ERROR
    before_head = git_head_sha(cfg.clone)
    receipt = _CommitReceipt()
    try:
        _commit(cfg.clone, message, paths=paths)
    except Exception as error:
        after_head = git_head_sha(cfg.clone)
        if before_head and after_head == before_head:
            raise _CommitFailure(str(error)) from error
        # A post-commit hook can time out AFTER Git has committed the note. Never
        # restore old files/index over that durable commit. Verify both HEAD and
        # the committed target bytes before returning a successful receipt.
        verified = False
        if after_head and after_head != before_head:
            try:
                relative = [str(path.relative_to(cfg.clone)) for path in paths]
                tree = subprocess.run(
                    ["git", "-C", str(cfg.clone), "ls-tree", "-r", "--name-only", "-z",
                     after_head, "--", *relative],
                    check=True, capture_output=True, timeout=5,
                )
                outcome = subprocess.run(
                    ["git", "-C", str(cfg.clone), "diff", "--quiet", after_head, "--",
                     *relative],
                    capture_output=True, timeout=5,
                )
                committed_paths = set(tree.stdout.split(b"\0"))
                verified = (outcome.returncode == 0
                            and all(os.fsencode(path) in committed_paths for path in relative))
            except (subprocess.SubprocessError, OSError):
                pass
        if not verified:
            raise CommitOutcomeUnknown(
                "Git commit outcome is uncertain; files were preserved. Inspect the note and Git status "
                "before retrying the save."
            ) from error
        receipt.warnings.append(f"Saved commit verified despite Git command failure: {error}")
    try:
        _pull_rebase_push(cfg.clone)
        receipt.synced = True
    except Exception as error:
        receipt.warnings.append(f"Saved locally; remote sync pending: {error}")
        for state in ("rebase-merge", "rebase-apply"):
            if (cfg.clone / ".git" / state).exists():
                try:
                    subprocess.run(["git", "-C", str(cfg.clone), "rebase", "--abort"],
                                   check=True, capture_output=True, text=True, timeout=5)
                except (subprocess.SubprocessError, OSError) as abort_error:
                    receipt.warnings.append(f"Git rebase needs recovery: {abort_error}")
                    return receipt  # do not index conflict markers
                break
    try:
        assert_readable_tree(cfg.clone)
        from memd.index import pending_vectors, refresh_lexical
        db = open_db(cfg.db, dim=cfg.embed_dim)
        try:
            refresh_lexical(db, cfg)
            receipt.lexical_indexed = True
            receipt.indexed = pending_vectors(db) == 0
        finally:
            db.close()
        LAST_INDEX_ERROR = None
    except Exception as error:
        LAST_INDEX_ERROR = f"{type(error).__name__}: {error}"
        receipt.warnings.append(f"Saved; keyword index pending: {LAST_INDEX_ERROR}")
    if not receipt.indexed:
        try:
            from memd.refresh import request_refresh
            request_refresh(cfg)
        except Exception as error:
            receipt.warnings.append(f"Semantic indexing pending: {error}")
    return receipt


def _slug_is_free(clone: Path, slug: str) -> bool:
    """True when `slug` names neither an existing note (by identity) nor a file.

    Checks BOTH the synthesized target filename (via the shared ``note_filename``
    helper so it matches what ``_write_note`` would actually create) AND the
    hyphen ``<slug>.md`` legacy spelling, AND that no existing note resolves to
    this slug — so a supersede never silently clobbers a hand-authored note.
    """
    from memd.codec import codec_for
    if any(os.path.lexists(clone / name) for name in codec_for(clone).taken_names(slug)):
        return False
    return read_note(clone, slug) is None


def _unique_slug(clone: Path, base: str) -> str:
    """A collision-resistant supersede slug for `base` that names NO existing note.

    Starts at ``<base>-<short-rev>`` and, if that slug is already taken (a prior
    supersede landed at the same HEAD, or a hand-authored note squats it),
    appends ``-2``, ``-3``, … until it is free — so a supersede never silently
    overwrites an existing note.
    """
    candidate = f"{base[:160].rstrip('-_')}-{_short_rev(clone)}"
    if _slug_is_free(clone, candidate):
        return candidate
    n = 2
    while not _slug_is_free(clone, f"{candidate}-{n}"):
        n += 1
    return f"{candidate}-{n}"


def save(fact: dict, profile: str = "amber", *, cfg: Config) -> SaveResult:
    from memd import metrics
    try:
        result = _save(fact, profile, cfg=cfg)
    except Exception:
        metrics.inc("memd_saves_total", outcome="error")
        raise
    metrics.record_save(result)
    return result


def _save(fact: dict, profile: str, *, cfg: Config) -> SaveResult:
    from memd.access import authorize
    authorize(profile, write=True)
    # §9.5 hard isolation: derive clone/db from the REQUESTED profile and refuse
    # any cross-profile path BEFORE any open_db/git/commit op. A save bound to
    # one profile must NEVER write into another profile's clone.
    clone, db_path = guard_paths(profile, cfg.clone, cfg.db)
    cfg = dataclasses.replace(cfg, clone=clone, db=db_path, profile=profile)
    # Serialize the decide->write->commit->sync sequence against
    # any concurrent save() or nightly reflect on the SAME clone (fix-group 4a):
    # they share one flock so their git ops never interleave and corrupt the tree.
    from memd import control
    from memd.sources import source_lock, export_note
    import contextlib
    managed = control.store(profile)
    outer = source_lock(profile) if managed and managed["kind"] == "obsidian" else contextlib.nullcontext()
    # The semantic probe is a network call and only advisory: run it BEFORE
    # taking the clone lock (index.py's rule), accepting a moment of staleness.
    try:
        near = _vector_near_matches(cfg, normalize_fact(fact)["body"])
    except Exception:
        near = []   # _save_locked reports a bad payload itself
    advice: dict = {}
    with outer, clone_lock(cfg.clone):
        result = _save_locked(fact, profile, cfg, near=near, advice=advice)
        if managed and managed["config"].get("managed") and not managed["config"].get("repo_ssh"):
            result.synced = False
        if managed and managed["kind"] == "obsidian":
            try:
                warning = export_note(cfg, Path(result.path))
            except Exception as exc:
                warning = "Saved in memd; vault export failed: " + str(exc)
            if warning:
                result.warnings.append(warning)
    # The model check is a network call: after the durable commit and outside
    # the clone lock, under its own hard deadline. It only ever adds advice.
    _model_conflict_check(result, cfg, advice)
    return result


@dataclasses.dataclass
class _Identity:
    """What a save of ``fact`` would do to the store: save and dry_run share it."""
    by_slug: dict
    own: Note | None           # the live or retired note holding the requested slug
    old: Note | None           # the note being retired (supersedes / conflict)
    existing: Note | None      # the note updated in place, or None for a new note
    fresh_slug: bool           # a new note whose slug is taken gets a unique one at write time


def _identity(fact: dict, slug: str, notes: list) -> _Identity:
    """The identity decision; raises ValueError or RevisionConflict as save does."""
    by_slug = {}
    for note in notes:
        if note.slug in by_slug:
            raise ValueError(f"duplicate stored slug {note.slug!r}; resolve its files before saving")
        by_slug[note.slug] = note
    own = by_slug.get(slug)
    from memd.store import RETRACTED
    if own and own.superseded_by == RETRACTED:
        raise ValueError("This memory identity was retracted by its owner and cannot be overwritten")
    target_slug = fact.get("supersedes")
    old = by_slug.get(target_slug) if target_slug else None
    if target_slug and (old is None or old.superseded_by):
        raise ValueError(f"supersedes must name an existing live note: {target_slug}")
    if not target_slug and fact.get("conflict") and own and not own.superseded_by:
        old = own  # backward-compatible, explicitly requested correction
    # An explicit live identity plus supersedes updates that note in place and
    # retires the other one: the call a conflict receipt suggests.
    in_place = old is not None and own is not None and own is not old and bool(fact.get("slug"))
    if in_place and own.superseded_by:
        raise ValueError("replacement slug already belongs to another note")
    # expected_revision guards the note this save overwrites: the in-place one,
    # else the one being retired, else the one being updated.
    revision_target = own if in_place else (old or own)
    expected = fact.get("expected_revision")
    if expected is not None and (revision_target is None or revision_target.git_blob != expected):
        current = revision_target.git_blob if revision_target else "missing"
        raise RevisionConflict(f"note changed: expected revision {expected}, current revision {current}; read it again")
    existing = own if in_place else (own if own and not own.superseded_by and old is None else None)
    return _Identity(by_slug, own, old, existing, fresh_slug=existing is None and slug in by_slug)


def dry_run(fact: dict, profile: str = "amber", *, cfg: Config) -> dict:
    """Save's advisory checks for a fact, without writing anything.

    The same identity decision, related-note, near-duplicate and conflict checks
    a real save would report in its receipt, computed read-only (no clone lock,
    no Git, no index write) so the inbox (memd.inbox) can show a reviewer what
    approving a candidate would do. A fact save would refuse (a retracted
    identity, a ``supersedes`` naming no live note, a stale
    ``expected_revision``) is reported under ``error`` instead of raising;
    only a payload with no content raises (NormalizeError). ``save`` itself is
    unchanged by this function.
    """
    clone, db_path = guard_paths(profile, cfg.clone, cfg.db)
    cfg = dataclasses.replace(cfg, clone=clone, db=db_path, profile=profile)
    fact = normalize_fact(fact)
    title, body = fact["title"], fact["body"]
    slug = fact.get("slug") or slugify(title) or f"note-{hashlib.sha256(title.encode()).hexdigest()[:12]}"
    validate_slug(slug)
    out: dict = {"slug": slug, "action": "create", "related": [], "near_duplicates": [],
                 "conflicts": [], "warnings": [], "error": None}
    assert_readable_tree(cfg.clone)
    notes = list_notes(cfg.clone)
    by_slug = {note.slug: note for note in notes}
    old = None
    try:
        ident = _identity(fact, slug, notes)
        old = ident.old
        from memd.sources import guard_vault_write
        guard_vault_write(cfg, ident.existing)
        guard_vault_write(cfg, ident.old)
    except (ValueError, RevisionConflict, PermissionError) as exc:
        out["error"] = str(exc)
        ident = None
    if ident is not None:
        if old is not None:
            out["action"] = "supersede"
            out["supersedes"] = old.slug
        elif ident.existing is not None:
            out["action"] = "update"
        if ident.fresh_slug:
            # save picks a unique slug at write time; never report the occupied one.
            out["slug"] = None
            out["warnings"].append(f"{slug} is taken, so this is saved under a new unique slug.")
    try:
        near = _vector_near_matches(cfg, body)
    except Exception:
        near = []
    near = [(s, c) for s, c in near if s != slug and (old is None or s != old.slug)]
    related = [s for s, _ in near]
    match = _bm25_strong_match(cfg, title, body)
    if match and match != slug and (old is None or match != old.slug):
        if match not in related:
            related.append(match)
        out["near_duplicates"].append({"slug": match, "method": "keyword"})
    for s, c in near:
        if c >= NEAR_DUP_COSINE:
            out["near_duplicates"] = [d for d in out["near_duplicates"] if d["slug"] != s]
            out["near_duplicates"].append({"slug": s, "method": "vector", "cosine": c})
    if near and near[0][1] >= NEAR_DUP_COSINE:
        out["warnings"].append(
            f"Near-duplicate of {near[0][0]} (cosine {near[0][1]:.2f}): if this restates that fact, "
            f"update it with slug={near[0][0]} instead of adding a new note.")
    out["related"] = related
    result = SaveResult(slug=slug)
    exclude = {slug, fact.get("slug") or "", old.slug if old else ""} - {""}
    new = Note(title=title, slug=slug, path="", body=body,
               observed_at=fact.get("observed_at"), verified_at=fact.get("verified_at"))
    _fact_conflict_check(result, cfg, notes, new, exclude)
    _model_conflict_check(result, cfg, {"new": new, "related": [
        by_slug[s] for s in related if s in by_slug and s not in exclude and not by_slug[s].superseded_by]})
    out["conflicts"] = result.conflicts
    out["warnings"] += result.warnings
    return out


def _add_conflicts(result: SaveResult, found) -> None:
    from memd.conflicts import MAX_CONFLICTS, warning_line
    known = {c["slug"] for c in result.conflicts}
    for conflict in found:
        if conflict.slug in known or len(result.conflicts) >= MAX_CONFLICTS:
            continue
        known.add(conflict.slug)
        result.conflicts.append(conflict.to_dict())
        result.warnings.append(warning_line(conflict))
    if result.conflicts:
        result.flagged_for_review = True


def _fact_conflict_check(result: SaveResult, cfg: Config, notes: list[Note], new: Note,
                         exclude: set[str]) -> None:
    """Deterministic fact comparison (no model); a failure is only a warning."""
    if cfg.conflict_check == "off":
        return
    try:
        from memd.conflicts import fact_conflicts
        from memd.facts import note_date
        d = note_date(new)
        _add_conflicts(result, fact_conflicts(new.body, d.isoformat() if d else None, notes,
                                              slug=new.slug, exclude=exclude, db_path=cfg.db))
    except Exception as error:
        result.warnings.append(f"Conflict check skipped: {type(error).__name__}: {error}")


def _model_conflict_check(result: SaveResult, cfg: Config, advice: dict) -> None:
    """Optional chat-model verdicts on the top related notes; never raises."""
    try:
        from memd.conflicts import model_conflicts, model_enabled
        if not advice or not model_enabled(cfg):
            return
        known = {c["slug"] for c in result.conflicts}
        others = [n for n in advice["related"] if n.slug not in known]
        found, skipped = model_conflicts(advice["new"], others, cfg=cfg)
        if skipped:
            result.warnings.append(f"Conflict check skipped: {skipped}.")
        _add_conflicts(result, found)
    except Exception as error:
        result.warnings.append(f"Conflict check skipped: {type(error).__name__}: {error}")


def _set_verify(note: dict, fact: dict) -> None:
    """Carry a declared `verify` list into the note's frontmatter (memd.verify).

    Changing the probes drops mem-verify's failed marker, which described the
    old ones; an empty list removes the declaration.
    """
    if "verify" not in fact:
        return
    metadata = dict(note.get("metadata") or {})
    if metadata.get("verify") != fact["verify"]:
        metadata.pop("verification", None)
    if fact["verify"]:
        metadata["verify"] = fact["verify"]
    else:
        metadata.pop("verify", None)
    note["metadata"] = metadata


def _unarchive(note: dict) -> None:
    """Saving to an archived note (memd.forget) brings it back: it is current again."""
    from memd.store import ARCHIVE_KEYS
    metadata = dict(note.get("metadata") or {})
    if any(key in metadata for key in ARCHIVE_KEYS):
        for key in ARCHIVE_KEYS:
            metadata.pop(key, None)
        note["metadata"] = metadata


# Provenance of a note copied from another store by memd.share.publish. A
# context variable, never a fact field: normalize_fact drops unknown keys, so no
# save or propose payload can forge `published_from`; only the publish path (and
# an inbox approval of a candidate that path filed) sets it.
_published_from: ContextVar[dict | None] = ContextVar("memd_published_from", default=None)


@contextmanager
def publishing(provenance: dict | None):
    """Stamp `published_from: provenance` on the note saved inside this block."""
    marker = _published_from.set(dict(provenance) if provenance else None)
    try:
        yield
    finally:
        _published_from.reset(marker)


def _set_provenance(note: dict) -> None:
    provenance = _published_from.get()
    if not provenance:
        return
    metadata = dict(note.get("metadata") or {})
    metadata["published_from"] = dict(provenance)
    note["metadata"] = metadata


def _save_locked(fact: dict, profile: str, cfg: Config, *,
                 near: list[tuple[str, float]] | None = None,
                 advice: dict | None = None) -> SaveResult:
    assert_readable_tree(cfg.clone)
    fact = normalize_fact(fact)
    title, body = fact["title"], fact["body"]
    slug = fact.get("slug") or slugify(title) or f"note-{hashlib.sha256(title.encode()).hexdigest()[:12]}"
    validate_slug(slug)

    # All identity decisions read the durable store, never a stale vector cache.
    notes = list_notes(cfg.clone)
    ident = _identity(fact, slug, notes)
    by_slug, own, old, existing = ident.by_slug, ident.own, ident.old, ident.existing
    from memd.sources import guard_vault_write, write_directory
    guard_vault_write(cfg, existing)
    guard_vault_write(cfg, old)
    host = fact.get("host", existing.host if existing else local_host())
    if existing and body == existing.body and host == existing.host:
        grounding = existing.grounding
    elif host != "any" and host == local_host():
        try:
            grounding = _ground_gpuhost(body)
        except Exception:
            grounding = "unverified-local"  # grounding is advisory
    else:
        grounding = "unverified-remote"

    # Similar vocabulary or meaning is only a suggestion. It never retires a
    # distinct fact; a near-duplicate earns a warning naming the note to update.
    match = _bm25_strong_match(cfg, title, body)
    near = [(s, c) for s, c in near or []
            if s != slug and (old is None or s != old.slug)]
    related = [s for s, _ in near]
    if match and match != slug and (old is None or match != old.slug) and match not in related:
        related.append(match)
    extra_warnings: list[str] = []
    if near and near[0][1] >= NEAR_DUP_COSINE:
        extra_warnings.append(
            f"Near-duplicate of {near[0][0]} (cosine {near[0][1]:.2f}): if this restates that fact, "
            f"update it with slug={near[0][0]} instead of adding a new note.")
    snapshots: dict[Path, bytes | None] = {}
    index_path = cfg.clone / ".git" / "index"
    index_before = index_path.read_bytes() if index_path.exists() else None
    try:
        if existing:
            note = dataclasses.asdict(existing)
            note.update(body=body, profile=profile, host=host, grounding=grounding)
            # Re-stamp with the current caller; keep the previous one when unattributed.
            note["saved_by"] = get_actor() or existing.saved_by
            # An explicit identity allows a display-title change. A slug-only caller
            # gets the existing title, retaining compatibility with loose payloads.
            if fact.get("slug") and title != slug:
                note["title"] = title
            for key in ("importance", "tags", "description", "source", "observed_at",
                        "verified_at", "pinned", "volatility"):
                if key in fact:
                    note[key] = fact[key]
            _set_verify(note, fact)
            _set_provenance(note)
            _unarchive(note)
            path = Path(existing.path)
            snapshots[path] = path.read_bytes()
            path = _write_note(cfg.clone, note, path=existing.path)
            action = "updated"
            if old:
                snapshots[Path(old.path)] = Path(old.path).read_bytes()
                _set_superseded(cfg.clone, old.slug, slug)
                action = "superseded"
        else:
            # Only an occupied identity requires a new slug. A filename-only
            # collision receives a separate path, so title-only retries converge.
            if slug in by_slug:
                slug = _unique_slug(cfg.clone, slug)
            note = {
                "title": title, "slug": slug, "profile": profile, "host": host,
                "importance": fact.get("importance", 3), "superseded_by": None,
                "tags": fact.get("tags", []), "grounding": grounding, "body": body,
                "saved_by": get_actor(),
            }
            for key in ("description", "source", "observed_at", "verified_at", "pinned",
                        "volatility"):
                if key in fact:
                    note[key] = fact[key]
            _set_verify(note, fact)
            _set_provenance(note)
            path = _new_note_path(write_directory(cfg), slug, clone=cfg.clone)
            snapshots[path] = None
            path = _write_note(cfg.clone, note, path=str(path), create=True)
            action = "created"
            if old:
                snapshots[Path(old.path)] = Path(old.path).read_bytes()
                _set_superseded(cfg.clone, old.slug, slug)
                action = "superseded"
    except Exception as error:
        for target, data in snapshots.items():
            # Exclusive creation may fail because an unrelated file won the race.
            if data is None and isinstance(error, FileExistsError):
                continue
            _restore_bytes(target, data)
        raise
    try:
        message = f"memd: {action} {slug}"
        actor_label = get_actor()
        if actor_label:
            message += f"\n\nSaved-By: {actor_label}"
        receipt = _commit_and_push(cfg, message, paths=list(snapshots))
    except _CommitFailure as error:
        for target, data in snapshots.items():
            _restore_bytes(target, data)
        _restore_bytes(index_path, index_before)
        raise error.__cause__ from error
    try:
        assert_readable_tree(cfg.clone)
        revision = subprocess.run(
            ["git", "-C", str(cfg.clone), "hash-object", str(path)], check=True,
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except (StoreUnavailable, subprocess.SubprocessError, OSError) as error:
        revision = ""
        receipt.warnings.append(f"Saved; revision lookup unavailable: {error}")
    result = SaveResult(
        slug=slug, action=action, grounding=grounding,
        flagged_for_review=bool(related) or old is not None or grounding == "unverified-local",
        path=str(path), saved=True, synced=receipt.synced,
        indexed=receipt.indexed, lexical_indexed=receipt.lexical_indexed,
        revision=revision, related=related,
        warnings=receipt.warnings + extra_warnings,
    )
    # Advisory only, after the durable commit: the note being written, the one
    # it replaced and the one it explicitly supersedes are never reported.
    exclude = {slug, fact.get("slug") or "", old.slug if old else ""} - {""}
    new = Note(title=note["title"], slug=slug, path=str(path), body=body,
               observed_at=note.get("observed_at"), verified_at=note.get("verified_at"))
    _fact_conflict_check(result, cfg, notes, new, exclude)
    if advice is not None:
        advice.update(new=new, related=[by_slug[s] for s in related
                                        if s in by_slug and s not in exclude
                                        and not by_slug[s].superseded_by])
    return result
