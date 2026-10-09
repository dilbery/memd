"""mem-backup: encrypted backup bundles, verified restore and recovery drills.

``mem-backup create`` writes ONE encrypted bundle per run to MEMD_BACKUP_DIR
holding, for every store this process can see (the legacy profiles, stores in
the control database and stores under MEMD_STORES_ROOT):

  * ``git bundle --all`` of the clone: every committed note and its history,
    review branches included. Uncommitted working-tree edits are not in it.
  * consistent snapshots (SQLite backup API) of the side files next to the
    index that Git cannot rebuild: the review inbox (``memd.inbox.db``) and the
    usage log (``memd.usage.db``).
  * the store's key-file REFERENCE (path, setting and key id), never key bytes.

plus snapshots of the administration database (MEMD_ADMIN_DB: accounts, stores,
grants, tokens) and the token registry (MEMD_CONTROL_DB), with browser sessions
removed. The index (``memd.db``) is not included: it is rebuilt from Git.

Store keys and the backup key are never in a bundle; back them up separately
(docs/OPERATING.md, "Backups"). Without a store's key its restored clone is
ciphertext, and without the backup key a bundle is noise.

Bundle format (``memd-backup-<UTC time to the microsecond>-<random>.mbk``):

  header  ``MAGIC | VERSION | key id (8) | salt (16) | chunk size (4)``, plaintext
  chunks  AES-256-GCM, each ``chunk size`` plaintext bytes (the final one
          shorter, possibly empty) plus a 16-byte tag

The bundle key is HKDF-SHA256(backup key, salt, "memd backup bundle v1"), so
every bundle has its own key. Chunk ``i``'s nonce is ``i`` (8 bytes) followed
by a 4-byte final-chunk flag, and the header is every chunk's associated data:
a changed header, a flipped bit, reordered or dropped chunks, a truncated file,
trailing bytes and a wrong key all fail authentication. The plaintext stream is
``manifest length (4) | manifest JSON | members in manifest order``; the
manifest lists the stores, and every member's path, size and SHA-256, the memd
version and the creation time.

``verify`` checks a bundle end to end without writing anything; ``restore --to``
unpacks into an empty or new directory only, checking every hash; ``drill``
restores the newest bundle into a scratch directory, clones every Git bundle,
checks HEADs, refs and note counts against the manifest, opens the side files,
rebuilds a lexical index for one store and runs a sample recall query on it,
then deletes the scratch directory. It exits 1 and names what failed.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from memd.crypt import CryptError, generate_key_file, load_key_bytes

MAGIC = b"MEMDBAK"
VERSION = 1
KEY_ID_BYTES = 8
SALT_BYTES = 16
TAG_BYTES = 16
HEADER = struct.Struct(f">{len(MAGIC)}sB{KEY_ID_BYTES}s{SALT_BYTES}sI")
CHUNK_SIZE = 1 << 20
MIN_CHUNK, MAX_CHUNK = 1 << 10, 1 << 24
MAX_MANIFEST = 16 << 20
SUFFIX = ".mbk"
PREFIX = "memd-backup-"
DEFAULT_KEEP = 14
FORMAT = "memd-backup"
# A member path is a few safe segments; nothing absolute, hidden or '..'.
_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._%+@-]{0,199}$")
_STORE_DIR = re.compile(r"^[a-z0-9_][a-z0-9._%+@-]{0,199}$")
_BUNDLE_NAME = re.compile(rf"^{PREFIX}\d{{8}}T\d{{6}}\.\d{{6}}Z-[0-9a-f]{{8}}\{SUFFIX}$")


class BackupError(RuntimeError):
    """A bundle cannot be written, read or trusted. Never carries key bytes."""


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackupKey:
    """The backup key file's bytes (hidden from repr) and its public key id."""

    key: bytes
    key_id: bytes

    def __repr__(self) -> str:
        return f"BackupKey(key_id={self.key_id.hex()})"

    def bundle_key(self, salt: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                    info=b"memd backup bundle v1").derive(self.key)


