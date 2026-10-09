"""Env-driven config + the load-bearing 24985-byte MEMORY.md budget.

Each profile maps to its own repository, clone, database and credentials.
Legacy instances retain a serving-profile lock. With administration enabled,
authorized accounts can select registered stores; request and path guards
enforce the selected store's boundaries before accessing its files.
"""
from __future__ import annotations

import os
import socket
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from pathlib import Path

# 24.4 KB == 24.4 * 1024 == 24985 bytes. The Claude Code loader truncates the
# tail of MEMORY.md past this; one byte over silently re-breaks recall.
MEMORY_BUDGET_BYTES = 24985

# Host roles a note can be scoped to. The machine names are placeholders; a
# deployment maps them to its real short hostnames with MEMD_HOST_NAMES, e.g.
# MEMD_HOST_NAMES="gpuhost=workstation1,vmhost=buildbox,apphost=nas".
HOST_ROLES = ("gpuhost", "lapbox", "vmhost", "lxc", "remote", "any")
VALID_HOSTS = HOST_ROLES  # the unmapped names; see valid_hosts()

# Defaults suit a local Lemonade Server; overridden by ~/.config/memd/env (or whatever
# MEMD_ENV_FILE points at) which is loaded as the base config layer.
DEFAULT_EMBED_URL = "http://127.0.0.1:8000"
DEFAULT_EMBED_MODEL = "nomic-embed-text-v1-GGUF"
# Vector width of the embedding backend. nomic-embed-text-v1 is 768;
# Qwen3-Embedding-4B is 2560. Changing it rebuilds the
# vector cache (index.open_db), which is derived data, not the notes.
DEFAULT_EMBED_DIM = 768
DEFAULT_RERANK_URL = "http://127.0.0.1:8000"
DEFAULT_RERANK_MODEL = "bge-reranker-v2-m3-GGUF"
# Lemonade serves the reranker at this gerund path. LiteLLM serves the Cohere-shaped
# /v1/rerank: same request fields and the same results[].index/relevance_score reply.
DEFAULT_RERANK_PATH = "/api/v1/reranking"
DEFAULT_PROFILE = "amber"
# The env-configured (pre-administration) profiles. MEMD_LEGACY_PROFILES="a,b"
# replaces these placeholder names; the first one is the default MEMD_PROFILE.
DEFAULT_LEGACY_PROFILES = ("amber", "cobalt")
# Hot-path model deadlines. Defaults are the values recall.py used as literals; a
# gateway deployment raises them because a remote reranker is slower than a local GGUF.
DEFAULT_EMBED_DEADLINE_MS = 800
DEFAULT_RERANK_DEADLINE_MS = 900
# What recall's vector arm searches: "chunks" (per-chunk vectors, a note scored by
# its best chunk) or "notes" (one whole-body vector per note, the arm before
# chunking). Both are always indexed, so switching needs no re-embed.
DEFAULT_RECALL_VECTORS = "chunks"
RECALL_VECTORS = ("chunks", "notes")
# Optional chat model (memd.llm) for background jobs such as mem-summarize. Off
# unless MEMD_LLM_URL is set; never consulted by recall. The timeout bounds one
# whole completion, which for a local model summarising a dozen notes is slow.
DEFAULT_LLM_TIMEOUT_S = 120.0
# Save's advisory conflict check (memd.conflicts). "facts" compares the new
# note's deterministic facts with current facts of other notes (no model);
# "llm" also asks the chat model about the top related notes, under a hard
# deadline, and only when MEMD_LLM_URL is set; "off" disables both.
CONFLICT_CHECKS = ("off", "facts", "llm")
DEFAULT_CONFLICT_CHECK = "facts"
DEFAULT_CONFLICT_DEADLINE_MS = 1500
# ask's chat-model answer (memd.ask): one deadline for the whole completion; on
# expiry ask answers extractively instead. Clamped to ASK_DEADLINE_RANGE.
DEFAULT_ASK_DEADLINE_MS = 6000
ASK_DEADLINE_RANGE = (500, 30000)
# Recall usage log (memd.usage): a derived per-store SQLite file next to the
# index recording recalls and the reads that followed. "on" stores the
# normalised query text there (never in logs), "hash" stores only its hash,
# "off" records nothing. The learned boost is separate and off by default.
USAGE_LOG_MODES = ("on", "hash", "off")
DEFAULT_USAGE_LOG = "on"
DEFAULT_USAGE_RETENTION_DAYS = 90

