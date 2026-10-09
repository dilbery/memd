"""Per-profile hard isolation (design §9.5).

Each profile maps to a SEPARATE Git repo, clone path, sqlite DB, and
credential. Isolation is enforced here at the memd layer, not by a soft
frontmatter tag: an Amber-profile operation can never read a Cobalt path, and a
recall bound to one profile can never be handed another profile's clone/DB.

The guard is DENY-BY-DEFAULT: a profile may touch ONLY paths inside its own
registered root (its resolved clone/db, or an explicit per-call override root).
Anything outside the union of registered roots is refused — this closes the
sibling-directory and symlink bypasses where a path was "not owned by another
profile" yet still escaped the owner's tree.
"""
from __future__ import annotations

import os
from pathlib import Path

def _legacy() -> dict[str, str]:
    """Legacy registry: profile -> env var prefix (MEMD_LEGACY_PROFILES)."""
    from memd.config import legacy_prefix, legacy_profiles
    return {name: legacy_prefix(name) for name in legacy_profiles()}


def _default() -> str:
    from memd.config import default_profile
    return default_profile(effective_environment())


def registry():
    """Legacy profiles plus stores provisioned in the persistent control database."""
    from memd.control import stores
    return {**_legacy(), **{key: "MEMD_" + key.upper().replace("-", "_") for key in stores()}}


class UnknownProfile(KeyError):
    """An unregistered profile was requested."""


class CrossProfileViolation(PermissionError):
    """A path belonging to a different profile (or outside all roots) was touched."""


class ProfileMismatch(PermissionError):
    """A request attempted to override an instance's configured profile lock."""


def locked_profile() -> str | None:
    """Return the instance lock, shared by REST and every MCP transport."""
    env = effective_environment()
    if env.get("MEMD_ENFORCE_PROFILE", "").strip().lower() in {
        "1", "true", "yes", "on"
    }:
        return env.get("MEMD_PROFILE", "").strip() or _default()
    return None


def resolve_profile(requested: str | None = None) -> str:
    """Select the store for this call: identity first, then request, then lock.

    Three cases, in priority order.

    1. **A bound identity wins outright** (Plan 2, §9a). The store is the
       caller's own. A `requested` value is accepted only when it names that
       same store, and refused otherwise -- including when it names a legacy
       profile, and including when the instance lock says something else. This
       is the property: no request field moves a caller off their own store.
       The instance lock is deliberately ignored here because it is a
       single-tenant concept; honouring it would serve every authenticated user
       the one locked store.

    2. **Unbound, multi-tenant.** The sync job, admin REST and the CLI have no
       OIDC identity. They may name any VALID store, whether or not it exists
       yet, because piece 3's onboarding job has to initialise one. Validity is
       still enforced, so a traversal attempt is refused rather than resolved.

    3. **Unbound, single-tenant.** Exactly the shipped behaviour: default to the
       serving instance and enforce its lock before any I/O.
    """
    from memd import access
    from memd.identity import current_identity
    from memd.stores import InvalidStoreName, is_multitenant, store_name

    # An administered account (not a legacy file token) is confined by its store
    # grants in access.authorize, not by the single-instance lock.
    principal = access.current.get()
    account = bool(principal and not principal.id.startswith("legacy:"))

    requested = requested.strip() if isinstance(requested, str) else None

    identity = current_identity()
    if identity is not None:
        if requested:
            try:
                asked = store_name(requested)
            except InvalidStoreName as exc:
                raise ProfileMismatch(
                    f"caller may only address its own store {identity!r}"
                ) from exc
            if asked != identity:
                raise ProfileMismatch(
                    f"caller may only address its own store {identity!r}"
                )
        return identity

    if is_multitenant():
        lock = None if account else locked_profile()

        def _granted(value: str | None) -> str | None:
            # A legacy file token is unbound: on a multi-tenant instance the
            # instance lock confines it (piece 1), not the serving-profile grant
            # it carries on a single-store instance. Administered accounts stay
            # confined by their store grants.
            if principal is not None and principal.id.startswith("legacy:"):
                return value
            return access.authorize(value)

        def _named(value: str) -> str:
            # InvalidStoreName is a ValueError, and the REST layer maps only
            # ProfileMismatch (403) and UnknownProfile (400). Letting it escape
            # turns a plainly bad request into a 500 with a traceback.
            try:
                return store_name(value)
            except InvalidStoreName as exc:
                raise UnknownProfile(str(exc)) from exc

        if requested:
            asked = _named(requested)
            # THE LOCK STILL CONFINES AN UNBOUND CALLER. It is not merely a
            # default that stops applying once a `profile` field is present.
            #
            # An unbound caller on this hub is a static token: a typical pilot
            # runs MEMD_ENFORCE_PROFILE=1 with per-person tokens (`alice`,
            # `bob`). If the lock only supplied a default, then
            # setting MEMD_STORES_ROOT on that container would let any of those
            # tokens address any store by request field. That is cross-tenant
            # access by an authenticated caller, in a plausible
            # configuration, and it is the same premature-flip hazard already
            # closed for MEMD_CLONE.
            #
            # Piece 3's onboarding job needs to name an arbitrary store; it runs
            # against an instance with the lock off, or as an admin path of its
            # own. Broad addressing is a property of an unlocked instance, not
            # something a lock should stop enforcing.
            if lock and asked != _named(lock):
                raise ProfileMismatch(f"instance locked to profile {lock!r}")
            return _granted(asked)
        fallback = effective_environment().get("MEMD_PROFILE", "").strip()
        if lock:
            return _granted(_named(lock))
        if account:
            return _granted(None)
        if fallback:
            return _granted(_named(fallback))
        raise UnknownProfile(
            "no store requested and MEMD_PROFILE is unset on a multi-tenant instance"
        )

    lock = None if account else locked_profile()
    if lock and requested and requested != lock:
        raise ProfileMismatch(f"instance locked to profile {lock!r}")
    requested = access.authorize(requested)
    profile = requested or lock or effective_environment().get("MEMD_PROFILE", "").strip() or _default()
    if profile not in registry():
        raise UnknownProfile(f"unknown profile {profile!r}; known: {', '.join(registry())}")
    return profile


