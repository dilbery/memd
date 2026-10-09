"""Per-user stores under one root (Plan 2, §9a): naming and path resolution.

Today one memd process locks one profile and serves one clone. Plan 2 gives
every user their own store at ``<MEMD_STORES_ROOT>/<store>/{clone,memd.db}``,
selected from the AUTHENTICATED IDENTITY and never from a request field.

This module owns the boundary between an identity string and the filesystem.
A store name becomes exactly ONE path segment, so everything that could make it
more than one, or make it escape the root, is refused here. Two layers, both
deliberate:

1. A strict lower-case allowlist. Anything outside it is refused outright
   rather than sanitised, because silently rewriting an identity is how one
   person ends up with two stores, or two people with one.
2. A resolved-parent check in :func:`store_paths`. The allowlist should already
   have caught it; this is the line that still holds if the allowlist is ever
   widened carelessly, and it also covers platform path oddities (Windows
   drive-relative names, trailing separators) the regex was not written for.

MEMD_STORES_ROOT unset means single-tenant: the shipped ``amber``/``cobalt``
registry and the pilot deployment behave exactly as before. Presence of the
variable is what turns multi-tenancy on, so no existing deployment changes
behaviour by upgrading the code alone.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# One segment of a store name. Lower case only (the caller normalises first),
# and every character here is safe in a POSIX path component and in a Windows
# one except ':' and '\\', which are excluded. '@', '.', '+', '-', '_' and '%'
# are permitted so an RFC 5322 local part and a domain both survive intact.
#
# The leading character may not be '.', which removes '.', '..' and every
# dotfile (notably '.git') in one rule.
_STORE_NAME_RE = re.compile(r"^[a-z0-9_][a-z0-9._%+@-]*$")

# Long enough for any real email, short enough to stay inside the filesystem's
# per-component limit (255 bytes on ext4) with the '/clone' suffix to come.
MAX_STORE_NAME = 200

# MEMD_STORES_LOCAL_DOMAIN names the one email domain whose users get a short
# `u-<localpart>` repo name. Anything else keeps its domain in the slug so two
# people called `sam` at different domains cannot collide. Unset means every
# store keeps its domain.
_LOCAL_DOMAIN_ENV = "MEMD_STORES_LOCAL_DOMAIN"


def local_domain() -> str:
    """The configured short-name email domain, lower-cased, or ''."""
    return (os.environ.get(_LOCAL_DOMAIN_ENV) or "").strip().lower()

# A Forgejo repository name allows letters, digits, dash, underscore and dot.
# Notably NOT "@", which is why a store name and a repo name can never be the
# same string and why this module owns the conversion.
_REPO_UNSAFE = re.compile(r"[^a-z0-9._-]+")

CLONE_DIRNAME = "clone"
DB_FILENAME = "memd.db"

_ROOT_ENV = "MEMD_STORES_ROOT"


class InvalidStoreName(ValueError):
    """An identity did not yield a usable single-segment store name."""


def store_name(raw: object) -> str:
    """Normalise and validate `raw` into a store name, or raise.

    The single call site for turning an identity into a name. Normalisation is
    only case folding and surrounding whitespace: nothing else is rewritten, so
    a name that would have needed rewriting is a refusal, not a silent change.
    """
    if not isinstance(raw, str):
        raise InvalidStoreName(f"store name must be a string, got {type(raw).__name__}")
    name = raw.strip().lower()
    if not name:
        raise InvalidStoreName("store name is empty")
    if len(name) > MAX_STORE_NAME:
        raise InvalidStoreName(
            f"store name is {len(name)} characters, limit is {MAX_STORE_NAME}"
        )
    if not _STORE_NAME_RE.match(name):
        # Deliberately does not echo the whole rejected value: it can be a
        # control-character probe, and this string reaches logs.
        raise InvalidStoreName(
            "store name must match [a-z0-9_][a-z0-9._%+@-]* "
            "(no path separators, no leading dot, no whitespace)"
        )
    return name


def store_dirname(name: str) -> str:
    """The directory segment for a validated store name.

    Identity and directory are the same string today: §9b makes the lower-cased
    email the store name, and keeping the directory equal to it means the
    filesystem, the audit trail and the token all read the same. §9a's sketch
    used ``u-<username>`` instead. This seam is where that choice lives, so
    changing it is one function and its tests, not a migration of call sites.
    """
    return name


def repo_slug(name: str) -> str:
    """The Forgejo repository name for a store.

    ONE derivation, imported by both sides. memd wires each clone's remote from
    it, and the onboarding job creates the repository from it. Two independent
    derivations is exactly the bug this replaces: the job created
    `memory/u-alice` while memd pointed the clone at
    `memory/alice@corp.example.com.git`, so every save reported
    `synced: false` forever and nothing ever reached Forgejo. Nothing failed
    loudly; it simply never synced.

    STABLE BY CONTRACT: every repository already created is named by this
    function, so changing it strands them and becomes a migration.
    """
    store = store_name(name)
    local, _, domain = store.partition("@")
    base = local if domain and domain == local_domain() else store.replace("@", "-at-")
    slug = _REPO_UNSAFE.sub("-", base).strip("-.")
    if not slug:
        raise InvalidStoreName(f"store {name!r} does not yield a usable repo name")
    return f"u-{slug}"


def stores_root_from(env: dict[str, str]) -> Path | None:
    """The multi-tenant root according to `env`, or None.

    Takes an explicit mapping so Config.from_env(env) stays hermetic: the test
    suite passes a dict that os.environ never sees.
    """
    raw = (env.get(_ROOT_ENV) or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def stores_root() -> Path | None:
    """The multi-tenant root, or None when this instance is single-tenant."""
    return stores_root_from(os.environ)


def is_multitenant() -> bool:
    """True when MEMD_STORES_ROOT is configured."""
    return stores_root() is not None


def store_paths(name: str) -> tuple[Path, Path]:
    """Return ``(clone, db)`` for `name` under the stores root.

    Raises InvalidStoreName for a bad name and RuntimeError when this instance
    is not multi-tenant, so a caller can never silently resolve a per-user
    store against an unconfigured root.
    """
    root = stores_root()
    if root is None:
        raise RuntimeError(
            f"{_ROOT_ENV} is not set: this instance serves a single store"
        )
    return store_paths_under(root, name)


def store_paths_under(root: Path, name: str) -> tuple[Path, Path]:
    """Return ``(clone, db)`` for `name` under an explicitly given root."""
    root = Path(root).expanduser().resolve()
    directory = (root / store_dirname(store_name(name))).resolve()
    # The allowlist should make this unreachable. It is kept because it is the
    # property that actually matters -- a store lives directly under the root --
    # stated in terms of the resolved path rather than the input string.
    if directory.parent != root:
        raise InvalidStoreName(
            f"store {name!r} does not resolve to a direct child of {root}"
        )
    return directory / CLONE_DIRNAME, directory / DB_FILENAME


def known_stores() -> list[str]:
    """Store names that already exist on disk under the root, sorted.

    Only used for reporting and for enumerating registered roots. Access is
    NEVER decided by membership of this list: a store the caller is entitled to
    may not exist yet (first save creates it), and a store that does exist
    confers nothing on anyone else.
    """
    root = stores_root()
    if root is None or not root.is_dir():
        return []
    out = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            out.append(store_name(child.name))
        except InvalidStoreName:
            continue
    return sorted(out)
