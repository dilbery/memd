"""Note encryption for personal stores: key files, subkeys and the file envelope.

An encrypted store keeps every note in Git as ciphertext, so the repository can
live on any Git host. Only the memd server holding the store's key decrypts it;
the derived local index stays plaintext. This module holds the primitives and no
store logic (memd.codec is the seam every clone read and write goes through).

* Key: 32 random bytes in a key file (64 hex characters, or the raw 32 bytes),
  mode 0600 or stricter. ``load_key`` refuses anything looser.
* Subkeys: HKDF-SHA256 derives separate keys for note encryption, for opaque
  filenames and for the public key id, so the key file itself is used only as
  HKDF input keying material.
* Cipher: AES-256-GCM with a random 96-bit nonce per write.
* Envelope: ``MAGIC | VERSION | key id (8) | nonce (12) | ciphertext+tag``,
  base64-armoured on one line so no Git text conversion can alter it. The
  associated data is the header plus the file's path in the clone, so a
  ciphertext moved to another path (or another key id) fails authentication.

Nothing here logs, and no error message carries key material or plaintext.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"MEMDENC"
VERSION = 1
KEY_BYTES = 32
KEY_ID_BYTES = 8
NONCE_BYTES = 12
HEADER_BYTES = len(MAGIC) + 1 + KEY_ID_BYTES
# Hex characters of HMAC-SHA256(filename key, slug) used as an encrypted note's
# file name: 128 bits, so distinct slugs never share a name in practice.
NAME_HEX = 32
_AD_PREFIX = b"memd-note-path\x00"


class CryptError(ValueError):
    """A key or envelope problem. Messages never include key bytes or plaintext."""


class KeyFileError(CryptError):
    """The key file is missing, unreadable, malformed or too permissive."""


class EnvelopeError(CryptError):
    """A stored file is not a valid envelope, or it failed authentication."""


def _hkdf(key: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(key)


@dataclass(frozen=True)
class StoreKey:
    """Subkeys derived from one store key. ``repr`` never shows key bytes."""

    enc: bytes = field(repr=False)
    names: bytes = field(repr=False)
    key_id: str = ""

    @classmethod
    def from_bytes(cls, key: bytes) -> "StoreKey":
        if not isinstance(key, bytes) or len(key) != KEY_BYTES:
            raise KeyFileError("a store key must be exactly 32 bytes")
        return cls(enc=_hkdf(key, b"memd note encryption v1"),
                   names=_hkdf(key, b"memd note filenames v1"),
                   key_id=_hkdf(key, b"memd key id v1", KEY_ID_BYTES).hex())

    def file_stem(self, slug: str) -> str:
        """The opaque file stem for a note identity: an HMAC prefix, never the slug."""
        return hmac.new(self.names, slug.encode("utf-8"), hashlib.sha256).hexdigest()[:NAME_HEX]


def generate_key_file(path: str | Path) -> str:
    """Write a new random key (hex) to ``path`` with mode 0600; returns its key id.

    The file is created exclusively: an existing key is never overwritten,
    because replacing a key makes every note encrypted with it unreadable.
    """
    path = Path(path)
    key = secrets.token_bytes(KEY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, key.hex().encode("ascii") + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    return StoreKey.from_bytes(key).key_id


def load_key(path: str | Path) -> StoreKey:
    """Read and check a key file: a regular file, mode 0600 or stricter, 32 bytes."""
    return StoreKey.from_bytes(load_key_bytes(path))


def load_key_bytes(path: str | Path) -> bytes:
    """The raw 32 key bytes of a checked key file (same rules as ``load_key``).

    For callers that derive their own subkeys (memd.backup); never log the result.
    """
    path = Path(path)
    try:
        info = os.stat(path)
    except OSError as exc:
        raise KeyFileError(f"key file {path} cannot be read: {exc.strerror or type(exc).__name__}") from None
    if not stat.S_ISREG(info.st_mode):
        raise KeyFileError(f"key file {path} is not a regular file")
    if info.st_mode & 0o077:
        raise KeyFileError(f"key file {path} has mode {stat.S_IMODE(info.st_mode):04o}; "
                           "it must be 0600 or stricter (chmod 600)")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise KeyFileError(f"key file {path} cannot be read: {exc.strerror or type(exc).__name__}") from None
    text = data.strip()
    if len(text) == 2 * KEY_BYTES:
        try:
            return binascii.unhexlify(text)
        except (binascii.Error, ValueError):
            pass
    if len(data) == KEY_BYTES:
        return data
    raise KeyFileError(f"key file {path} must hold 32 random bytes (64 hex characters); "
                       "create one with `mem-crypt keygen`")


def _associated_data(header: bytes, rel_path: str) -> bytes:
    return header + _AD_PREFIX + rel_path.encode("utf-8")


def seal(key: StoreKey, rel_path: str, payload: dict) -> bytes:
    """Encrypt ``payload`` (JSON) for the file at ``rel_path``; returns the file bytes."""
    header = MAGIC + bytes([VERSION]) + bytes.fromhex(key.key_id)
    nonce = secrets.token_bytes(NONCE_BYTES)
    plaintext = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ciphertext = AESGCM(key.enc).encrypt(nonce, plaintext, _associated_data(header, rel_path))
    return base64.b64encode(header + nonce + ciphertext) + b"\n"


def envelope_key_id(data: bytes) -> str:
    """The key id in a stored envelope's header (no decryption)."""
    raw = _unarmour(data)
    return raw[len(MAGIC) + 1:HEADER_BYTES].hex()


def _unarmour(data: bytes) -> bytes:
    try:
        raw = base64.b64decode(data.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise EnvelopeError("not a memd encrypted note (bad armour)") from None
    if len(raw) < HEADER_BYTES + NONCE_BYTES + 16 or not raw.startswith(MAGIC):
        raise EnvelopeError("not a memd encrypted note (bad header)")
    if raw[len(MAGIC)] != VERSION:
        raise EnvelopeError(f"unsupported encrypted note version {raw[len(MAGIC)]}")
    return raw


def open_sealed(key: StoreKey, rel_path: str, data: bytes) -> dict:
    """Authenticate and decrypt the file at ``rel_path``; returns its payload."""
    raw = _unarmour(data)
    header = raw[:HEADER_BYTES]
    stored_id = header[len(MAGIC) + 1:].hex()
    if stored_id != key.key_id:
        raise EnvelopeError(f"{rel_path} was encrypted with a different key (key id {stored_id}, "
                            f"configured key id {key.key_id})")
    nonce = raw[HEADER_BYTES:HEADER_BYTES + NONCE_BYTES]
    try:
        plaintext = AESGCM(key.enc).decrypt(nonce, raw[HEADER_BYTES + NONCE_BYTES:],
                                            _associated_data(header, rel_path))
    except InvalidTag:
        raise EnvelopeError(f"{rel_path} failed authentication: it was modified, moved from "
                            "another path, or encrypted with another key") from None
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EnvelopeError(f"{rel_path} has an unreadable payload") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise EnvelopeError(f"{rel_path} has an unreadable payload")
    return payload