def load_backup_key(path: str | Path) -> BackupKey:
    """Read a backup key file; the same rules as a store key (memd.crypt)."""
    raw = load_key_bytes(path)
    key_id = HKDF(algorithm=hashes.SHA256(), length=KEY_ID_BYTES, salt=None,
                  info=b"memd backup key id v1").derive(raw)
    return BackupKey(raw, key_id)


def _env() -> dict[str, str]:
    from memd.config import resolved_env
    return resolved_env()


def _key_path(given: str | None) -> Path:
    path = given or (_env().get("MEMD_BACKUP_KEY_FILE") or "").strip()
    if not path:
        raise BackupError("no backup key: set MEMD_BACKUP_KEY_FILE (create one with "
                          "`mem-backup keygen`)")
    return Path(path).expanduser()


def _backup_dir(given: str | None) -> Path:
    path = given or (_env().get("MEMD_BACKUP_DIR") or "").strip()
    if not path:
        raise BackupError("no backup directory: set MEMD_BACKUP_DIR")
    return Path(path).expanduser()


def keep_count(raw: str | None) -> int:
    """MEMD_BACKUP_KEEP: bundles kept after a create (default 14, at least 1)."""
    if raw is None or not str(raw).strip():
        return DEFAULT_KEEP
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise BackupError(f"MEMD_BACKUP_KEEP must be a whole number, not {raw!r}") from None
    if value < 1:
        raise BackupError("MEMD_BACKUP_KEEP must be at least 1")
    return value


# ---------------------------------------------------------------------------
# Chunked AES-256-GCM stream
# ---------------------------------------------------------------------------


def _nonce(index: int, final: bool) -> bytes:
    return struct.pack(">QI", index, 1 if final else 0)


class _Writer:
    """Encrypts a plaintext stream into ``fh`` in fixed-size authenticated chunks."""

    def __init__(self, fh, key: BackupKey, chunk_size: int = CHUNK_SIZE):
        if not MIN_CHUNK <= chunk_size <= MAX_CHUNK:
            raise BackupError(f"chunk size must be between {MIN_CHUNK} and {MAX_CHUNK}")
        salt = secrets.token_bytes(SALT_BYTES)
        self.header = HEADER.pack(MAGIC, VERSION, key.key_id, salt, chunk_size)
        self.aead = AESGCM(key.bundle_key(salt))
        self.fh, self.size, self.index, self.buffer = fh, chunk_size, 0, bytearray()
        fh.write(self.header)

    def _emit(self, data: bytes, final: bool) -> None:
        self.fh.write(self.aead.encrypt(_nonce(self.index, final), data, self.header))
        self.index += 1

    def write(self, data: bytes) -> None:
        self.buffer += data
        # Keep at least one byte back so close() always has the final chunk to
        # mark, even when the stream is an exact multiple of the chunk size.
        while len(self.buffer) > self.size:
            self._emit(bytes(self.buffer[:self.size]), False)
            del self.buffer[:self.size]

    def close(self) -> None:
        self._emit(bytes(self.buffer), True)
        self.buffer.clear()