DEFAULT_ENV_FILE = Path.home() / ".config" / "memd" / "env"

# Per-profile isolation map. clone_path / db_path / repo_ssh / credential are
# all distinct per profile so recall over one profile can never read another's.
PROFILES: dict[str, dict[str, str]] = {
    "amber": {
        "repo_ssh": "ssh://git@10.10.1.10:2200/svcuser/amber-memory.git",
        "clone_path": str(Path.home() / ".memd" / "amber" / "clone"),
        "db_path": str(Path.home() / ".memd" / "amber" / "memd.db"),
        "credential": "MEMD_TOKEN_AMBER",
    },
    "cobalt": {
        "repo_ssh": "ssh://git@10.10.1.10:2200/svcuser/cobalt-memory.git",
        "clone_path": str(Path.home() / ".memd" / "cobalt" / "clone"),
        "db_path": str(Path.home() / ".memd" / "cobalt" / "memd.db"),
        "credential": "MEMD_TOKEN_COBALT",
    },
}


def _process_env() -> dict[str, str]:
    """The process env merged with the memd env file, as Config.from_env merges them.

    Deployment names (MEMD_LEGACY_PROFILES, MEMD_HOST_NAMES) are read on hot
    paths, so the merge is cached until the file or the MEMD_* env changes.
    """
    chosen = os.environ.get("MEMD_ENV_FILE")
    path = Path(chosen) if chosen else DEFAULT_ENV_FILE
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = None
    key = (str(path), stamp, tuple(sorted((k, v) for k, v in os.environ.items() if k.startswith("MEMD_"))))
    cached = _PROCESS_ENV.get("merged")
    if cached is None or cached[0] != key:
        cached = (key, _merge_env(None)[0])
        _PROCESS_ENV["merged"] = cached
    return cached[1]


_PROCESS_ENV: dict[str, tuple] = {}


def legacy_profiles(env: dict[str, str] | None = None) -> tuple[str, ...]:
    """Names of the env-configured profiles, in MEMD_LEGACY_PROFILES order."""
    raw = (_process_env() if env is None else env).get("MEMD_LEGACY_PROFILES", "")
    names = tuple(dict.fromkeys(n.strip() for n in raw.split(",") if n.strip()))
    return names or DEFAULT_LEGACY_PROFILES


def default_profile(env: dict[str, str] | None = None) -> str:
    """The profile an unconfigured MEMD_PROFILE falls back to."""
    return legacy_profiles(env)[0]


def legacy_prefix(profile: str, env: dict[str, str] | None = None) -> str | None:
    """MEMD_<PROFILE> env prefix for a legacy profile; None for any other name."""
    if profile not in legacy_profiles(env):
        return None
    return "MEMD_" + profile.upper().replace("-", "_")


def legacy_defaults(profile: str) -> dict[str, str]:
    """Default clone/db/credential for a legacy profile without explicit env."""
    if profile in PROFILES:
        return PROFILES[profile]
    return {
        "repo_ssh": "",  # no remote unless MEMD_<PROFILE>_REPO names one
        "clone_path": str(Path.home() / ".memd" / profile / "clone"),
        "db_path": str(Path.home() / ".memd" / profile / "memd.db"),
        "credential": "MEMD_TOKEN_" + profile.upper().replace("-", "_"),
    }


def profile_config(profile: str) -> dict[str, str]:
    """Return the isolation map for a profile. Raises on unknown profile."""
    if profile in legacy_profiles():
        return legacy_defaults(profile)
    raise ValueError(f"unknown profile {profile!r}; known: {sorted(legacy_profiles())}")


def host_names(env: dict[str, str] | None = None) -> dict[str, str]:
    """Placeholder host name -> this deployment's hostname (MEMD_HOST_NAMES)."""
    raw = (_process_env() if env is None else env).get("MEMD_HOST_NAMES", "")
    out: dict[str, str] = {}
    for pair in raw.split(","):
        role, sep, name = pair.partition("=")
        if sep and role.strip() and name.strip():
            out[role.strip().casefold()] = name.strip().casefold()
    return out


def host_name(role: str) -> str:
    """The deployment's name for a placeholder host role (identity if unmapped)."""
    return host_names().get(role, role)


def valid_hosts() -> tuple[str, ...]:
    """Host values a note may be scoped to, with MEMD_HOST_NAMES applied."""
    return tuple(host_name(role) for role in HOST_ROLES)


