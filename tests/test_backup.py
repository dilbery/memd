"""mem-backup: encrypted bundles, tamper detection, verified restore and drills.

Hermetic: temp git clones, temp key files and temp output directories; no
network. Two stores: ``amber`` (plaintext, the shared 3-note clone with a review
branch) and ``cobalt`` (encrypted with its own store key).
"""
import json
import os
import sqlite3
import subprocess
import tempfile
import threading

import pytest

import memd.save as save_mod
from memd import backup
from memd.cli_crypt import encrypt_store
from memd.crypt import KeyFileError, generate_key_file
from memd.registry import Registry
from tests.conftest import NOTE_CORE, NOTE_TUNING


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _side_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, text TEXT)")
    conn.executemany("INSERT INTO items(text) VALUES (?)", [(r,) for r in rows])
    conn.commit()
    conn.close()


@pytest.fixture
def world(config, tmp_path, monkeypatch):
    """amber (plain, review branch, inbox + usage), cobalt (encrypted), a token registry."""
    monkeypatch.setattr(save_mod, "_pull_rebase_push", lambda clone: None)
    amber = config.clone
    _git(amber, "checkout", "-q", "-b", "memd/review-1")
    (amber / "proposal.md").write_text(NOTE_CORE.replace("repo-hosting-policy", "proposal-x"))
    _git(amber, "add", "-A")
    _git(amber, "commit", "-q", "-m", "proposal")
    _git(amber, "checkout", "-q", "-")
    _side_db(tmp_path / "memd.inbox.db", ["candidate one", "candidate two"])
    _side_db(tmp_path / "memd.usage.db", ["recall a"])

    cobalt = tmp_path / "cobalt" / "clone"
    cobalt.mkdir(parents=True)
    _git(cobalt, "init", "-q")
    _git(cobalt, "config", "user.email", "test@memd")
    _git(cobalt, "config", "user.name", "memd-test")
    (cobalt / "tuning.md").write_text(NOTE_TUNING)
    _git(cobalt, "add", "-A")
    _git(cobalt, "commit", "-q", "-m", "seed")
    store_key = tmp_path / "keys" / "cobalt.key"
    generate_key_file(store_key)
    monkeypatch.setenv("MEMD_COBALT_CLONE", str(cobalt))
    monkeypatch.setenv("MEMD_COBALT_DB", str(tmp_path / "cobalt" / "memd.db"))
    monkeypatch.setenv("MEMD_COBALT_KEY_FILE", str(store_key))
    encrypt_store(cobalt)

    registry = tmp_path / "control" / "registry.db"
    monkeypatch.setenv("MEMD_CONTROL_DB", str(registry))
    reg = Registry(registry)
    reg.initialize()
    reg.session_create({"user": "someone"})

    backup_key = tmp_path / "keys" / "backup.key"
    generate_key_file(backup_key)
    out = tmp_path / "backups"
    monkeypatch.setenv("MEMD_BACKUP_KEY_FILE", str(backup_key))
    monkeypatch.setenv("MEMD_BACKUP_DIR", str(out))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return {"amber": amber, "cobalt": cobalt, "store_key": store_key, "backup_key": backup_key,
            "out": out, "registry": registry, "scratch": scratch, "tmp": tmp_path,
            "key": backup.load_backup_key(backup_key)}


def _create(world, **kw):
    kw.setdefault("chunk_size", backup.MIN_CHUNK)
    return backup.create(out_dir=world["out"], key=world["key"], **kw)


def _plaintext(path, key):
    with open(path, "rb") as fh:
        reader = backup._Reader(fh, key)
        data = bytearray()
        while block := reader.read(1 << 16):
            data += block
    return bytes(data)


# --------------------------------------------------------------------------- round trip


