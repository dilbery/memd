"""Read-only owner suggestions from the existing trusted identity sync feed."""
import json
import os
from pathlib import Path
import uuid
import time
from memd.stores import store_name


def personal_entitled(subject, store):
    """Fail closed on absent/stale directory data or a changed identity/email."""
    try:
        data = json.loads(Path(os.environ["MEMD_KASM_USER_MAP"]).read_text())
        age = time.time() - data["generated"]
        if not -60 <= age <= 900:
            return False
        return any(isinstance(row, dict) and row.get("active") is True
                   and row.get("subject") == subject and row.get("email") == store
                   for row in data["directory"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def directory():
    path = os.environ.get("MEMD_KASM_USER_MAP")
    if not path:
        return {"owners": [], "source": "not configured", "generated": None}
    try:
        data = json.loads(Path(path).read_text())
        rows = data.get("directory")
        source = "user directory"
        if not isinstance(rows, list):
            rows = [{"email":e, "name":e, "username":""} for e in (data.get("users") or {}).values()]
            source = "Kasm identity map"
        owners = {}
        for row in rows[:10000]:
            if not isinstance(row, dict) or row.get("active") is False:
                continue
            email = store_name(row.get("email"))
            if "@" not in email:
                continue
            owners[email] = {"email":email, "name":str(row.get("name") or email)[:200],
                             "username":str(row.get("username") or "")[:200]}
        return {"owners":sorted(owners.values(),key=lambda r:r["name"].casefold()),
                "source":source,"generated":data.get("generated")}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"owners":[],"source":"temporarily unavailable","generated":None}


def backfill_legacy(registry):
    """Explicit deployment/CLI operation, never triggered by a read request.

    Only exact, unambiguous directory usernames/emails can replace the migration
    placeholder. Free-text/service owners and reviewed rows are preserved.
    """
    rows=directory()["owners"]; count=0
    for token in registry.list():
        if not token["legacy"] or token["owner"] != "Legacy — review owner":
            continue
        label=token["label"].casefold()
        matches={r["email"] for r in rows if label in {r["email"].casefold(),r["username"].casefold()}}
        if len(matches)==1:
            registry.edit(token["id"],actor="cli:directory-owner-match",operation_id=str(uuid.uuid4()),
                          revision=token["revision"],label=token["label"],owner=matches.pop(),purpose=token["purpose"])
            count+=1
    return count
