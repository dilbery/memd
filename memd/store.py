"""Read/normalize git-markdown notes into the shared v2 Note contract.

Tolerates BOTH the legacy frontmatter (name/description/type/metadata) used by
existing notes AND the v2 schema, so the corpus needs no migration.

Title precedence: explicit v2 `title` > body H1 > legacy `name` > filename stem.
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from memd.staleness import normalize_volatility

from memd.slug import slugify


def _default_profile() -> str:
    """The configured default profile (MEMD_LEGACY_PROFILES), for notes that name none."""
    from memd.config import default_profile
    return default_profile()


_FM = re.compile(r"^---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
_H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)

# The carved index artifacts written by mem-carve. These are NOT notes: the
# recall index must never ingest them, reflect must never dedup them, and the
# degraded grep fallback must never return them. Factored here as the single
# source of truth so store / reflect / degraded_grep agree (fix-group 4e).
CARVED_INDEX_FILES = frozenset({"MEMORY.md", "MEMORY-full.md", "README.md"})
# The files mem-carve writes. They list every note's title and first line, so an
# encrypted store must never hold them (memd.codec refuses; mem-crypt encrypt
# removes them).
CARVE_OUTPUT_FILES = frozenset({"MEMORY.md", "MEMORY-full.md"})

# Reserved retirement target; ':' cannot occur in a normal saved slug.
RETRACTED = "memd:retracted"

# Archiving (memd.forget) is frontmatter, not a move: `archived: <date>` and
# `archived_reason` stay in the note's unknown frontmatter (Note.metadata), so
# the file keeps its path and history and restoring it is removing two keys.
ARCHIVE_KEYS = ("archived", "archived_reason")


def archived_value(metadata) -> str | None:
    """The `archived` date of a note's metadata as a string, or None when not archived."""
    value = metadata.get("archived") if isinstance(metadata, dict) else None
    if value is None or value is False or value == "" or value == 0:
        return None
    return str(value)


def is_archived(note) -> bool:
    """True for a Note (or note dict) that mem-forget archived and a merge approved."""
    metadata = note.get("metadata") if isinstance(note, dict) else getattr(note, "metadata", None)
    return archived_value(metadata) is not None


def not_archived_sql(alias: str = "") -> str:
    """SQL condition on an index `notes` row: not archived (see archived_value)."""
    prefix = f"{alias}." if alias else ""
    return (f"COALESCE(json_extract({prefix}metadata, '$.metadata.archived'), '') "
            "IN ('', 0)")


class StoreUnavailable(RuntimeError):
    """The clone cannot currently provide a coherent note snapshot."""


def assert_readable_tree(clone: Path) -> None:
    """Refuse transient rebases and unresolved merge/autostash conflicts.

    Call under clone_lock when consuming a snapshot. Conflict-marker text must
    never become a memory or be committed by a subsequent save.
    """
    if any((clone / ".git" / state).exists() for state in ("rebase-merge", "rebase-apply")):
        raise StoreUnavailable("memory clone has an unfinished rebase; recover it before reading or saving")
    try:
        result = subprocess.run(
            ["git", "-C", str(clone), "ls-files", "--unmerged", "-z"],
            check=True, capture_output=True, timeout=2,
        )
    except (subprocess.SubprocessError, OSError) as error:
        raise StoreUnavailable(f"memory clone is unavailable: {error}") from error
    if result.stdout:
        raise StoreUnavailable("memory clone has unresolved merge or autostash conflicts; recover it before reading or saving")


def clone_lock_path(clone: Path) -> Path:
    """The flock file that serializes ALL git mutation on a shared clone.

    Both the save() write path and the nightly reflect take this lock, so their
    `git checkout`/`commit`/`push` sequences never interleave on the same working
    tree (a reflect racing a save previously corrupted the clone). Kept inside
    `.git/` so it is never tracked or pushed.
    """
    return Path(clone) / ".git" / "memd-reflect.lock"


@contextlib.contextmanager
def clone_lock(clone: Path, *, blocking: bool = True):
    """Exclusive flock over a clone for one git mutation sequence (save/reflect)."""
    lock_path = clone_lock_path(clone)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()


class _StrTimestampLoader(yaml.SafeLoader):
    """SafeLoader that leaves ISO timestamps as plain strings.

    PyYAML's default implicit resolver turns `last_used: 2026-06-16T09:14:00Z`
    into a datetime, which would not round-trip and breaks the string-valued
    v2 `last_used` contract. Dropping that one resolver keeps it the raw string.
    """


