"""SSH recovery/migration CLI using the same registry and transactions as the UI."""
import argparse
import json
import os
import sys
import uuid
from memd.registry import Registry, ControlError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("MEMD_CONTROL_DB"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("import-legacy", help="Import the current token file exactly once, preserving existing access")
    sub.add_parser("list")
    sub.add_parser("activity")
    backup = sub.add_parser("backup")
    backup.add_argument("destination")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("token_id")
    revoke.add_argument("--revision", type=int, required=True)
    issue = sub.add_parser("issue")
    for field in ("label", "owner", "purpose", "stores", "operations"):
        issue.add_argument("--" + field, required=True)
    issue.add_argument("--days", type=int, default=90)
    args = parser.parse_args()
    if not args.db:
        parser.error("--db or MEMD_CONTROL_DB is required")
    registry = Registry(args.db)
    actor = "cli:" + os.environ.get("SUDO_USER", os.environ.get("USER", "operator"))
    try:
        if args.command == "init":
            registry.initialize()
            print("Registry initialized. Authentication cutover requires MEMD_CONTROL_DB.")
        elif args.command == "import-legacy":
            from memd.mcp_http import load_tokens
            print(json.dumps({"imported": registry.import_legacy(load_tokens(), actor)}))
        elif args.command == "list":
            print(json.dumps(registry.list(), indent=2))
        elif args.command == "activity":
            print(json.dumps(registry.events(), indent=2))
        elif args.command == "backup":
            registry.backup(args.destination)
            print("Consistent backup created; browser sessions excluded.")
        elif args.command == "revoke":
            print(json.dumps(registry.revoke(args.token_id, actor=actor, operation_id=str(uuid.uuid4()), revision=args.revision)))
        elif args.command == "issue":
            # Refuse accidental credential capture in command transcripts and CI logs.
            if not sys.stdout.isatty():
                parser.error("issue requires an interactive terminal; use the web UI for secure one-time display")
            print(json.dumps(registry.issue(actor=actor, operation_id=str(uuid.uuid4()),
                label=args.label, owner=args.owner, purpose=args.purpose, stores=args.stores.split(","),
                operations=args.operations.split(","), days=args.days), indent=2))
    except (ControlError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
