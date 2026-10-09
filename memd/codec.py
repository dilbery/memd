"""The note codec: the single seam between a clone's files and note text.

Every path that lists, reads or writes note files in a clone goes through
``codec_for(clone)``: store listing and reading, save's writes, the review
branches of mem-summarize and mem-verify, and mem-crypt's migrations. Two
codecs exist:

* ``PlainCodec`` for ordinary stores: ``*.md`` files, exactly as before.
* ``EncryptedCodec`` for a store whose root holds the tracked ``.memd-encrypted``
  marker: notes are ``<hmac(slug) prefix>.md.enc`` envelopes (memd.crypt) with
  the note's original file name, slug, title and Markdown inside, and commit
  messages are generic.

Which codec applies is decided by the marker committed in the clone, never by
configuration alone, so a store is never read or written as plaintext because a
key setting went missing: a marked store without a working key fails closed,
and a configured key over an unmarked store also refuses until ``mem-crypt
encrypt`` has run. A mixed tree (plaintext notes or mem-carve output in an
encrypted store, or envelopes without the marker) is always refused. The marker
is committed and so comes from the Git host: nothing in it relaxes these checks.
A migration in progress is recorded only inside the clone's ``.git`` directory
(``migration_state``), which is never committed or pulled, and only mem-crypt
acts on it; here it merely words the refusal.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from memd.crypt import CryptError, StoreKey, load_key, open_sealed, seal
from memd.store import CARVE_OUTPUT_FILES, CARVED_INDEX_FILES, StoreUnavailable

MARKER = ".memd-encrypted"
# mem-crypt's record of a migration in progress, local to the clone (see above).
MIGRATION_STATE = "memd-crypt-migration.json"
ENC_SUFFIX = ".md.enc"
CIPHER = "AES-256-GCM"
# Commit messages of encrypted stores: the Git host sees every message, so they
# never name a note, an action on it, or the caller that made it.
GENERIC_COMMIT = "memd: update memory"
GENERIC_PROPOSAL = "memd: proposed changes for review"


class CodecError(StoreUnavailable):
    """The store's encryption state is unusable (missing/wrong key, mixed tree)."""


# ---------------------------------------------------------------------------
# Marker and key binding
# ---------------------------------------------------------------------------