class _Reader:
    """Authenticates and decrypts a bundle chunk by chunk, as a file-like ``read``."""

    def __init__(self, fh, key: BackupKey):
        self.fh = fh
        head = fh.read(HEADER.size)
        if len(head) < HEADER.size:
            raise BackupError("not a memd backup bundle (file too short)")
        magic, version, key_id, salt, size = HEADER.unpack(head)
        if magic != MAGIC:
            raise BackupError("not a memd backup bundle (bad magic)")
        if version != VERSION:
            raise BackupError(f"unsupported backup bundle version {version}")
        if key_id != key.key_id:
            raise BackupError(f"the bundle was encrypted with another backup key (key id "
                              f"{key_id.hex()}, configured key id {key.key_id.hex()})")
        if not MIN_CHUNK <= size <= MAX_CHUNK:
            raise BackupError("bundle header is damaged (chunk size out of range)")
        self.header, self.size = head, size
        self.aead = AESGCM(key.bundle_key(salt))
        self.index, self.buffer, self.done = 0, bytearray(), False
        self.pending = fh.read(size + TAG_BYTES)

    def _next(self) -> None:
        block = self.pending
        self.pending = self.fh.read(self.size + TAG_BYTES) if len(block) == self.size + TAG_BYTES else b""
        final = not self.pending
        if len(block) < TAG_BYTES:
            raise BackupError(f"bundle is truncated (chunk {self.index} is incomplete)")
        try:
            data = self.aead.decrypt(_nonce(self.index, final), block, self.header)
        except InvalidTag:
            raise BackupError(f"bundle failed authentication at chunk {self.index}: it was "
                              "modified, truncated or reordered") from None
        self.index += 1
        self.buffer += data
        self.done = final

    def read(self, n: int) -> bytes:
        while len(self.buffer) < n and not self.done:
            self._next()
        out = bytes(self.buffer[:n])
        del self.buffer[:n]
        return out

    def exactly(self, n: int, what: str) -> bytes:
        data = self.read(n)
        if len(data) != n:
            raise BackupError(f"bundle ended inside {what}")
        return data

    def finish(self) -> None:
        """Read to the authenticated end; any plaintext past the last member is an error."""
        while not self.done:
            self._next()
        if self.buffer:
            raise BackupError("bundle has unexpected data after its last member")


# ---------------------------------------------------------------------------
# What gets backed up
# ---------------------------------------------------------------------------


@dataclass
class StoreSource:
    name: str
    kind: str
    clone: Path
    db: Path | None


def configured_stores() -> list[StoreSource]:
    """Every store this process can resolve whose clone exists, one per clone."""
    from memd import control
    from memd.config import Config, default_profile, legacy_profiles
    from memd.profiles import registry, resolve
    from memd.stores import known_stores

    env = _env()
    legacy = set(legacy_profiles(env))
    active = env.get("MEMD_PROFILE") or default_profile(env)
    names = list(dict.fromkeys([*registry(), *known_stores()]))
    seen: set[Path] = set()
    out: list[StoreSource] = []
    for name in names:
        try:
            paths = resolve(name)
            clone, db = Path(paths["clone_path"]), Path(paths["db_path"])
            if name == active and name in legacy and not control.store(name):
                # MEMD_CLONE/MEMD_DB override the serving profile's paths.
                cfg = Config.from_env()
                clone, db = Path(cfg.clone or clone), Path(cfg.db or db)
        except Exception:   # an unresolvable row is not a store this process serves
            continue
        clone = clone.expanduser().resolve()
        if clone in seen or not (clone / ".git").exists():
            continue
        seen.add(clone)
        kind = "control" if control.store(name) else "legacy" if name in legacy else "stores-root"
        out.append(StoreSource(name, kind, clone, db.expanduser().resolve() if db else None))
    return out


def _git(clone: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(clone), *args], check=check, capture_output=True,
                          text=True, timeout=600)


def note_count(repo: Path, rev: str) -> int:
    """Note files in the tree at ``rev``: ``*.md`` (not carved index files) and ``*.md.enc``."""
    from memd.codec import ENC_SUFFIX
    from memd.store import CARVED_INDEX_FILES

    names = _git(repo, "ls-tree", "-r", "-z", "--name-only", rev).stdout.split("\0")
    return sum(1 for n in names if n and not n.startswith(".git/") and (
        n.endswith(ENC_SUFFIX) or (n.endswith(".md") and n.rsplit("/", 1)[-1] not in CARVED_INDEX_FILES)))


def bundle_heads(bundle: Path) -> dict[str, str]:
    """``{ref: sha}`` a git bundle carries (HEAD included)."""
    out = subprocess.run(["git", "bundle", "list-heads", str(bundle)], check=True,
                         capture_output=True, text=True, timeout=600).stdout
    heads = {}
    for line in out.splitlines():
        sha, _, ref = line.partition(" ")
        if ref:
            heads[ref] = sha
    return heads