def effective_environment() -> dict[str, str]:
    """Match Config's env-file precedence before applying request overrides.

    Resolve the file first, then set the already-authorized request profile.
    Passing this result to Config with env_file=None prevents a second file
    merge from silently changing the profile after its lock was checked.
    """
    from memd.config import load_env_file
    env = dict(os.environ)
    explicit = env.get("MEMD_ENV_FILE")
    values = load_env_file(Path(explicit) if explicit else None)
    return {**env, **values} if explicit else {**values, **env}


def _default_map(profile: str) -> dict:
    """Static per-profile defaults from config.PROFILES.

    Imported lazily to avoid a config<->profiles import cycle. This unifies the
    two registries: when the env-var contract (MEMD_AMBER_*/MEMD_COBALT_*) is not
    fully populated, resolve() still yields the canonical isolated clone/db so
    the guard has a registered root to enforce against.
    """
    from memd.config import legacy_defaults

    return legacy_defaults(profile)


def _resolve_dynamic(profile: str) -> dict:
    """Resolve a per-user store under MEMD_STORES_ROOT (Plan 2, §9a).

    The remote and the credential are shared machinery, not per-store secrets:
    every store is pushed by memd's own deploy key to a repo named after the
    store (§9a: `memory/u-<username>`, created by the onboarding job).
    MEMD_STORES_REPO_TEMPLATE carries `{store}` and is empty by default, so a
    store with no configured remote is local-only rather than mis-pointed at
    somebody else's repository.
    """
    from memd.stores import store_dirname, store_name, store_paths

    name = store_name(profile)
    clone, db = store_paths(name)
    template = (os.environ.get("MEMD_STORES_REPO_TEMPLATE") or "").strip()
    return {
        "profile": name,
        "repo_ssh": template.format(store=store_dirname(name)) if template else "",
        "clone_path": clone,
        "db_path": db,
        "credential": os.environ.get("MEMD_SSH_KEY", "MEMD_SSH_KEY"),
    }