def read_marker(clone: Path) -> dict | None:
    """The clone's encryption marker, or None for a plaintext store."""
    path = Path(clone) / MARKER
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise CodecError(f"{MARKER} must be a regular file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise CodecError(f"{MARKER} is unreadable; the store's encryption state is unknown") from None
    if (not isinstance(data, dict) or data.get("format") != "memd-encrypted-store"
            or data.get("version") != 1 or not isinstance(data.get("key_id"), str)):
        raise CodecError(f"{MARKER} is not a supported memd encryption marker")
    if "migrating" in data:
        # The marker is unauthenticated and comes from the Git host. memd never
        # writes this field (migration state stays in .git/), so a marker that
        # carries it was edited outside memd, e.g. to get plaintext notes read.
        raise CodecError(f"{MARKER} claims a migration in progress, which memd never commits; the "
                         f"marker was changed outside memd. Check `git log -p -- {MARKER}` and "
                         "restore it before using the store")
    return data


def marker_bytes(key: StoreKey) -> bytes:
    data = {"format": "memd-encrypted-store", "version": 1, "cipher": CIPHER,
            "key_id": key.key_id}
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def migration_state_path(clone: Path) -> Path:
    """Where mem-crypt records a migration in progress: inside ``.git/``, so the
    record is local to this clone and never committed, pushed or pulled."""
    return Path(clone) / ".git" / MIGRATION_STATE


def migration_state(clone: Path) -> dict | None:
    """``{"action": "encrypt"|"decrypt", "key_id": ...}`` of a mem-crypt run that
    is in progress or was interrupted in this clone, else None."""
    path = migration_state_path(clone)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise CodecError(f"mem-crypt's migration record {path} is unreadable") from None
    if (not isinstance(data, dict) or data.get("action") not in ("encrypt", "decrypt")
            or not isinstance(data.get("key_id"), str)):
        raise CodecError(f"mem-crypt's migration record {path} is not valid")
    return data


def _interrupted(clone: Path) -> str | None:
    try:
        state = migration_state(clone)
    except CodecError:
        return "mem-crypt's migration record in .git/ is damaged; check `git status`"
    if state:
        return (f"a `mem-crypt {state['action']}` run is in progress or was interrupted in this "
                f"clone; re-run `mem-crypt {state['action']}` to finish it")
    return None


def normalise_newlines(text: str) -> str:
    """Universal newlines, as ``Path.read_text`` reads a file: ``\\r\\n`` and a
    lone ``\\r`` become ``\\n``, so a CRLF note's frontmatter still parses."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def decode_text(data: bytes) -> str:
    """A plaintext note file's UTF-8 bytes as note text (universal newlines)."""
    return normalise_newlines(data.decode("utf-8"))


@dataclass(frozen=True)
class KeyBinding:
    """Where a clone's key comes from (a path, never key bytes)."""

    key_file: str | None
    setting: str
    obsidian: bool = False
    managed: dict | None = None


def _legacy_key_file(target: Path) -> tuple[str | None, str] | None:
    """MEMD_<PROFILE>_KEY_FILE of the legacy profile whose clone is ``target``."""
    from memd.config import default_profile, legacy_defaults, legacy_prefix, legacy_profiles, resolved_env
    env = resolved_env()
    active = env.get("MEMD_PROFILE") or default_profile(env)
    for profile in legacy_profiles(env):
        prefix = legacy_prefix(profile, env)
        paths = [env.get(f"{prefix}_CLONE") or legacy_defaults(profile)["clone_path"]]
        if profile == active and env.get("MEMD_CLONE"):
            paths.append(env["MEMD_CLONE"])
        for raw in paths:
            try:
                if raw and Path(raw).expanduser().resolve() == target:
                    return env.get(f"{prefix}_KEY_FILE") or None, f"{prefix}_KEY_FILE"
            except OSError:
                continue
    return None


def key_binding(clone: Path) -> KeyBinding:
    """The key file configured for a clone: the control-DB store's ``key_file``
    setting, else ``MEMD_<PROFILE>_KEY_FILE`` of the legacy profile it belongs to."""
    target = Path(clone).resolve()
    row = None
    from memd import control
    if control.enabled():
        from memd.sources import settings_for_clone
        row = settings_for_clone(target)
    obsidian = bool(row and row["kind"] == "obsidian")
    if row and row["config"].get("key_file"):
        return KeyBinding(row["config"]["key_file"], f"key_file of store {row['id']}", obsidian, row)
    legacy = _legacy_key_file(target)
    if legacy and legacy[0]:
        return KeyBinding(legacy[0], legacy[1], obsidian, row)
    if row:
        return KeyBinding(None, f"the key_file setting of store {row['id']}", obsidian, row)
    return KeyBinding(None, legacy[1] if legacy else "MEMD_<PROFILE>_KEY_FILE", obsidian, None)


def codec_for(clone: Path) -> "PlainCodec":
    """The codec for ``clone``; raises CodecError when it cannot be used safely."""
    clone = Path(clone)
    marker = read_marker(clone)
    binding = key_binding(clone)
    if marker is None:
        if binding.key_file:
            raise CodecError(_interrupted(clone) or (
                f"a key is configured for this store ({binding.setting}) but its clone is "
                "not encrypted; run `mem-crypt encrypt` or remove the key setting"))
        return PlainCodec(clone, binding.managed)
    if binding.obsidian:
        raise CodecError("Obsidian vault stores cannot be encrypted")
    if not binding.key_file:
        raise CodecError(f"this store is encrypted but no key is configured; set {binding.setting} "
                         "to the store's key file")
    try:
        key = load_key(binding.key_file)
    except CryptError as exc:
        raise CodecError(f"encrypted store unavailable: {exc}") from None
    if key.key_id != marker["key_id"]:
        raise CodecError(f"encrypted store unavailable: the configured key (key id {key.key_id}) is not "
                         f"this store's key (key id {marker['key_id']})")
    return EncryptedCodec(clone, binding.managed, key)


def is_encrypted(clone: Path) -> bool:
    """True when the clone carries the encryption marker (no key needed)."""
    return os.path.lexists(Path(clone) / MARKER)


def refuse_encrypted(clone: Path, tool: str) -> None:
    """For plaintext-only tools that work on files directly (carve, sweep, reflect)."""
    if is_encrypted(clone):
        raise CodecError(f"{tool} works on plaintext stores only; {clone} is encrypted")


# ---------------------------------------------------------------------------
# Codecs
# ---------------------------------------------------------------------------


def _in_git_dir(clone: Path, path: Path) -> bool:
    parts = path.relative_to(clone).parts
    return bool(parts) and parts[0] == ".git"


class PlainCodec:
    """Plain Markdown notes (``*.md``): the store format memd always had."""

    encrypted = False
    key_id: str | None = None

    def __init__(self, clone: Path, managed: dict | None = None):
        self.clone = Path(clone)
        self.managed = managed

    # -- listing -----------------------------------------------------------

    def _note_eligible(self, rel: str) -> bool:
        if Path(rel).name in CARVED_INDEX_FILES:
            return False
        if self.managed and self.managed["config"].get("managed"):
            from memd.sources import included
            return included(rel, self.managed["config"])
        return True

    def _scan(self) -> tuple[list[Path], list[Path], list[Path]]:
        """(plaintext notes, envelopes, mem-carve output files) in the clone.

        Include/exclude filters apply to plaintext notes only: an envelope's
        path is chosen by the codec (the clone root), not by the store layout.
        """
        plain, sealed, carved = [], [], []
        for path in sorted(self.clone.rglob("*.md*")):
            if _in_git_dir(self.clone, path):
                continue
            if path.name.endswith(ENC_SUFFIX):
                sealed.append(path)
            elif path.name in CARVE_OUTPUT_FILES:
                carved.append(path)
            elif path.suffix == ".md":
                if self._note_eligible(path.relative_to(self.clone).as_posix()):
                    plain.append(path)
        return plain, sealed, carved

    def note_files(self) -> list[Path]:
        """Candidate note files, sorted; refuses ciphertext in a plaintext store."""
        plain, sealed, _ = self._scan()
        if sealed:
            raise CodecError(f"found {len(sealed)} encrypted note file(s) but no {MARKER} marker; "
                             "the store's encryption state is inconsistent")
        return plain

    # -- encoding ----------------------------------------------------------

    def decode(self, rel: str, data: bytes) -> tuple[str, str | None]:
        """(Markdown text, identity file name or None to use the real path)."""
        if rel.endswith(ENC_SUFFIX):
            raise CodecError(f"{rel} is encrypted but the store is not")
        return decode_text(data), None

    def encode(self, rel: str, text: str, *, name: str | None = None) -> bytes:
        if rel.endswith(ENC_SUFFIX) or not rel.endswith(".md"):
            raise CodecError(f"refusing to write {rel} in a plaintext store")
        return text.encode("utf-8")

    def unchanged(self, rel: str, data: bytes, text: str) -> bool:
        """True when stored ``data`` already holds exactly ``text``."""
        return data == text.encode("utf-8")

    # -- naming ------------------------------------------------------------

    def filename(self, slug: str) -> str:
        from memd.save import note_filename
        return note_filename(slug)

    def new_note_path(self, directory: Path, slug: str) -> Path:
        """Allocate a filename independently of a new note's stable identity."""
        natural = Path(directory) / self.filename(slug)
        if not os.path.lexists(natural):
            return natural
        digest = hashlib.sha256(slug.encode()).hexdigest()[:10]
        base = f"{natural.stem}__{digest}"
        candidate = Path(directory) / f"{base}.md"
        number = 2
        while os.path.lexists(candidate):
            candidate = Path(directory) / f"{base}_{number}.md"
            number += 1
        return candidate

    def taken_names(self, slug: str) -> list[str]:
        """File names that would already claim ``slug`` (save's supersede check)."""
        return [self.filename(slug), f"{slug}.md"]

    def commit_message(self, message: str, *, proposal: bool = False) -> str:
        return message


class EncryptedCodec(PlainCodec):
    """Encrypted notes: opaque ``<hmac>.md.enc`` envelopes and generic commits."""

    encrypted = True

    def __init__(self, clone: Path, managed: dict | None, key: StoreKey):
        super().__init__(clone, managed)
        self.key = key
        self.key_id = key.key_id

    def note_files(self) -> list[Path]:
        """Every envelope, sorted. Refuses plaintext notes and mem-carve output:
        either would publish note contents or titles in the clear, and the
        committed marker can never vouch for them."""
        plain, sealed, carved = self._scan()
        rel = lambda p: p.relative_to(self.clone).as_posix()  # noqa: E731
        if carved:
            raise CodecError(f"found mem-carve output in an encrypted store ({', '.join(map(rel, carved))}); "
                             "it lists every note's title in plaintext and is for plaintext stores "
                             "only. Run `mem-crypt encrypt` to remove it")
        if plain:
            detail = _interrupted(self.clone) or (
                "memd never writes plaintext notes here: review them, then remove them or "
                "encrypt them with `mem-crypt encrypt`")
            raise CodecError(f"found {len(plain)} plaintext note file(s) in an encrypted store "
                             f"(first: {rel(plain[0])}); {detail}")
        return sealed

    def decode(self, rel: str, data: bytes) -> tuple[str, str | None]:
        if not rel.endswith(ENC_SUFFIX):
            raise CodecError(f"{rel} is not an encrypted note")
        try:
            payload = open_sealed(self.key, rel, data)
        except CryptError as exc:
            raise CodecError(f"encrypted store unavailable: {exc}") from None
        name = payload.get("name")
        name = name if isinstance(name, str) and name.endswith(".md") else None
        return normalise_newlines(payload["text"]), name

    def encode(self, rel: str, text: str, *, name: str | None = None) -> bytes:
        if not rel.endswith(ENC_SUFFIX):
            raise CodecError(f"refusing to write plaintext {rel} in an encrypted store")
        from memd.store import parse_text
        note = parse_text(text, path=name or "note.md")
        if name is None:
            name = PlainCodec.filename(self, note.slug)
        return seal(self.key, rel, {"v": 1, "name": name, "slug": note.slug,
                                    "title": note.title, "text": text})

    def unchanged(self, rel: str, data: bytes, text: str) -> bool:
        try:
            return self.decode(rel, data)[0] == text
        except (CodecError, UnicodeDecodeError):
            return False

    def filename(self, slug: str) -> str:
        return self.key.file_stem(slug) + ENC_SUFFIX

    def new_note_path(self, directory: Path, slug: str) -> Path:
        stem = self.key.file_stem(slug)
        candidate = Path(self.clone) / f"{stem}{ENC_SUFFIX}"
        number = 2
        while os.path.lexists(candidate):
            candidate = Path(self.clone) / f"{stem}_{number}{ENC_SUFFIX}"
            number += 1
        return candidate

    def taken_names(self, slug: str) -> list[str]:
        return [self.filename(slug)]

    def commit_message(self, message: str, *, proposal: bool = False) -> str:
        return GENERIC_PROPOSAL if proposal else GENERIC_COMMIT