def local_host() -> str:
    """The host this writer is running on (short name); 'any' if unrecognized.

    MEMD_LOCAL_HOST wins when set and valid. A gateway deployment sets it to 'any': its
    guests are not in VALID_HOSTS, the host-inference rules in infer_host/ground
    describe a different cluster entirely, and a note must not be scoped to
    whichever container happened to receive the save. With host 'any' the write
    path skips local grounding and records 'unverified-remote'.
    """
    override = os.environ.get("MEMD_LOCAL_HOST", "").strip()
    if override:
        return override if override in valid_hosts() else "any"
    short = socket.gethostname().split(".")[0]
    return short if short in valid_hosts() else "any"


def load_env_file(path: Path | None = None) -> dict[str, str]:
    """Parse a systemd-style EnvironmentFile.

    Returns a dict of only the ``MEMD_*`` keys found. A missing or unreadable
    file returns ``{}`` — config resolution must never be able to crash an entry
    point (§9.5 hard isolation applies to availability too: a bad env file
    must not take down a profile-serving daemon).

    Rules:
    * ``path`` defaults to :data:`DEFAULT_ENV_FILE`.
    * Skip blank lines and lines whose first non-whitespace char is ``#``.
    * Strip an optional leading ``export `` from the key.
    * Split on the FIRST ``=`` only (so ``=`` inside a value is preserved).
    * Strip surrounding whitespace from key and value.
    * Strip ONE matching pair of surrounding single or double quotes.
    * Ignore lines with no ``=`` or an empty key after stripping.
    """
    if path is None:
        path = DEFAULT_ENV_FILE
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}

    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            continue
        raw_key, _, raw_val = line.partition("=")
        key = raw_key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key:
            continue
        val = raw_val.strip()
        # NB: strip ONE matching quote pair only. Written out longhand on
        # purpose — the terse `val[0] == val[-1] in quotes` chained form is
        # correct but reads as a bug and invites a breaking "fix".
        if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
            val = val[1:-1]
        if key.startswith("MEMD_"):
            out[key] = val
    return out


def _embed_dim(value: str | None) -> int:
    """Vector width, or the default when unset or unusable.

    Never raises: config resolution must not be able to stop a profile-serving
    daemon from starting.
    """
    try:
        n = int(value) if value not in (None, "") else DEFAULT_EMBED_DIM
    except (TypeError, ValueError):
        return DEFAULT_EMBED_DIM
    return n if 8 <= n <= 8192 else DEFAULT_EMBED_DIM


def _recall_vectors(value: str | None) -> str:
    """The vector arm's unit; anything unrecognised is the default (never raises)."""
    v = (value or "").strip().lower()
    return v if v in RECALL_VECTORS else DEFAULT_RECALL_VECTORS


def _llm_timeout_s(value: str | None) -> float:
    """Chat completion timeout in seconds, clamped to 1..600 (never raises)."""
    try:
        n = float(value) if value not in (None, "") else DEFAULT_LLM_TIMEOUT_S
    except (TypeError, ValueError):
        return DEFAULT_LLM_TIMEOUT_S
    if n != n:  # NaN
        return DEFAULT_LLM_TIMEOUT_S
    return max(1.0, min(600.0, n))


def _conflict_check(value: str | None) -> str:
    """Save's conflict check mode; anything unrecognised is the default (never raises)."""
    v = (value or "").strip().lower()
    return v if v in CONFLICT_CHECKS else DEFAULT_CONFLICT_CHECK


def _llm_fields(env) -> dict:
    """The MEMD_LLM_* and MEMD_CONFLICT_* settings as Config keyword arguments."""
    return {
        "llm_url": (env.get("MEMD_LLM_URL") or "").strip() or None,
        "llm_model": (env.get("MEMD_LLM_MODEL") or "").strip(),
        "llm_timeout_s": _llm_timeout_s(env.get("MEMD_LLM_TIMEOUT_S")),
        "conflict_check": _conflict_check(env.get("MEMD_CONFLICT_CHECK")),
        "conflict_deadline_ms": _deadline_ms(env.get("MEMD_CONFLICT_DEADLINE_MS"),
                                             DEFAULT_CONFLICT_DEADLINE_MS),
        "ask_deadline_ms": _ask_deadline_ms(env.get("MEMD_ASK_DEADLINE_MS")),
    }