def test_create_restore_round_trip_plain_and_encrypted(world, tmp_path):
    path, manifest = _create(world)
    assert path.parent == world["out"] and oct(path.stat().st_mode & 0o777) == "0o600"
    stores = {s["name"]: s for s in manifest["stores"]}
    assert set(stores) == {"amber", "cobalt"}
    assert stores["amber"]["head"] == _git(world["amber"], "rev-parse", "HEAD")
    assert "refs/heads/memd/review-1" in stores["amber"]["refs"]
    assert stores["amber"]["notes"] == 3 and not stores["amber"]["encrypted"]
    assert set(stores["amber"]["members"]) == {"bundle", "inbox", "usage"}
    assert stores["cobalt"]["encrypted"] and stores["cobalt"]["notes"] == 1
    assert stores["cobalt"]["key_file"] == str(world["store_key"])
    assert stores["cobalt"]["key_setting"] == "MEMD_COBALT_KEY_FILE"
    assert set(manifest["control"]) == {"registry"}

    target = tmp_path / "restored"
    restored = backup.restore(path, world["key"], target)
    assert restored["members"] == manifest["members"]
    assert json.loads((target / "manifest.json").read_text())["created"] == manifest["created"]
    for store, clone in (("amber", world["amber"]), ("cobalt", world["cobalt"])):
        entry = stores[store]
        copy = tmp_path / f"copy-{store}"
        subprocess.run(["git", "clone", "-q", str(target / entry["members"]["bundle"]), str(copy)],
                       check=True, capture_output=True)
        assert _git(copy, "rev-parse", "HEAD") == _git(clone, "rev-parse", "HEAD")
        assert sorted(_git(copy, "ls-files").split()) == sorted(_git(clone, "ls-files").split())
    review = _git(tmp_path / "copy-amber", "rev-parse", "origin/memd/review-1")
    assert review == _git(world["amber"], "rev-parse", "memd/review-1")
    # the encrypted store stays ciphertext in the bundle
    assert "Lemonade" not in (tmp_path / "copy-cobalt" / _git(tmp_path / "copy-cobalt", "ls-files",
                                                             "*.md.enc")).read_text()
    inbox = sqlite3.connect(target / stores["amber"]["members"]["inbox"])
    assert [r[0] for r in inbox.execute("SELECT text FROM items ORDER BY id")] == ["candidate one",
                                                                                 "candidate two"]
    inbox.close()
    reg = sqlite3.connect(target / "control" / "registry.db")
    assert reg.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0   # sessions dropped
    reg.close()
    assert backup.verify(path, world["key"])["members"] == manifest["members"]


def test_index_is_not_in_the_bundle(world):
    (world["tmp"] / "memd.db").write_bytes(b"index-bytes-that-must-not-be-backed-up")
    path, manifest = _create(world)
    assert not any(m["path"].endswith("memd.db") for m in manifest["members"])
    assert b"index-bytes-that-must-not-be-backed-up" not in _plaintext(path, world["key"])


def test_no_key_material_in_the_bundle(world):
    path, _ = _create(world)
    plain = _plaintext(path, world["key"])
    raw = path.read_bytes()
    for key_file in (world["store_key"], world["backup_key"]):
        hex_key = key_file.read_text().strip()
        key_bytes = bytes.fromhex(hex_key)
        for blob in (plain, raw):
            assert hex_key.encode() not in blob and hex_key.upper().encode() not in blob
            assert key_bytes not in blob


# --------------------------------------------------------------------------- tamper detection


def _chunks(path):
    size = backup.MIN_CHUNK + backup.TAG_BYTES
    data = path.read_bytes()
    return data[:backup.HEADER.size], [data[i:i + size] for i in range(backup.HEADER.size, len(data), size)]


@pytest.fixture
def bundle(world):
    path, _ = _create(world)
    header, chunks = _chunks(path)
    assert len(chunks) >= 4, "the test bundle should span several chunks"
    return path


def _refused(path, key, match):
    with pytest.raises(backup.BackupError, match=match):
        backup.verify(path, key)