def resolve(profile: str) -> dict:
    """Resolve a store to its isolated repo/clone/db/credential.

    Two registries, checked in this order.

    1. The static rows (`amber`, `cobalt`). Env vars MEMD_<PROFILE>_REPO/_CLONE/
       _DB/_CRED win, then the config.PROFILES defaults. These are checked FIRST
       even on a multi-tenant instance: an existing container may already set
       MEMD_AMBER_CLONE=..., and turning on a stores root must never
       silently relocate a store that is already serving.
    2. MEMD_STORES_ROOT, when configured: any valid store name resolves to
       `<root>/<store>/{clone,memd.db}`, whether or not it exists yet.

    Neither matching is an unknown profile.
    """
    from memd.control import store
    managed = store(profile)
    if managed:
        cfg = managed["config"]
        return {"profile": profile, "repo_ssh": cfg.get("repo_ssh", ""),
                "clone_path": Path(cfg["clone_path"]).resolve(),
                "db_path": Path(cfg["db_path"]).resolve(), "credential": cfg.get("credential", "")}
    prefix = _legacy().get(profile)
    if prefix is None:
        from memd.stores import InvalidStoreName, is_multitenant

        if is_multitenant():
            try:
                return _resolve_dynamic(profile)
            except InvalidStoreName as exc:
                raise UnknownProfile(str(exc)) from exc
        raise UnknownProfile(profile)
    dflt = _default_map(profile)
    from memd.config import _process_env
    env = _process_env()   # the same env-file view Config.from_env resolves paths from

    def _get(suffix: str, dkey: str) -> str:
        val = env.get(f"{prefix}_{suffix}")
        if val is None:
            val = dflt.get(dkey)
        if val is None:
            raise KeyError(f"{prefix}_{suffix}")
        return val

    return {
        "profile": profile,
        "repo_ssh": _get("REPO", "repo_ssh"),
        "clone_path": Path(_get("CLONE", "clone_path")).expanduser().resolve(),
        "db_path": Path(_get("DB", "db_path")).expanduser().resolve(),
        "credential": _get("CRED", "credential"),
    }


def _roots_for(profile: str) -> list[Path]:
    """The registered roots owned by `profile` (its clone + db paths)."""
    cfg = resolve(profile)
    return [cfg["clone_path"], cfg["db_path"]]


def registered_roots() -> dict[str, list[Path]]:
    """Map every resolvable store -> its registered roots (clone + db).

    The static rows plus every store that currently exists under
    MEMD_STORES_ROOT. Note what this list is NOT used for: deciding whether a
    store may be touched. A store the caller owns may be absent (it is created
    on first save), and a store's presence here grants nobody else anything.
    Sibling refusal is structural, in :func:`assert_no_cross_profile`.
    """
    from memd.stores import known_stores

    out: dict[str, list[Path]] = {}
    for prof in registry():
        try:
            out[prof] = _roots_for(prof)
        except (UnknownProfile, KeyError):
            continue
    for store in known_stores():
        if store in out:
            continue
        try:
            out[store] = _roots_for(store)
        except (UnknownProfile, KeyError, ValueError):
            continue
    return out


def _is_within(target: Path, root: Path) -> bool:
    """True iff `target` is `root` or lives inside `root`'s subtree."""
    return target == root or root in target.parents


def _assert_known(profile: str) -> None:
    """Raise UnknownProfile unless `profile` is a resolvable store."""
    if profile in _legacy():
        return
    resolve(profile)  # raises UnknownProfile when it is neither static nor a store


def _within_own_store(profile: str, target: Path) -> bool:
    """False iff `target` is under the stores root but not in `profile`'s store.

    Returns True for every path outside the root, leaving those to the ordinary
    per-profile root checks. Single-tenant instances always return True.
    """
    from memd.stores import store_dirname, store_name, stores_root

    root = stores_root()
    if root is None or not _is_within(target, root):
        return True
    if target == root:
        return False  # the root is not owned by any store
    owner = target.relative_to(root).parts[0]
    try:
        mine = store_dirname(store_name(profile))
    except ValueError:
        return False
    return owner == mine