def _ask_deadline_ms(value: str | None) -> int:
    """ask's model deadline in milliseconds, clamped to ASK_DEADLINE_RANGE (never raises)."""
    try:
        n = int(float(value)) if value not in (None, "") else DEFAULT_ASK_DEADLINE_MS
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_ASK_DEADLINE_MS
    return max(ASK_DEADLINE_RANGE[0], min(ASK_DEADLINE_RANGE[1], n))


def _usage_fields(env) -> dict:
    """The MEMD_USAGE_* settings as Config keyword arguments (never raises)."""
    mode = (env.get("MEMD_USAGE_LOG") or "").strip().lower()
    mode = {"1": "on", "true": "on", "yes": "on", "0": "off", "false": "off", "no": "off"}.get(mode, mode)
    try:
        days = int(float(env.get("MEMD_USAGE_RETENTION_DAYS") or DEFAULT_USAGE_RETENTION_DAYS))
    except (TypeError, ValueError, OverflowError):
        days = DEFAULT_USAGE_RETENTION_DAYS
    return {
        "usage_log": mode if mode in USAGE_LOG_MODES else DEFAULT_USAGE_LOG,
        "usage_retention_days": max(1, min(3650, days)),
        "usage_boost": (env.get("MEMD_USAGE_BOOST") or "").strip().lower() in {"1", "true", "yes", "on"},
    }


def _deadline_ms(value: str | None, default: int) -> int:
    """A hot-path deadline in milliseconds, clamped to a usable band.

    Unparseable input falls back to the default rather than raising: config
    resolution must never take down a profile-serving daemon.
    """
    try:
        n = int(float(value)) if value not in (None, "") else default
    except (TypeError, ValueError, OverflowError):
        return default
    return max(100, min(10000, n))


def _merge_env(env: dict[str, str] | None, env_file: Path | str | None = "auto"
               ) -> tuple[dict[str, str], Path | None, dict[str, str]]:
    """The process env merged with the memd env file, with its precedence rules.

    Returns (merged, chosen env file path, the file's values).
    """
    e = os.environ if env is None else env
    explicit_file = False
    if env_file == "auto":
        chosen = e.get("MEMD_ENV_FILE")
        # A named MEMD_ENV_FILE is a deliberate per-consumer choice; the
        # implicit default is merely a fallback location.
        explicit_file = bool(chosen)
        chosen_path = Path(chosen) if chosen else DEFAULT_ENV_FILE
    elif env_file is None:
        chosen_path = None
    else:
        chosen_path = Path(env_file)
        explicit_file = True
    file_values: dict[str, str] = load_env_file(chosen_path) if chosen_path is not None else {}
    # Precedence. An EXPLICITLY configured env file layers ABOVE the process
    # env; only the implicit DEFAULT_ENV_FILE sits beneath it.
    #
    # The process env is a SNAPSHOT taken when the parent shell started, while
    # the file is materialised by the secrets manager and rotated in place — and
    # rotation REVOKES the previous token. A shell startup hook loads
    # memd.env at shell startup, so a long-lived tmux session exports a dead
    # token to every child forever. Env-first meant that snapshot beat the live
    # file: an opaque 401, or worse a silently mis-targeted embed backend, with
    # nothing naming which source won (slow to diagnose).
    # clients/memd-mcp-bridge resolves the same variable the same way; one
    # secret must not have two precedence rules.
    #
    # Env still wins for the implicit default so `MEMD_EMBED_URL=... mem recall`
    # remains a usable one-off override.
    merged = ({**e, **file_values} if explicit_file and file_values
              else {**file_values, **e})
    return merged, chosen_path, file_values


def resolved_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """The env Config.from_env resolves profiles from (process env plus env file)."""
    return _merge_env(env)[0]