def snapshot_sqlite(source: Path, dest: Path, *, drop: tuple[str, ...] = ()) -> None:
    """A consistent copy of a live SQLite file (backup API: one read transaction).

    ``drop`` names tables emptied in the copy (browser sessions must never be
    restored). The copy is a single rollback-journal file, checked for integrity.
    """
    fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=rw", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            tables = {r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in drop:
                if table in tables:
                    dst.execute(f'DELETE FROM "{table}"')
            dst.commit()
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError(f"the snapshot of {source.name} failed its integrity check")
        finally:
            dst.close()
    finally:
        src.close()


def _key_reference(clone: Path) -> dict:
    """Whether a clone is encrypted and where its key is configured (never the key)."""
    from memd.codec import key_binding, read_marker

    try:
        marker = read_marker(clone)
    except Exception as exc:
        return {"encrypted": True, "key_id": None, "key_file": None, "key_setting": None,
                "key_note": f"marker unreadable: {exc}"}
    if marker is None:
        return {"encrypted": False, "key_id": None, "key_file": None, "key_setting": None}
    try:
        binding = key_binding(clone)
        key_file, setting = binding.key_file, binding.setting
    except Exception:
        key_file, setting = None, None
    return {"encrypted": True, "key_id": marker.get("key_id"), "key_file": key_file,
            "key_setting": setting}


def _store_dir(name: str, index: int, used: set[str]) -> str:
    base = name if _STORE_DIR.match(name) else f"store-{index}"
    while base in used:
        base = f"{base}-{index}"
    used.add(base)
    return base


def _hash_file(path: Path) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as fh:
        while block := fh.read(1 << 20):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


def _memd_version() -> str:
    try:
        from importlib.metadata import version
        return version("memd")
    except Exception:
        from memd import __version__
        return __version__


def _stage(staging: Path, stores: list[StoreSource], env: dict[str, str]) -> tuple[dict, list[tuple[str, Path]]]:
    """Write every member into ``staging``; returns the manifest (no member hashes yet)."""
    from memd.inbox import inbox_file
    from memd.usage import usage_path

    members: list[tuple[str, Path]] = []
    entries, used = [], set()
    for i, src in enumerate(stores):
        directory = _store_dir(src.name, i, used)
        (staging / "stores" / directory).mkdir(parents=True, mode=0o700)
        entry = {"name": src.name, "kind": src.kind, "dir": f"stores/{directory}",
                 "head": "", "refs": {}, "notes": 0, "members": {}, **_key_reference(src.clone)}
        bundle = staging / "stores" / directory / "repo.bundle"
        heads = _git(src.clone, "rev-parse", "--verify", "-q", "HEAD", check=False)
        refs = _git(src.clone, "for-each-ref", "--count=1", check=False).stdout.strip()
        if heads.returncode == 0 or refs:
            _git(src.clone, "bundle", "create", "-q", str(bundle), "--all")
            carried = bundle_heads(bundle)
            entry["head"] = carried.get("HEAD", "")
            entry["refs"] = {r: s for r, s in carried.items() if r != "HEAD"}
            if entry["head"]:
                entry["notes"] = note_count(src.clone, entry["head"])
            path = f"stores/{directory}/repo.bundle"
            entry["members"]["bundle"] = path
            members.append((path, bundle))
        if src.db is not None:
            for role, side in (("inbox", inbox_file(src.db)), ("usage", usage_path(src.db))):
                if side.is_file():
                    path = f"stores/{directory}/{role}.db"
                    snapshot_sqlite(side, staging / path)
                    entry["members"][role] = path
                    members.append((path, staging / path))
        entries.append(entry)

    control = {}
    (staging / "control").mkdir(mode=0o700)
    for role, var, filename in (("admin", "MEMD_ADMIN_DB", "admin.db"),
                                ("registry", "MEMD_CONTROL_DB", "registry.db")):
        raw = (env.get(var) or "").strip()
        if raw and Path(raw).expanduser().is_file():
            path = f"control/{filename}"
            snapshot_sqlite(Path(raw).expanduser(), staging / path, drop=("sessions",))
            control[role] = {"path": path, "setting": var, "source": str(Path(raw).expanduser())}
            members.append((path, staging / path))

    manifest = {
        "format": FORMAT, "version": VERSION, "memd_version": _memd_version(),
        "created": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stores": entries, "control": control,
        "excluded": {"index": "rebuilt from Git (mem reindex)",
                     "keys": "store keys and the backup key are never in a bundle; back them up separately"},
    }
    return manifest, members