_StrTimestampLoader.yaml_implicit_resolvers = {
    k: [(tag, regexp) for tag, regexp in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


@dataclass
class Note:
    title: str
    slug: str
    path: str
    body: str
    profile: str = field(default_factory=lambda: _default_profile())
    host: str = "any"
    importance: int = 3
    last_used: str | None = None
    superseded_by: str | None = None
    tags: list[str] = field(default_factory=list)
    grounding: str = "ok"
    description: str = ""
    git_blob: str = ""
    pinned: bool = False
    source: str = ""
    observed_at: str | None = None
    verified_at: str | None = None
    volatility: str | None = None   # durable | state | volatile (memd.staleness)
    # The authenticated caller that last wrote this note (memd.actor). A client
    # label, not a person, until Plan 3 puts an authenticated email here.
    saved_by: str = ""
    # Unknown frontmatter survives updates and maintenance round-trips.
    metadata: dict = field(default_factory=dict)
    # Render provenance, set by recall(): True = returned because it matched the
    # query (render full body), False = part of the always-on core index (stub).
    # Not persisted -- no write path goes through to_dict(), so this never
    # reaches a note's front-matter.
    matched: bool = False

    def to_dict(self) -> dict:
        """Serialize for the MCP/hook/server JSON surfaces (single path)."""
        return {**dataclasses.asdict(self), "revision": self.revision}

    @property
    def revision(self) -> str:
        """Content revision accepted by save(expected_revision=...)."""
        return self.git_blob


def _loose_frontmatter(fm_text: str) -> dict:
    """Tolerant fallback for legacy frontmatter with unquoted colons in values.

    PyYAML rejects ``description: foo: bar`` — the unquoted colon-space parses as
    a nested mapping (some real-world notes are like this). We only need the flat
    top-level scalar fields, so read them line-by-line, split on the first
    colon, and skip indented continuation lines (e.g. the ``metadata:`` block).
    """
    out: dict = {}
    for line in fm_text.splitlines():
        if not line or line[0].isspace():
            continue  # blank, or indented nested-block line — ignore
        m = re.match(r"^([A-Za-z_][\w-]*):\s?(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]  # strip matching surrounding quotes
        out[key] = val
    return out


def _split(text: str) -> tuple[dict, str]:
    m = _FM.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    try:
        fm = yaml.load(raw, Loader=_StrTimestampLoader) or {}
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError:
        fm = _loose_frontmatter(raw)  # legacy note with unquoted colon in a value
    return fm, m.group(2)


def parse_text(text: str, *, path: str) -> Note:
    fm, body = _split(text)
    stem = Path(path).stem

    # title: explicit v2 title > body H1 > legacy `name` > filename stem
    title = fm.get("title")
    if not title:
        h1 = _H1.search(body)
        if h1:
            title = h1.group(1).strip()
        else:
            title = fm.get("name") or stem.replace("_", " ")

    slug = fm.get("slug") or slugify(str(title))

    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    sb = fm.get("superseded_by")
    if sb in ("null", "", None):
        sb = None

    known = {"title", "slug", "profile", "host", "importance", "last_used",
             "superseded_by", "tags", "grounding", "description", "pinned",
             "source", "observed_at", "verified_at", "volatility", "saved_by"}
    return Note(
        title=str(title),
        slug=slug,
        path=path,
        body=body.strip(),
        profile=str(fm.get("profile") or _default_profile()),
        host=str(fm.get("host", "any")),
        importance=int(fm.get("importance", 3)),
        last_used=fm.get("last_used"),
        superseded_by=sb,
        tags=list(tags),
        grounding=str(fm.get("grounding", "ok")),
        description=str(fm.get("description", "")),
        pinned=fm.get("pinned") is True,
        source=str(fm.get("source") or ""),
        observed_at=fm.get("observed_at"),
        verified_at=fm.get("verified_at"),
        volatility=normalize_volatility(fm.get("volatility")),
        saved_by=str(fm.get("saved_by") or ""),
        metadata={key: value for key, value in fm.items() if key not in known},
    )


def parse_note(path: str | Path) -> Note:
    p = Path(path)
    return parse_text(p.read_text(encoding="utf-8"), path=p.name)


def iter_notes(directory: str | Path):
    """Plain ``*.md`` notes of a directory (carve/sweep). Refuses an encrypted store."""
    from memd.codec import refuse_encrypted
    refuse_encrypted(Path(directory), "this tool")
    for p in sorted(Path(directory).glob("*.md")):
        if p.name in CARVED_INDEX_FILES:
            continue
        yield parse_note(p)


def dump_note(note: Note) -> str:
    fm = {
        **note.metadata,
        "title": note.title,
        "slug": note.slug,
        "profile": note.profile,
        "host": note.host,
        "importance": note.importance,
        "superseded_by": note.superseded_by,
        "tags": note.tags,
        "grounding": note.grounding,
    }
    # `description` is the curated one-liner index_line() and render.py show; it
    # is parsed in at :170 but was not written back, so a single `mem-sweep
    # --write` (which round-trips EVERY note through here) erased it corpus-wide.
    if note.description:
        fm["description"] = note.description
    # `last_used` is emitted only when set. Writing `last_used: null` into every
    # note was churning the blob of files that had no timestamp, and a populated
    # value is what would trip recall's `n.last_used or 0` sort into comparing str
    # with int. Absent key == absent value; no information is lost.
    if note.last_used:
        fm["last_used"] = note.last_used
    if note.pinned:
        fm["pinned"] = True
    for key in ("source", "observed_at", "verified_at", "volatility", "saved_by"):
        value = getattr(note, key)
        if value:
            fm[key] = value
    head = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False).strip()
    return f"---\n{head}\n---\n{note.body}\n"


def index_line(note: Note) -> str:
    if note.description:
        desc = note.description
    elif note.body:
        desc = note.body.splitlines()[0]
    else:
        desc = ""
    return f"- [{note.title}]({note.path}) — {desc.strip()}"


# ---------------------------------------------------------------------------
# Clone read-path (Task 5): load notes from a MEMD_CLONE git working tree.
#
# This layers the git-aware read API on top of the legacy-tolerant parser.
# It NEVER writes. Defaults follow the read-path frontmatter contract:
#   profile=amber, host=any, importance=3, superseded_by=None, tags=[],
#   grounding=unverified-local (vs. the write/normalize default of "ok").
# Each Note carries its git_blob sha so the index can skip unchanged notes.
# ---------------------------------------------------------------------------


def _note_from_text(text: str, path: Path, git_blob: str) -> Note:
    """Parse one note for the clone read-path, applying read-path defaults.

    Reuses the legacy-tolerant ``parse_text`` (H1 titles, loose frontmatter,
    timestamp-as-string), then applies the read-path ``grounding`` default and
    attaches the git blob sha. ``parse_text`` defaults grounding to ``"ok"`` for
    the write/normalize path; the read-path default is ``"unverified-local"``.
    """
    fm, _ = _split(text)
    note = parse_text(text, path=path.as_posix())
    grounding = note.grounding if "grounding" in fm else "unverified-local"
    return replace(note, grounding=grounding, git_blob=git_blob)


def _blob_shas(clone: Path) -> dict[str, str]:
    """Map relative path -> git blob sha for every *unmodified* tracked file.

    Files modified in the working tree are intentionally omitted so ``list_notes``
    falls back to ``hash-object`` (which hashes the worktree content). This makes
    ``git_blob`` track the on-disk note, so the recall index re-embeds a note as
    soon as its body changes — even before the edit is committed.
    """
    out = subprocess.run(
        ["git", "-C", str(clone), "ls-files", "-s"],
        check=True, capture_output=True, text=True,
    ).stdout
    modified = set(
        subprocess.run(
            ["git", "-C", str(clone), "ls-files", "-m"],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
    )
    shas: dict[str, str] = {}
    for line in out.splitlines():
        # format: <mode> <sha> <stage>\t<path>
        meta, _, rel = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 2 and rel not in modified:
            shas[rel] = parts[1]
    return shas


def list_notes(clone: Path, codec=None) -> list[Note]:
    """List every note in the clone, frontmatter-parsed with git blob.

    Files come from the store's codec (memd.codec): ``*.md`` for a plaintext
    store, decrypted ``*.md.enc`` envelopes for an encrypted one. Skips the
    carved index files (MEMORY.md / MEMORY-full.md / README.md) the same way
    ``iter_notes`` does, so the recall index never ingests them. ``git_blob`` is
    always the blob of the stored file (the ciphertext for an encrypted store),
    so revision guards work unchanged.

    ``codec`` overrides the clone's configured codec; only mem-backup's drill
    passes one, for a restored clone at a scratch path that no setting names.
    """
    clone = Path(clone)
    from memd.codec import codec_for
    codec = codec if codec is not None else codec_for(clone)
    managed = codec.managed
    shas = _blob_shas(clone)
    notes: list[Note] = []
    for md in codec.note_files():
        # A note symlink can expose another profile or an arbitrary host file.
        # Reject links (including linked parent directories), even within clone.
        if md.is_symlink() or any((clone / parent).is_symlink()
                                 for parent in md.relative_to(clone).parents):
            continue
        if not md.is_file() or not md.resolve().is_relative_to(clone.resolve()):
            continue
        rel = md.relative_to(clone).as_posix()
        # hash-object gives a blob sha even for un-committed working-tree edits.
        blob = shas.get(rel) or subprocess.run(
            ["git", "-C", str(clone), "hash-object", str(md)],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        text, name = codec.decode(rel, md.read_bytes())
        if name is None:
            note = _note_from_text(text, md, blob)
        else:
            # Identity (the title's filename fallback) comes from the note's
            # original file name inside the envelope, never the opaque one.
            note = replace(_note_from_text(text, Path(name), blob), path=md.as_posix())
        if managed and managed["config"].get("managed"):
            note = replace(note, profile=managed["id"])
        notes.append(note)
    return notes


def read_note(clone: Path, slug: str) -> Note | None:
    for n in list_notes(clone):
        if n.slug == slug:
            return n
    return None


def git_head_sha(clone: Path) -> str:
    """HEAD sha, or "" when it cannot be determined.

    NEVER raises. recall() calls this to decide whether the index is stale, one
    line above the try/except that guards reindex — so `check=True` here used to
    fail the whole turn when the clone was missing, was not a repo, or had no
    commits (`git rev-parse HEAD` exits 128). An empty string makes the caller
    treat the index as current and serve it, which is the correct degradation for
    a read path whose contract is "never block or fail a turn".

    `timeout` is the other half: a git that hangs on a contended lock or a stalled
    network filesystem would otherwise block the turn with no bound. 2s is ~2800x
    the measured 0.72ms cost of this call, so it only trips on a real hang.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=2.0,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""