def test_a_bit_flip_in_any_chunk_or_the_header_is_detected(world, bundle):
    original = bundle.read_bytes()
    header, chunks = _chunks(bundle)
    starts = [len(header) + i * (backup.MIN_CHUNK + backup.TAG_BYTES) for i in range(len(chunks))]
    offsets = [0, 7, 8, 20, len(header) - 1]            # magic, version, key id, salt, chunk size
    for start, chunk in zip(starts, chunks):
        offsets += [start, start + len(chunk) // 2, start + len(chunk) - 1]
    for offset in offsets:
        data = bytearray(original)
        data[offset] ^= 0x01
        bundle.write_bytes(bytes(data))
        with pytest.raises(backup.BackupError):
            backup.verify(bundle, world["key"])
    bundle.write_bytes(original)
    backup.verify(bundle, world["key"])


def test_truncation_reordering_and_trailing_bytes_are_detected(world, bundle):
    original = bundle.read_bytes()
    header, chunks = _chunks(bundle)
    bundle.write_bytes(header + b"".join(chunks[:-1]))                      # dropped final chunk
    _refused(bundle, world["key"], "authentication")
    bundle.write_bytes(original[:-7])                                       # cut mid-chunk
    _refused(bundle, world["key"], "authentication|truncated")
    bundle.write_bytes(original[:backup.HEADER.size - 3])                   # cut inside the header
    _refused(bundle, world["key"], "too short")
    bundle.write_bytes(header + chunks[1] + chunks[0] + b"".join(chunks[2:]))   # reordered
    _refused(bundle, world["key"], "chunk 0")
    bundle.write_bytes(header + b"".join(chunks[:2] + chunks[1:]))          # a chunk replayed
    _refused(bundle, world["key"], "authentication")
    bundle.write_bytes(original + b"\x00" * 40)                             # trailing bytes
    _refused(bundle, world["key"], "authentication")


def test_a_wrong_key_is_refused(world, bundle, tmp_path):
    other = tmp_path / "keys" / "other.key"
    generate_key_file(other)
    _refused(bundle, backup.load_backup_key(other), "another backup key")
    # the same key id with other key bytes is still an authentication failure
    forged = backup.BackupKey(os.urandom(32), world["key"].key_id)
    _refused(bundle, forged, "authentication")


def test_a_loose_backup_key_file_is_refused(world, capsys):
    os.chmod(world["backup_key"], 0o644)
    with pytest.raises(KeyFileError, match="0600"):
        backup.load_backup_key(world["backup_key"])
    assert backup.main(["create"]) == 1
    assert "0600" in capsys.readouterr().err
    assert not backup.bundles(world["out"])


def test_keygen_never_overwrites(world, tmp_path, capsys):
    fresh = tmp_path / "keys" / "fresh.key"
    assert backup.main(["--key-file", str(fresh), "keygen"]) == 0
    assert oct(fresh.stat().st_mode & 0o777) == "0o600"
    before = fresh.read_bytes()
    assert backup.main(["--key-file", str(fresh), "keygen"]) == 1
    assert fresh.read_bytes() == before and "never overwritten" in capsys.readouterr().err


# --------------------------------------------------------------------------- restore and retention


def test_restore_refuses_a_non_empty_target_and_cleans_up_a_failed_one(world, bundle, tmp_path):
    target = tmp_path / "live"
    target.mkdir()
    (target / "notes.md").write_text("live data")
    with pytest.raises(backup.BackupError, match="not empty"):
        backup.restore(bundle, world["key"], target)
    assert [p.name for p in target.iterdir()] == ["notes.md"]
    (tmp_path / "afile").write_text("x")
    with pytest.raises(backup.BackupError, match="not a directory"):
        backup.restore(bundle, world["key"], tmp_path / "afile")
    # a bundle that fails part way leaves the (empty) target as it was
    empty = tmp_path / "empty"
    empty.mkdir()
    bundle.write_bytes(bundle.read_bytes()[:-40])
    with pytest.raises(backup.BackupError):
        backup.restore(bundle, world["key"], empty)
    assert empty.is_dir() and not any(empty.iterdir())
    with pytest.raises(backup.BackupError):
        backup.restore(bundle, world["key"], tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_retention_prunes_the_oldest(world, monkeypatch, capsys):
    made = [_create(world, keep=3)[0] for _ in range(5)]
    left = backup.bundles(world["out"])
    assert left == made[-3:]
    monkeypatch.setenv("MEMD_BACKUP_KEEP", "1")
    assert backup.main(["create"]) == 0
    left = backup.bundles(world["out"])
    assert len(left) == 1 and left[0] not in made
    assert not [p for p in world["out"].iterdir() if p.name.startswith(".")]   # no staging/partials
    with pytest.raises(backup.BackupError):
        backup.keep_count("0")
    assert backup.keep_count(None) == 14


# --------------------------------------------------------------------------- drill


def test_drill_passes_and_removes_its_scratch_dir(world, capsys):
    _create(world)
    assert backup.main(["drill"]) == 0
    out = capsys.readouterr().out
    assert "drill passed" in out
    assert "amber: clone" in out and "cobalt: clone" in out and "amber: index and recall" in out
    assert "sample recall found" in out and "control: registry db" in out
    assert not any(world["scratch"].iterdir())


def test_drill_rebuilds_an_encrypted_store_with_its_key(world, monkeypatch):
    stores = [s for s in backup.configured_stores() if s.name == "cobalt"]
    path, _ = _create(world, stores=stores)
    results = backup.drill(path, world["key"], out=lambda *_: None)
    assert all(ok for _, ok, _ in results), results
    assert any(name == "cobalt: index and recall" for name, _, _ in results)


def test_drill_detects_a_corrupted_bundle(world, capsys):
    path, _ = _create(world)
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x40
    path.write_bytes(bytes(data))
    assert backup.main(["drill"]) == 1
    captured = capsys.readouterr()
    assert "FAILED" in captured.out and "authentication" in captured.out
    assert "drill FAILED" in captured.err
    assert not any(world["scratch"].iterdir())


def test_drill_detects_a_manifest_that_does_not_match_the_repository(world, monkeypatch, capsys):
    real = backup.note_count
    monkeypatch.setattr(backup, "note_count", lambda repo, rev: real(repo, rev) + 1)
    _create(world)
    monkeypatch.setattr(backup, "note_count", real)
    assert backup.main(["drill"]) == 1
    assert "manifest says" in capsys.readouterr().out


def test_verify_cli_and_missing_configuration(world, monkeypatch, capsys):
    path, _ = _create(world)
    assert backup.main(["verify", str(path)]) == 0
    assert "is intact" in capsys.readouterr().out
    monkeypatch.delenv("MEMD_BACKUP_DIR")
    assert backup.main(["create"]) == 1
    assert "MEMD_BACKUP_DIR" in capsys.readouterr().err
    monkeypatch.delenv("MEMD_BACKUP_KEY_FILE")
    assert backup.main(["drill"]) == 1
    assert "MEMD_BACKUP_KEY_FILE" in capsys.readouterr().err


# --------------------------------------------------------------------------- consistency


def test_side_db_snapshots_are_consistent_under_a_concurrent_writer(world, tmp_path):
    """Each writer transaction adds two rows and bumps a counter: a torn copy breaks that."""
    usage = tmp_path / "memd.usage.db"
    conn = sqlite3.connect(usage)
    conn.execute("CREATE TABLE ledger(id INTEGER PRIMARY KEY, n INTEGER)")
    conn.execute("CREATE TABLE counter(total INTEGER)")
    conn.execute("INSERT INTO counter VALUES (0)")
    conn.commit()
    conn.close()
    stop = threading.Event()
    errors = []

    def writer():
        w = sqlite3.connect(usage, timeout=30)
        try:
            i = 0
            while not stop.is_set():
                i += 1
                with w:
                    w.execute("INSERT INTO ledger(n) VALUES (?)", (i,))
                    w.execute("INSERT INTO ledger(n) VALUES (?)", (-i,))
                    w.execute("UPDATE counter SET total = total + 1")
        except Exception as exc:   # pragma: no cover - reported below
            errors.append(exc)
        finally:
            w.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        made = [_create(world, keep=10)[0] for _ in range(4)]
    finally:
        stop.set()
        thread.join()
    assert not errors
    totals = []
    for i, path in enumerate(made):
        target = tmp_path / f"r{i}"
        manifest = backup.restore(path, world["key"], target)
        amber = next(s for s in manifest["stores"] if s["name"] == "amber")
        snap = sqlite3.connect(target / amber["members"]["usage"])
        rows = snap.execute("SELECT count(*) FROM ledger").fetchone()[0]
        total = snap.execute("SELECT total FROM counter").fetchone()[0]
        assert snap.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        snap.close()
        assert rows == 2 * total
        totals.append(total)
    assert totals == sorted(totals) and totals[-1] > 0