def _write_bundle(path: Path, key: BackupKey, manifest: dict, members: list[tuple[str, Path]],
                  chunk_size: int) -> dict:
    manifest = dict(manifest, members=[])
    for name, source in members:
        size, digest = _hash_file(source)
        manifest["members"].append({"path": name, "size": size, "sha256": digest})
    blob = json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as fh:
        writer = _Writer(fh, key, chunk_size)
        writer.write(struct.pack(">I", len(blob)) + blob)
        for (name, source), member in zip(members, manifest["members"]):
            digest, size = hashlib.sha256(), 0
            with open(source, "rb") as src:
                while block := src.read(1 << 20):
                    digest.update(block)
                    size += len(block)
                    writer.write(block)
            if (size, digest.hexdigest()) != (member["size"], member["sha256"]):
                raise BackupError(f"{name} changed while it was being written")
        writer.close()
        fh.flush()
        os.fsync(fh.fileno())
    return manifest


def bundles(directory: Path) -> list[Path]:
    """Bundles in ``directory``, oldest first (their names sort by creation time)."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if _BUNDLE_NAME.match(p.name) and p.is_file())


def prune(directory: Path, keep: int) -> list[Path]:
    """Delete the oldest bundles beyond ``keep``; returns what was removed."""
    old = bundles(directory)[:-keep] if keep > 0 else []
    for path in old:
        path.unlink()
    return old


def create(*, out_dir: Path, key: BackupKey, keep: int = DEFAULT_KEEP,
           stores: list[StoreSource] | None = None, chunk_size: int = CHUNK_SIZE,
           env: dict[str, str] | None = None) -> tuple[Path, dict]:
    """Write one bundle to ``out_dir`` and prune to ``keep``; returns (path, manifest)."""
    env = _env() if env is None else env
    stores = configured_stores() if stores is None else stores
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    final = out_dir / f"{PREFIX}{stamp}-{secrets.token_hex(4)}{SUFFIX}"
    partial = out_dir / f".{final.name}.partial"
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=out_dir))
    try:
        manifest, members = _stage(staging, stores, env)
        manifest = _write_bundle(partial, key, manifest, members, chunk_size)
        os.replace(partial, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        partial.unlink(missing_ok=True)
    _fsync_dir(out_dir)
    prune(out_dir, keep)
    return final, manifest


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Reading: verify and restore
# ---------------------------------------------------------------------------


def _check_member_path(path: object) -> str:
    if not isinstance(path, str) or not path:
        raise BackupError("manifest names a member without a path")
    parts = path.split("/")
    if len(parts) > 4 or any(not _SEGMENT.match(p) or p in (".", "..") for p in parts):
        raise BackupError(f"manifest names an unsafe member path {path[:80]!r}")
    return path


def _manifest(reader: _Reader) -> dict:
    (length,) = struct.unpack(">I", reader.exactly(4, "the manifest length"))
    if length > MAX_MANIFEST:
        raise BackupError("manifest is implausibly large")
    try:
        manifest = json.loads(reader.exactly(length, "the manifest").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BackupError("manifest is unreadable") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT \
            or manifest.get("version") != VERSION or not isinstance(manifest.get("members"), list):
        raise BackupError("manifest is not a supported memd backup manifest")
    seen = set()
    for member in manifest["members"]:
        if not isinstance(member, dict):
            raise BackupError("manifest member entry is malformed")
        path = _check_member_path(member.get("path"))
        if path in seen or not isinstance(member.get("size"), int) or member["size"] < 0 \
                or not isinstance(member.get("sha256"), str):
            raise BackupError(f"manifest entry for {path} is malformed")
        seen.add(path)
    return manifest


def _read_members(reader: _Reader, manifest: dict, sink) -> None:
    """Stream each member through ``sink(path)`` (a writable or None), checking size and hash."""
    for member in manifest["members"]:
        out = sink(member["path"])
        digest, left = hashlib.sha256(), member["size"]
        try:
            while left:
                block = reader.read(min(left, 1 << 20))
                if not block:
                    raise BackupError(f"bundle ended inside {member['path']}")
                digest.update(block)
                left -= len(block)
                if out is not None:
                    out.write(block)
        finally:
            if out is not None:
                out.close()
        if digest.hexdigest() != member["sha256"]:
            raise BackupError(f"{member['path']} does not match its manifest hash")
    reader.finish()


def verify(bundle: Path, key: BackupKey) -> dict:
    """Authenticate every chunk and check every member hash; nothing is written."""
    with open(bundle, "rb") as fh:
        reader = _Reader(fh, key)
        manifest = _manifest(reader)
        _read_members(reader, manifest, lambda path: None)
    return manifest


def _refuse_target(target: Path) -> bool:
    """Raise unless ``target`` is new or an empty directory; True when it must be created."""
    if not os.path.lexists(target):
        return True
    if target.is_symlink() or not target.is_dir():
        raise BackupError(f"restore target {target} exists and is not a directory")
    if any(target.iterdir()):
        raise BackupError(f"restore target {target} is not empty; restore only into a new or "
                          "empty directory, never over live data")
    return False


def restore(bundle: Path, key: BackupKey, target: Path) -> dict:
    """Unpack ``bundle`` into ``target`` (new or empty), checking every hash.

    On any failure everything written is removed again, so a restore either
    completes or leaves ``target`` as it found it.
    """
    target = Path(target).expanduser()
    created = _refuse_target(target)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = target.resolve()

    def sink(path: str):
        dest = (root / path).resolve()
        if not dest.is_relative_to(root) or dest == root:
            raise BackupError(f"member {path} escapes the restore directory")
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        return os.fdopen(os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb")

    try:
        with open(bundle, "rb") as fh:
            reader = _Reader(fh, key)
            manifest = _manifest(reader)
            _read_members(reader, manifest, sink)
        fd = os.open(root / "manifest.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(manifest, out, indent=2, sort_keys=True)
            out.write("\n")
    except BaseException:
        if created:
            shutil.rmtree(root, ignore_errors=True)
        else:
            for child in root.iterdir():
                shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        raise
    return manifest


# ---------------------------------------------------------------------------
# Drill
# ---------------------------------------------------------------------------


def _sqlite_ok(path: Path) -> str:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        raise BackupError(f"integrity check: {result}")
    return f"{tables} table(s)"


def _drill_index(clone: Path, entry: dict, scratch: Path) -> str:
    """Rebuild a lexical index for a restored clone and run one sample recall."""
    from memd.codec import EncryptedCodec, PlainCodec
    from memd.config import Config
    from memd.index import open_db, refresh_lexical
    from memd.query import distill
    from memd.recall import _bm25_arm
    from memd.store import list_notes

    if entry.get("encrypted"):
        from memd.crypt import load_key
        key = load_key(entry["key_file"])
        if key.key_id != entry.get("key_id"):
            raise BackupError("the configured key file is not this store's key")
        codec = EncryptedCodec(clone, None, key)
    else:
        codec = PlainCodec(clone)
    notes = [n for n in list_notes(clone, codec=codec) if not n.superseded_by]
    cfg = Config(clone=clone, db=scratch / "drill-index.db", profile=entry["name"])
    db = open_db(cfg.db)
    try:
        refresh_lexical(db, cfg, notes=notes)
        indexed = db.execute("SELECT count(*) FROM notes").fetchone()[0]
        if indexed != len(notes):
            raise BackupError(f"index holds {indexed} note(s), the clone {len(notes)}")
        if not notes:
            return "index rebuilt (no notes to query)"
        probe = next((n for n in notes if n.title.strip()), notes[0])
        hits = _bm25_arm(db, distill(db, probe.title or probe.slug))
        if probe.slug not in hits:
            raise BackupError(f"sample recall for {probe.slug!r} did not return it")
        return f"index rebuilt ({indexed} notes); sample recall found {probe.slug}"
    finally:
        db.close()


def drill(bundle: Path, key: BackupKey, *, out=print) -> list[tuple[str, bool, str]]:
    """Restore ``bundle`` into a scratch directory and prove it usable.

    Returns ``[(check, ok, detail)]``; the scratch directory is always removed.
    """
    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn) -> object:
        try:
            detail = fn()
        except Exception as exc:
            results.append((name, False, f"{type(exc).__name__}: {exc}"))
            out(f"   FAILED {name}: {type(exc).__name__}: {exc}")
            return None
        results.append((name, True, str(detail or "")))
        out(f"   ok     {name}{': ' + str(detail) if detail else ''}")
        return True

    scratch = Path(tempfile.mkdtemp(prefix="memd-drill-"))
    try:
        restored = scratch / "restore"
        manifest: dict = {}

        def _restore():
            manifest.update(restore(bundle, key, restored))
            return f"{len(manifest['members'])} member(s), created {manifest.get('created')}"

        if not check(f"restore {bundle.name}", _restore):
            return results
        candidates = []
        for i, entry in enumerate(manifest.get("stores", [])):
            name = entry.get("name", f"store-{i}")
            members = entry.get("members", {})
            clone = scratch / "clones" / str(i)
            if "bundle" in members:
                def _clone(entry=entry, clone=clone, path=restored / members["bundle"]):
                    heads = bundle_heads(path)
                    if heads.get("HEAD", "") != entry["head"]:
                        raise BackupError(f"bundle HEAD {heads.get('HEAD')} != manifest {entry['head']}")
                    if {r: s for r, s in heads.items() if r != "HEAD"} != entry["refs"]:
                        raise BackupError("bundle refs differ from the manifest")
                    clone.parent.mkdir(parents=True, exist_ok=True)
                    subprocess.run(["git", "clone", "-q", str(path), str(clone)], check=True,
                                   capture_output=True, text=True, timeout=600)
                    head = _git(clone, "rev-parse", "HEAD").stdout.strip()
                    if head != entry["head"]:
                        raise BackupError(f"cloned HEAD {head} != manifest {entry['head']}")
                    count = note_count(clone, "HEAD")
                    if count != entry["notes"]:
                        raise BackupError(f"{count} note(s) at HEAD, manifest says {entry['notes']}")
                    return f"HEAD {head[:12]}, {count} note(s), {len(entry['refs'])} ref(s)"

                if check(f"{name}: clone", _clone):
                    candidates.append((entry, clone))
            elif entry.get("head"):
                results.append((f"{name}: clone", False, "manifest has a HEAD but no Git bundle"))
                out(f"   FAILED {name}: clone: manifest has a HEAD but no Git bundle")
            for role in ("inbox", "usage"):
                if role in members:
                    check(f"{name}: {role} db", lambda p=restored / members[role]: _sqlite_ok(p))
        for role, info in manifest.get("control", {}).items():
            check(f"control: {role} db", lambda p=restored / info["path"]: _sqlite_ok(p))

        # One store is enough to prove notes are readable and recall works: a
        # plaintext one if any, else an encrypted one whose key file is here.
        usable = [c for c in candidates if not c[0].get("encrypted")] or [
            c for c in candidates if c[0].get("key_file") and Path(c[0]["key_file"]).is_file()]
        if usable:
            entry, clone = max(usable, key=lambda c: c[0].get("notes", 0))
            check(f"{entry['name']}: index and recall",
                  lambda: _drill_index(clone, entry, scratch))
        elif candidates:
            out("   note   no plaintext store and no store key file here: index rebuild skipped")
        return results
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _newest(directory: Path) -> Path:
    found = bundles(directory)
    if not found:
        raise BackupError(f"no backup bundles in {directory}")
    return found[-1]


def _human(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(size)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mem-backup", description=__doc__.split("\n\n")[0])
    ap.add_argument("--key-file", default=None, help="backup key file (default MEMD_BACKUP_KEY_FILE)")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create", help="write one encrypted bundle of every store, then prune")
    p.add_argument("--dir", default=None, help="output directory (default MEMD_BACKUP_DIR)")
    p.add_argument("--keep", default=None, help="bundles to keep (default MEMD_BACKUP_KEEP or 14)")
    p = sub.add_parser("verify", help="check a bundle's integrity without extracting it")
    p.add_argument("bundle", nargs="?", help="bundle file (default: newest in MEMD_BACKUP_DIR)")
    p.add_argument("--dir", default=None)
    p = sub.add_parser("restore", help="unpack a bundle into a new or empty directory")
    p.add_argument("bundle", nargs="?", help="bundle file (default: newest in MEMD_BACKUP_DIR)")
    p.add_argument("--to", required=True, help="new or empty directory to restore into")
    p.add_argument("--dir", default=None)
    p = sub.add_parser("drill", help="restore the newest bundle into scratch space and prove it works")
    p.add_argument("bundle", nargs="?", help="bundle file (default: newest in MEMD_BACKUP_DIR)")
    p.add_argument("--dir", default=None)
    sub.add_parser("keygen", help="create the backup key file (never overwrites one)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        if args.command == "keygen":
            path = _key_path(args.key_file)
            if os.path.lexists(path):
                raise BackupError(f"{path} already exists; a backup key is never overwritten")
            generate_key_file(path)
            print(f"mem-backup: wrote backup key {path} (key id {load_backup_key(path).key_id.hex()}). "
                  "Copy it somewhere safe and separate from the bundles: without it no bundle "
                  "can be restored.")
            return 0
        key = load_backup_key(_key_path(args.key_file))
        if args.command == "create":
            env = _env()
            keep = keep_count(args.keep if args.keep is not None else env.get("MEMD_BACKUP_KEEP"))
            started = time.monotonic()
            path, manifest = create(out_dir=_backup_dir(args.dir), key=key, keep=keep, env=env)
            print(f"mem-backup: wrote {path} ({_human(path.stat().st_size)}, "
                  f"{time.monotonic() - started:.1f}s)")
            for entry in manifest["stores"]:
                enc = f", encrypted (key id {entry['key_id']})" if entry["encrypted"] else ""
                print(f"   store {entry['name']}: HEAD {entry['head'][:12] or '-'}, "
                      f"{entry['notes']} note(s), {', '.join(entry['members']) or 'no members'}{enc}")
            for role in manifest["control"]:
                print(f"   control: {role}")
            if not manifest["stores"]:
                print("mem-backup: warning: no store clone was found to back up", file=sys.stderr)
            return 0
        bundle = Path(args.bundle) if args.bundle else _newest(_backup_dir(args.dir))
        if args.command == "verify":
            manifest = verify(bundle, key)
            print(f"mem-backup: {bundle} is intact: {len(manifest['stores'])} store(s), "
                  f"{len(manifest['members'])} member(s), created {manifest['created']}")
            return 0
        if args.command == "restore":
            manifest = restore(bundle, key, Path(args.to))
            print(f"mem-backup: restored {len(manifest['members'])} member(s) to {args.to}; "
                  "every hash matched. Clone a store with `git clone <dir>/stores/<name>/repo.bundle`.")
            return 0
        print(f"== mem-backup drill: {bundle}")
        results = drill(bundle, key)
        failed = [r for r in results if not r[1]]
        if failed:
            print(f"mem-backup: drill FAILED ({len(failed)} of {len(results)} checks): "
                  + "; ".join(name for name, _, _ in failed), file=sys.stderr)
            return 1
        print(f"mem-backup: drill passed ({len(results)} checks)")
        return 0
    except (BackupError, CryptError, OSError, subprocess.CalledProcessError, sqlite3.Error) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else exc
        print(f"mem-backup: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