@dataclass(frozen=True)
class Config:
    embed_url: str = DEFAULT_EMBED_URL
    embed_model: str = DEFAULT_EMBED_MODEL
    rerank_url: str = DEFAULT_RERANK_URL
    rerank_model: str = DEFAULT_RERANK_MODEL
    rerank_path: str = DEFAULT_RERANK_PATH
    embed_dim: int = DEFAULT_EMBED_DIM
    clone: Path | None = None
    db: Path | None = None
    profile: str = DEFAULT_PROFILE
    token: str | None = None
    # repr=False: a Config is printed in errors and logs; the key must not be.
    model_api_key: str | None = field(default=None, repr=False)
    # Origins the key may be sent to. None means every model request (the key
    # and the model URLs share one source); an administered store can point its
    # model URLs elsewhere, so it only sends the key to operator-set origins.
    model_key_origins: tuple[str, ...] | None = None
    embed_deadline_ms: int = DEFAULT_EMBED_DEADLINE_MS
    rerank_deadline_ms: int = DEFAULT_RERANK_DEADLINE_MS
    recall_vectors: str = DEFAULT_RECALL_VECTORS
    llm_url: str | None = None
    llm_model: str = ""
    llm_timeout_s: float = DEFAULT_LLM_TIMEOUT_S
    conflict_check: str = DEFAULT_CONFLICT_CHECK
    conflict_deadline_ms: int = DEFAULT_CONFLICT_DEADLINE_MS
    ask_deadline_ms: int = DEFAULT_ASK_DEADLINE_MS
    usage_log: str = DEFAULT_USAGE_LOG
    usage_retention_days: int = DEFAULT_USAGE_RETENTION_DAYS
    usage_boost: bool = False
    env_file_path: Path | None = None
    env_file_keys: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, *,
                 env_file: Path | str | None = "auto") -> "Config":
        merged, chosen_path, file_values = _merge_env(env, env_file)
        profile = merged.get("MEMD_PROFILE") or default_profile(merged)
        from memd.control import store
        managed = store(profile)
        env_file_path = chosen_path if file_values else None
        env_file_keys = tuple(sorted(file_values))
        shared = dict(
            rerank_path=merged.get("MEMD_RERANK_PATH", DEFAULT_RERANK_PATH),
            embed_dim=_embed_dim(merged.get("MEMD_EMBED_DIM")),
            profile=profile,
            token=merged.get("MEMD_TOKEN"),
            model_api_key=model_api_key_from(merged),
            embed_deadline_ms=_deadline_ms(merged.get("MEMD_EMBED_DEADLINE_MS"), DEFAULT_EMBED_DEADLINE_MS),
            rerank_deadline_ms=_deadline_ms(merged.get("MEMD_RERANK_DEADLINE_MS"), DEFAULT_RERANK_DEADLINE_MS),
            recall_vectors=_recall_vectors(merged.get("MEMD_RECALL_VECTORS")),
            **_llm_fields(merged),
            **_usage_fields(merged),
            env_file_path=env_file_path,
            env_file_keys=env_file_keys,
        )
        env_models = {key: merged.get("MEMD_" + key.upper(), globals()["DEFAULT_" + key.upper()])
                      for key in ("embed_url", "embed_model", "rerank_url", "rerank_model")}
        if managed:
            settings = managed["config"]
            trusted = [env_models["embed_url"], env_models["rerank_url"], shared["llm_url"]]
            return cls(
                clone=Path(settings["clone_path"]), db=Path(settings["db_path"]),
                model_key_origins=tuple(sorted({o for o in map(url_origin, trusted) if o})),
                **shared,
                **{key: settings.get(key) or value for key, value in env_models.items()},
            )
        p_clone, p_db = cls._profile_paths(merged, profile)
        from memd.stores import stores_root_from

        if stores_root_from(merged) is not None:
            # MULTI-TENANT: the store's own paths are the only paths. The global
            # MEMD_CLONE/MEMD_DB single-store overrides are not consulted at
            # all, because they cannot mean anything sensible when one process
            # serves many stores -- they would point every identity at one
            # clone. The currently deployed pilot sets both of them
            # (the pilot's compose file), so this is the difference between
            # "per-user stores" and "one shared store wearing per-user names".
            clone, db = p_clone, p_db
        else:
            clone = merged.get("MEMD_CLONE") or p_clone
            db = merged.get("MEMD_DB") or p_db
            # §9.5 hard isolation: a MEMD_CLONE/MEMD_DB override must NOT
            # silently point this profile at ANOTHER profile's registered tree.
            # Gate the raw overrides through the cross-profile guard so a
            # profile-mismatched override raises CrossProfileViolation instead
            # of quietly winning.
            cls._gate_overrides(profile, merged.get("MEMD_CLONE"), merged.get("MEMD_DB"))
        return cls(
            clone=Path(clone) if clone else None,
            db=Path(db) if db else None,
            **env_models,
            **shared,
        )

    @staticmethod
    def _profile_paths(env: dict[str, str], profile: str) -> tuple[str | None, str | None]:
        """Per-store (clone, db) over the PASSED env, so from_env(env) is hermetic.

        Mirrors profiles.resolve's precedence exactly, and must keep doing so:
        this used to be a third copy of the registry (an inline
        two-profile map), which meant any other store name
        yielded ``(None, None)`` and the core silently fell back to the process
        working directory instead of the caller's store.

        1. The static rows, MEMD_<PROFILE>_CLONE/_DB then the PROFILES default.
           First, so a stores root never relocates a serving pilot store.
        2. MEMD_STORES_ROOT: any valid store name, existing or not.
        """
        from memd.stores import InvalidStoreName, store_paths_under, stores_root_from

        prefix = legacy_prefix(profile, env)
        pmap = legacy_defaults(profile) if prefix else {}
        clone = (env.get(f"{prefix}_CLONE") if prefix else None) or pmap.get("clone_path")
        db = (env.get(f"{prefix}_DB") if prefix else None) or pmap.get("db_path")
        if clone and db:
            return clone, db

        root = stores_root_from(env)
        if root is not None:
            try:
                store_clone, store_db = store_paths_under(root, profile)
            except InvalidStoreName:
                # Not a usable store name. Leave the paths unset rather than
                # guessing; resolve_profile/guard_paths refuse it by name.
                return clone, db
            return str(store_clone), str(store_db)
        return clone, db

    @staticmethod
    def _gate_overrides(profile: str, clone: str | None, db: str | None) -> None:
        """Refuse a MEMD_CLONE/MEMD_DB override that escapes into another profile.

        Imported lazily (profiles imports config). Unknown profiles and unset
        overrides are no-ops; only a path that resolves inside a DIFFERENT
        registered profile's tree raises CrossProfileViolation.
        """
        from memd.profiles import (
            assert_no_cross_profile,
            CrossProfileViolation,
            UnknownProfile,
        )

        for raw in (clone, db):
            if not raw:
                continue
            override = Path(raw).expanduser().resolve()
            try:
                # extra_roots = the override itself, so it only fails when the
                # path lands inside another profile's registered tree (step 1 of
                # the guard), not merely for being outside this profile's root.
                assert_no_cross_profile(
                    profile, str(override), extra_roots=[override]
                )
            except UnknownProfile:
                # Unregistered profile: nothing to isolate against here; the
                # service layer rejects unknown profiles at the guard_paths call.
                return
            except CrossProfileViolation:
                raise


