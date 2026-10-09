"""mem CLI: recall | save | doctor | reindex."""
from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

from memd.config import Config, host_name
from memd.embed import CanaryError, startup_canary
from memd.index import open_db, reindex, set_head_in_index
from memd.recall import recall
from memd.save import save
from memd.store import git_head_sha

# The surfaces that build their own Config, and the env each one actually gets.
# They have diverged silently before (only systemd read the env file, so the MCP
# server and the recall hook fell back to a stale embed endpoint: save raised
# ConnectionRefused, recall degraded to lexical-only). doctor now
# resolves all four and compares them, so drift is visible immediately.
ENTRY_POINTS: tuple[tuple[str, dict[str, str], object], ...] = (
    ("systemd (EnvironmentFile)", {}, "auto"),
    ("mcp (.claude.json)", {"MEMD_PROFILE": "amber"}, "auto"),
    ("hook (auto_recall)", {"MEMD_PROFILE": "amber",
                            "MEMD_FALLBACK_CHECKOUT": "/home/svcuser/.local/share/memd/notes"}, "auto"),
    ("no env file (bare defaults)", {}, None),
)


def _fingerprint_path(cfg: Config) -> Path:
    assert cfg.db is not None
    return cfg.db.parent / "canary_fingerprint.json"


def _cmd_recall(args, cfg: Config) -> int:
    kw = {"include_archived": True} if args.include_archived else {}
    notes = recall(args.query, profile=cfg.profile, k=args.k, cfg=cfg, **kw)
    print(json.dumps({"notes": [vars(n) for n in notes]}, default=str))
    return 0


def _cmd_save(args, cfg: Config) -> int:
    fact = {"title": args.title, "body": args.body, "host": args.host}
    if args.conflict:
        fact["conflict"] = True
    result = save(fact, profile=cfg.profile, cfg=cfg)
    print(json.dumps(vars(result), default=str))
    return 0


def _audit_entry_points() -> list[str]:
    """Print the per-surface config table; return the labels that disagree.

    The reference is the first row that resolved (systemd). A row differing from
    it means that surface silently talks to a different embed backend.
    """
    print("entry points:")
    rows: list[tuple[str, str, str, bool]] = []
    for label, stub, env_file in ENTRY_POINTS:
        try:
            ec = Config.from_env(dict(stub), env_file=env_file)
        except Exception as exc:  # a broken surface must not abort the audit
            rows.append((label, f"<error: {exc}>", "-", False))
        else:
            rows.append((label, ec.embed_url, ec.embed_model, True))

    reference = next((embed for _, embed, _, ok in rows if ok), None)
    mismatched: list[str] = []
    for label, embed, model, ok in rows:
        if not ok:
            status = "ERROR"
        elif embed != reference:
            status = "MISMATCH"
            mismatched.append(label)
        else:
            status = "OK"
        print(f"  {label:<30} embed={embed}  model={model}  {status}")
    return mismatched


def _probe_embed(url: str) -> None:
    """TCP-connect the embed backend. Reachability is advisory — the canary that
    follows is the authority on exit status."""
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname or "", port), timeout=2.0):
            print(f"embed backend {url}: reachable")
    except OSError as exc:
        print(f"embed backend {url}: UNREACHABLE ({exc})")


def _cmd_doctor(args, cfg: Config) -> int:
    if cfg.env_file_path is not None:
        keys = ", ".join(cfg.env_file_keys)
        print(f"config: env file {cfg.env_file_path} ({len(cfg.env_file_keys)} keys: {keys})")
    else:
        print("config: env file NOT FOUND (using module defaults)")

    mismatched = _audit_entry_points()
    if mismatched:
        print(f"DOCTOR WARN: {', '.join(mismatched)} resolve a different embed "
              "backend than systemd — those surfaces will silently use it.")

    _probe_embed(cfg.embed_url)

    try:
        startup_canary(cfg, fingerprint_path=_fingerprint_path(cfg))
    except CanaryError as e:
        print(f"DOCTOR FAIL: {e}")
        return 1
    try:
        head = git_head_sha(cfg.clone)
    except Exception as e:  # clone missing/unreadable
        print(f"DOCTOR FAIL: clone unreadable: {e}")
        return 1
    print(f"DOCTOR ok: embed canary passed, clone HEAD={head}")
    return 0


def _cmd_reindex(args, cfg: Config) -> int:
    db = open_db(cfg.db, dim=cfg.embed_dim)
    try:
        n = reindex(db, cfg)
        set_head_in_index(db, git_head_sha(cfg.clone))
    finally:
        db.close()
    print(f"reindex: re-embedded {n} changed note(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mem")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_recall = sub.add_parser("recall")
    p_recall.add_argument("query")
    p_recall.add_argument("--k", type=int, default=8)
    p_recall.add_argument("--include-archived", action="store_true",
                          help="also search notes archived by mem-forget")
    p_recall.set_defaults(func=_cmd_recall)

    p_save = sub.add_parser("save")
    p_save.add_argument("--title", required=True)
    p_save.add_argument("--body", default="")
    p_save.add_argument("--host", default=host_name("gpuhost"))
    p_save.add_argument("--conflict", action="store_true")
    p_save.set_defaults(func=_cmd_save)

    p_doctor = sub.add_parser("doctor")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_reindex = sub.add_parser("reindex")
    p_reindex.set_defaults(func=_cmd_reindex)

    args = parser.parse_args(argv)
    cfg = Config.from_env()
    return args.func(args, cfg)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
