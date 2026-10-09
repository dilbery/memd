"""`mem-crypt`: key files and whole-store encryption for personal stores.

    mem-crypt keygen PATH            write a new 32-byte key (mode 0600), print its key id
    mem-crypt status                 the configured store's encryption state
    mem-crypt encrypt [--dry-run]    encrypt every plaintext note in one commit
    mem-crypt decrypt [--dry-run]    turn an encrypted store back into plaintext notes

The store is the one ``MEMD_PROFILE`` selects (like every mem-* tool), and its
key is the one the server will use: ``MEMD_<PROFILE>_KEY_FILE`` for a legacy
store, the ``key_file`` setting for an administered store. A migration records
itself in the clone's ``.git`` directory (never in a committed file: the Git
host must not be able to claim a migration is under way), converts every note
and commits everything at once. If it fails the files are restored; if it is
killed, the record lets a re-run finish the job, and until then the store is
refused. Encrypting also removes mem-carve output (MEMORY.md, MEMORY-full.md),
which lists every note's title in plaintext. Neither migration rewrites Git
history: earlier commits still hold the plaintext notes (see the README before
pushing to a new host). Nothing here prints key material; ``--dry-run`` prints
file names only.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from memd.codec import (MARKER, CodecError, EncryptedCodec, PlainCodec, codec_for, decode_text,
                        is_encrypted, key_binding, marker_bytes, migration_state, migration_state_path,
                        read_marker)
from memd.crypt import CryptError, generate_key_file, load_key
from memd.store import (CARVE_OUTPUT_FILES, StoreUnavailable, assert_readable_tree, clone_lock,
                        parse_text)

ENCRYPT_MESSAGE = "memd: encrypt store"
DECRYPT_MESSAGE = "memd: decrypt store"

# A file's bytes and permission bits, or None for a file the migration created.
Snapshot = tuple[bytes, int] | None


def _git(clone: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(clone), *args], check=True, capture_output=True,
                          text=True, timeout=120)


def _rel(clone: Path, path: Path) -> str:
    return path.relative_to(clone).as_posix()


def _store_key(clone: Path):
    binding = key_binding(clone)
    if binding.obsidian:
        raise CodecError("Obsidian vault stores cannot be encrypted: the vault holds the notes in plaintext")
    if not binding.key_file:
        raise CodecError(f"no key is configured for this store; create one with `mem-crypt keygen PATH` "
                         f"and set {binding.setting} to it")
    try:
        return binding, load_key(binding.key_file)
    except CryptError as exc:
        raise CodecError(str(exc)) from None


def _require_no_staged(clone: Path) -> None:
    if subprocess.run(["git", "-C", str(clone), "diff", "--cached", "--quiet"],
                      capture_output=True, timeout=30).returncode != 0:
        raise CodecError("the clone has staged changes; commit or unstage them before migrating")


def _safe_target(clone: Path, rel: str) -> Path | None:
    """``clone/rel`` when it is a plain relative note path inside the clone, else None."""
    parts = Path(rel).parts
    if (not rel.endswith(".md") or Path(rel).is_absolute() or not parts
            or any(p in ("", ".", "..") or p.startswith(".") for p in parts)):
        return None
    target = clone / rel
    for parent in Path(rel).parents:
        if (clone / parent).is_symlink():
            return None
    if not target.resolve().is_relative_to(clone.resolve()):
        return None
    return target


def _write_new(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchmod(fd, mode)               # exactly ``mode``, whatever the umask
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _snapshot(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), stat.S_IMODE(os.stat(path).st_mode)


def _replace_marker(clone: Path, data: bytes | None) -> None:
    path = clone / MARKER
    if data is None:
        path.unlink(missing_ok=True)
        return
    temporary = clone / (MARKER + ".tmp")
    temporary.unlink(missing_ok=True)
    _write_new(temporary, data)
    os.replace(temporary, path)


def _resuming(clone: Path, action: str, key) -> bool:
    """True when this clone records an interrupted ``action`` run to finish.

    The record lives in ``.git/`` (``memd.codec.migration_state``), so only a
    run in this clone can have written it, never a commit from the Git host.
    """
    state = migration_state(clone)
    if state is None:
        return False
    if state["action"] != action:
        raise CodecError(f"an interrupted `mem-crypt {state['action']}` must be finished first; "
                         f"re-run `mem-crypt {state['action']}`")
    if state["key_id"] != key.key_id:
        raise CodecError(f"the interrupted `mem-crypt {action}` used another key (key id {state['key_id']}); "
                         f"the configured key has key id {key.key_id}")
    return True


def _begin(clone: Path, action: str, key) -> None:
    path = migration_state_path(clone)
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    _write_new(temporary, json.dumps({"action": action, "key_id": key.key_id}).encode("utf-8"))
    os.replace(temporary, path)


def _end(clone: Path) -> None:
    migration_state_path(clone).unlink(missing_ok=True)


def _commit(clone: Path, added: list[Path], removed: list[Path], message: str) -> str | None:
    """Stage and commit; None when nothing was staged (e.g. a resumed run found it all done)."""
    if added:
        _git(clone, "add", "--", *[_rel(clone, p) for p in added])
    if removed:
        _git(clone, "rm", "-q", "--cached", "--ignore-unmatch", "--", *[_rel(clone, p) for p in removed])
    if subprocess.run(["git", "-C", str(clone), "diff", "--cached", "--quiet"],
                      capture_output=True, timeout=60).returncode == 0:
        return None
    _git(clone, "commit", "-q", "--no-verify", "-m", message)
    return _git(clone, "rev-parse", "HEAD").stdout.strip()


def _deleted(clone: Path, suffix: str) -> list[Path]:
    """Tracked files with ``suffix`` already deleted from the working tree.

    An interrupted run leaves these, and so does a deletion never committed; the
    migration commit removes them from the index too, or it would keep them.
    """
    out = subprocess.run(["git", "-C", str(clone), "ls-files", "--deleted", "-z"],
                         capture_output=True, text=True, timeout=60).stdout
    return [clone / p for p in out.split("\0") if p.endswith(suffix)]


def _rollback(clone: Path, snapshots: dict[Path, Snapshot], marker_before: bytes | None,
              touched: list[Path]) -> None:
    for path, snap in snapshots.items():
        if snap is None:
            path.unlink(missing_ok=True)
        elif not path.exists():
            _write_new(path, *snap)       # its original bytes and mode
    _replace_marker(clone, marker_before)
    rels = [_rel(clone, p) for p in touched]
    if rels:
        subprocess.run(["git", "-C", str(clone), "reset", "-q", "--", *rels],
                       capture_output=True, timeout=60)


def encrypt_store(clone: Path, *, dry_run: bool = False) -> dict:
    """Encrypt every plaintext note of ``clone`` in one commit (or plan it).

    The same commit removes mem-carve output (MEMORY.md, MEMORY-full.md): it
    lists every note's title and first line in plaintext, and an encrypted
    store refuses it. It can be regenerated only for a plaintext store.
    """
    clone = Path(clone)
    binding, key = _store_key(clone)
    with clone_lock(clone):
        assert_readable_tree(clone)
        marker = read_marker(clone)
        if marker and marker["key_id"] != key.key_id:
            raise CodecError(f"this store is encrypted with another key (key id {marker['key_id']}); "
                             f"the configured key has key id {key.key_id}")
        resuming = _resuming(clone, "encrypt", key)
        codec = EncryptedCodec(clone, binding.managed, key)
        plain, sealed, carved = codec._scan()
        stale = [p for p in _deleted(clone, ".md")
                 if p.name in CARVE_OUTPUT_FILES or codec._note_eligible(_rel(clone, p))]
        if marker and not resuming and not plain and not carved and not stale:
            return {"action": "encrypt", "dry_run": dry_run, "changed": False, "key_id": key.key_id,
                    "notes": len(sealed), "files": [], "removed": [], "commit": None,
                    "detail": "store is already encrypted"}
        if not resuming:
            _require_no_staged(clone)
        for path in carved:
            if path.is_symlink() or not path.is_file():
                raise CodecError(f"{_rel(clone, path)} is not a regular file; remove it before encrypting")
        slugs: dict[str, str] = {}
        for path in sealed:
            rel = _rel(clone, path)
            text, name = codec.decode(rel, path.read_bytes())
            slug = parse_text(text, path=name or rel).slug
            slugs.setdefault(slug, rel)
        planned: set[Path] = set()
        plan: list[tuple[Path, str, str, Path]] = []
        for path in plain:
            rel = _rel(clone, path)
            if path.is_symlink() or any((clone / p).is_symlink() for p in Path(rel).parents):
                raise CodecError(f"refusing to encrypt symlinked note {rel}")
            text = decode_text(path.read_bytes())
            slug = parse_text(text, path=rel).slug
            if slug in slugs:
                raise CodecError(f"duplicate note slug {slug!r} ({slugs[slug]} and {rel}); "
                                 "resolve it before encrypting")
            slugs[slug] = rel
            stem, number = key.file_stem(slug), 2
            target = clone / f"{stem}.md.enc"
            while os.path.lexists(target) or target in planned:
                target = clone / f"{stem}_{number}.md.enc"
                number += 1
            planned.add(target)
            plan.append((path, rel, text, target))
        report = {"action": "encrypt", "dry_run": dry_run, "changed": True,
                  "key_id": key.key_id, "notes": len(plan) + len(sealed), "commit": None,
                  "files": [{"from": rel, "to": _rel(clone, t)} for _, rel, _, t in plan],
                  "removed": sorted(_rel(clone, p) for p in [*carved, *stale])}
        if dry_run:
            return report
        marker_path = clone / MARKER
        marker_before = marker_path.read_bytes() if marker_path.exists() else None
        snapshots: dict[Path, Snapshot] = {}
        if not resuming:
            _begin(clone, "encrypt", key)
        try:
            _replace_marker(clone, marker_bytes(key))
            for path, rel, text, target in plan:
                data = codec.encode(_rel(clone, target), text, name=rel)
                snapshots[target] = None
                _write_new(target, data, os.stat(path).st_mode & 0o777)
                snapshots[path] = _snapshot(path)
                path.unlink()
            for path in carved:
                snapshots[path] = _snapshot(path)
                path.unlink()
            report["commit"] = _commit(clone, [marker_path, *sealed, *(t for *_, t in plan)],
                                       [*plain, *carved, *stale], ENCRYPT_MESSAGE)
        except BaseException:
            _rollback(clone, snapshots, marker_before, [marker_path, *sealed, *snapshots, *stale])
            if not resuming:
                _end(clone)
            raise
        _end(clone)
    return report


def decrypt_store(clone: Path, *, dry_run: bool = False) -> dict:
    """Turn an encrypted store back into plaintext ``*.md`` notes in one commit."""
    clone = Path(clone)
    with clone_lock(clone):
        marker = read_marker(clone)
        state = migration_state(clone)
        if marker is None and not (state and state["action"] == "decrypt"):
            raise CodecError("this store is not encrypted")
        binding, key = _store_key(clone)
        resuming = _resuming(clone, "decrypt", key)
        if marker and key.key_id != marker["key_id"]:
            raise CodecError(f"the configured key (key id {key.key_id}) is not this store's key "
                             f"(key id {marker['key_id']})")
        assert_readable_tree(clone)
        if not resuming:
            _require_no_staged(clone)
        codec = EncryptedCodec(clone, binding.managed, key)
        plain, sealed, _ = codec._scan()
        stale = _deleted(clone, ".md.enc")
        plain_codec = PlainCodec(clone, binding.managed)
        taken = {p.resolve() for p in plain}
        plan: list[tuple[Path, str, Path]] = []
        for path in sealed:
            rel = _rel(clone, path)
            text, name = codec.decode(rel, path.read_bytes())
            target = _safe_target(clone, name) if name else None
            if target is None or os.path.lexists(target) or target.resolve() in taken:
                slug = parse_text(text, path=name or "note.md").slug
                target = plain_codec.new_note_path(clone, slug)
                number = 2
                while target.resolve() in taken:
                    target = clone / f"{target.stem}_{number}.md"
                    number += 1
            taken.add(target.resolve())
            plan.append((path, text, target))
        report = {"action": "decrypt", "dry_run": dry_run, "changed": True, "key_id": key.key_id,
                  "notes": len(plan) + len(plain), "commit": None,
                  "files": [{"from": _rel(clone, p), "to": _rel(clone, t)} for p, _, t in plan],
                  "removed": sorted(_rel(clone, p) for p in stale)}
        if dry_run:
            return report
        marker_path = clone / MARKER
        marker_before = marker_path.read_bytes() if marker_path.exists() else None
        snapshots: dict[Path, Snapshot] = {}
        if not resuming:
            _begin(clone, "decrypt", key)
        try:
            for path, text, target in plan:
                snapshots[target] = None
                _write_new(target, text.encode("utf-8"), os.stat(path).st_mode & 0o777)
                snapshots[path] = _snapshot(path)
                path.unlink()
            marker_path.unlink(missing_ok=True)
            report["commit"] = _commit(clone, [*plain, *(t for *_, t in plan)],
                                       [marker_path, *(p for p, *_ in plan), *stale], DECRYPT_MESSAGE)
        except BaseException:
            _rollback(clone, snapshots, marker_before, [marker_path, *plain, *snapshots, *stale])
            if not resuming:
                _end(clone)
            raise
        _end(clone)
    return report


def status(clone: Path) -> dict:
    clone = Path(clone)
    out = {"clone": str(clone), "encrypted": is_encrypted(clone), "ok": True, "key_id": None,
           "migrating": False, "detail": ""}
    try:
        state = migration_state(clone)
        out["migrating"] = state is not None
        codec = codec_for(clone)
        out["key_id"] = codec.key_id
        out["notes"] = len(codec.note_files())
        out["detail"] = "notes encrypted (AES-256-GCM)" if codec.encrypted else "plaintext store"
        if state:
            out["detail"] += (f"; an interrupted `mem-crypt {state['action']}` left a record in .git/: "
                              f"re-run `mem-crypt {state['action']}` to finish it")
    except StoreUnavailable as exc:
        out.update(ok=False, detail=str(exc))
    return out


def _refresh_index(cfg) -> str | None:
    """Re-read the converted notes into the index; a warning on failure."""
    try:
        from memd.index import open_db, refresh_lexical
        db = open_db(cfg.db, dim=cfg.embed_dim)
        try:
            with clone_lock(cfg.clone):
                refresh_lexical(db, cfg)
        finally:
            db.close()
        from memd.refresh import request_refresh
        request_refresh(cfg)
        return None
    except Exception as exc:  # the commit stands; `mem reindex` catches up
        return f"index refresh pending ({type(exc).__name__}: {exc}); run `mem reindex`"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mem-crypt", description="Encryption keys and store migration.")
    sub = ap.add_subparsers(dest="command", required=True)
    keygen = sub.add_parser("keygen", help="write a new random key file (mode 0600)")
    keygen.add_argument("path")
    sub.add_parser("status", help="show the configured store's encryption state")
    for name, text in (("encrypt", "encrypt every plaintext note in one commit"),
                       ("decrypt", "decrypt every note back to plaintext in one commit")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
        p.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if args.command == "keygen":
        try:
            key_id = generate_key_file(args.path)
        except FileExistsError:
            print(f"mem-crypt: {args.path} exists; refusing to replace a key", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"mem-crypt: cannot write {args.path}: {exc.strerror}", file=sys.stderr)
            return 1
        print(f"wrote key {key_id} to {args.path} (mode 0600). Back it up now, apart from the "
              "store: losing it loses every note encrypted with it.")
        return 0

    from memd.config import Config
    cfg = Config.from_env()
    if cfg.clone is None or not (Path(cfg.clone) / ".git").exists():
        print("mem-crypt: no clone configured (MEMD_CLONE / MEMD_PROFILE)", file=sys.stderr)
        return 2
    clone = Path(cfg.clone)
    if args.command == "status":
        report = status(clone)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    try:
        report = (encrypt_store if args.command == "encrypt" else decrypt_store)(clone, dry_run=args.dry_run)
    except (StoreUnavailable, CryptError, subprocess.CalledProcessError, UnicodeDecodeError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else exc
        print(f"mem-crypt: {args.command} failed: {detail}", file=sys.stderr)
        return 1
    if report["commit"] and args.command == "encrypt":
        report["warning"] = _refresh_index(cfg)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    for row in report["files"]:
        print(f"  {row['from']} -> {row['to']}")
    for rel in report.get("removed", []):
        print(f"  {rel} removed")
    if args.dry_run:
        print(f"dry-run: would {args.command} {len(report['files'])} note(s); nothing changed.")
    elif report["commit"]:
        print(f"{args.command}ed {len(report['files'])} note(s) in commit {report['commit'][:12]}.")
        if args.command == "encrypt":
            print("Earlier commits still hold the plaintext notes; publish the encrypted store to a "
                  "new repository with fresh history (see the README).")
            if report.get("warning"):
                print(f"warning: {report['warning']}")
        else:
            print("Remove the store's key setting now; the store is plaintext again. Then run "
                  "`mem reindex`.")
    else:
        print(report.get("detail") or "nothing to do.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