def model_api_key_from(env) -> str | None:
    """The model backend key, from MEMD_MODEL_API_KEY or MEMD_MODEL_API_KEY_FILE.

    The _FILE form exists for Docker/Swarm secrets: a GitOps sync tool
    rewrites the stack from Git and carries no stack env, so a ${VAR} key would
    deploy empty. The file is read on every call, stripped of surrounding
    whitespace (a trailing newline from `jq -r` or `echo`), and must be
    non-empty.

    Every misconfiguration raises instead of returning None, because None means
    an unauthenticated embedding call: the service stays healthy while every
    save and reindex fails 401. Setting both forms is refused rather than
    ranked, so there is never a question of which one won. No message ever
    includes the value.
    """
    value = env.get("MEMD_MODEL_API_KEY") or None
    path = (env.get("MEMD_MODEL_API_KEY_FILE") or "").strip()
    if not path:
        return value
    if value is not None:
        raise RuntimeError(
            "MEMD_MODEL_API_KEY and MEMD_MODEL_API_KEY_FILE are both set; set only one")
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            "MEMD_MODEL_API_KEY_FILE {} cannot be read: {}".format(
                path, exc.strerror or type(exc).__name__)) from None
    key = text.strip()
    if not key:
        raise RuntimeError("MEMD_MODEL_API_KEY_FILE {} is empty".format(path))
    return key


def url_origin(url: str | None) -> str | None:
    """scheme://host:port of a URL (default ports filled in), or None."""
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname or port is None:
        return None
    return f"{parts.scheme}://{parts.hostname.lower()}:{port}"


def model_headers(cfg: "Config", url: str | None = None) -> dict[str, str]:
    """Authorization header for a request to the embedding, rerank or chat backend.

    A local Lemonade endpoint takes no credential; LiteLLM refuses an
    unauthenticated call. One key serves all three because they sit behind the
    same gateway. When the config restricts the key to operator-set origins, a
    request to any other origin (or without a URL) goes without it.
    """
    if not cfg.model_api_key:
        return {}
    if cfg.model_key_origins is not None and url_origin(url) not in cfg.model_key_origins:
        return {}
    return {"Authorization": f"Bearer {cfg.model_api_key}"}