def assert_no_cross_profile(
    profile: str,
    path: str,
    *,
    extra_roots: list[Path] | None = None,
) -> None:
    """DENY-BY-DEFAULT path guard for `profile`.

    Allow `path` only if it is inside one of `profile`'s registered roots (its
    resolved clone/db) OR inside an explicit `extra_roots` entry (a per-call
    override root, e.g. a legacy MEMD_CLONE pointed at the profile's tree). In
    every other case raise CrossProfileViolation — whether the path belongs to a
    DIFFERENT profile, or to NO registered profile at all (sibling/symlink
    escape). A path that, after symlink resolution, lands inside a different
    profile's root is always refused even if it was reached via an allowed root.
    """
    _assert_known(profile)
    target = Path(path).expanduser().resolve()

    # 0. STRUCTURAL RULE for the multi-tenant root, checked before anything
    #    else so no override root can rescue a path from it. Inside
    #    MEMD_STORES_ROOT, the store that owns a path is decided by the first
    #    path component, full stop:
    #
    #      <root>/<owner>/...  belongs to <owner>, and to nobody else.
    #
    #    Deliberately structural rather than an enumeration of directories that
    #    exist. Enumerating would mean a store nobody has saved into yet is
    #    owned by no one, so whether user A could reach user B's path would
    #    depend on whether B had ever written a note. The root itself has no
    #    owner component and so belongs to no store.
    if not _within_own_store(profile, target):
        raise CrossProfileViolation(
            f"profile={profile} may not touch {target}: it is inside another "
            f"store under the multi-tenant root"
        )

    roots = registered_roots()

    # 1. Hard refuse: the (symlink-resolved) target sits inside ANOTHER profile's
    #    registered tree. This catches the symlink/relative bypass even when the
    #    caller passed an allowed override root.
    for other, other_roots in roots.items():
        if other == profile:
            continue
        if any(_is_within(target, r) for r in other_roots):
            raise CrossProfileViolation(
                f"profile={profile} may not touch {target} owned by {other!r}"
            )

    # 2. The set of roots this profile is permitted to touch: its own registered
    #    roots plus any explicit per-call override roots (resolved for symlinks).
    #    RESOLVED, not looked up in the enumeration above: a per-user store that
    #    has never been saved into has no directory yet, and enumerating would
    #    leave it owning nothing -- so its own first write would be refused.
    try:
        own = list(_roots_for(profile))
    except (UnknownProfile, KeyError, ValueError):
        own = list(roots.get(profile, []))
    if extra_roots:
        own += [Path(r).expanduser().resolve() for r in extra_roots]

    if any(_is_within(target, r) for r in own):
        return

    # 3. Deny-by-default: outside the union of this profile's permitted roots.
    raise CrossProfileViolation(
        f"profile={profile} may not touch {target}: outside its registered roots "
        f"{[str(r) for r in own]}"
    )


def open_for_recall(profile: str) -> tuple[Path, Path]:
    """Return (clone_path, db_path) for a recall bound to `profile`, asserting
    both belong to that profile. The caller must use ONLY these paths."""
    cfg = resolve(profile)
    assert_no_cross_profile(profile, str(cfg["clone_path"]))
    assert_no_cross_profile(profile, str(cfg["db_path"]))
    return cfg["clone_path"], cfg["db_path"]


def guard_paths(
    profile: str,
    clone: Path | str | None,
    db: Path | str | None,
) -> tuple[Path, Path]:
    """Authoritative gate for the recall/save core (FIX GROUP 2).

    Given the REQUESTED profile and the clone/db a caller intends to use (e.g.
    derived from a Config), enforce hard isolation BEFORE any open_db/git op:

      * resolve the profile's own registered roots,
      * treat the caller's clone/db as override roots (the legacy MEMD_CLONE /
        MEMD_DB single-clone contract) — but ONLY if they don't escape into a
        different profile's tree,
      * refuse with CrossProfileViolation if either path is owned by another
        profile, or (when no override is in play) sits outside the profile's
        registered roots entirely.

    A `None` clone/db falls back to the profile's resolved authoritative path,
    so the profile is always the source of truth for path selection.

    Returns the resolved (clone, db) the caller MUST use.
    """
    _assert_known(profile)
    auth = resolve(profile)
    from memd.control import store
    managed = store(profile)
    if managed and managed["config"].get("managed"):
        if (clone is not None and Path(clone).resolve() != auth["clone_path"]) or (db is not None and Path(db).resolve() != auth["db_path"]):
            raise CrossProfileViolation("Managed store paths cannot be overridden.")
    clone_p = (
        Path(clone).expanduser().resolve() if clone is not None
        else auth["clone_path"]
    )
    db_p = (
        Path(db).expanduser().resolve() if db is not None
        else auth["db_path"]
    )
    # The intended clone/db are themselves the override roots for this profile;
    # the guard still refuses them outright if they fall inside another profile.
    assert_no_cross_profile(profile, str(clone_p), extra_roots=[clone_p, db_p])
    assert_no_cross_profile(profile, str(db_p), extra_roots=[clone_p, db_p])
    return clone_p, db_p
