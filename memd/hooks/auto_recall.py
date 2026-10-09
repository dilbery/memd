"""Claude Code UserPromptSubmit hook: first-turn additive auto-recall.

Fires deterministic retrieval at turn start instead of waiting for the model to
elect the recall tool. Rails (design §9.1):
  - small fixed top_n (additive to the always-prepended core set, not a dump),
  - obeys the §5 recall deadlines via the core pipeline,
  - bounds the injected size,
  - degrades to grep over a SEPARATE read-only checkout when the core is down,
  - NEVER blocks the turn: any failure -> neutral empty context, exit 0.

Remote mode: when `MEMD_REMOTE` is set (e.g. `http://10.10.1.10:8077`) recall is
performed over HTTP instead of in-process, so client machines need no clone or
local memd data. Falls back to the existing `MEMD_FALLBACK_CHECKOUT` grep path
on any remote failure.
"""
from __future__ import annotations

import json
import os
import re
import sys

import httpx

from memd.config import Config, default_profile
from memd.recall import recall as _core_recall
from memd.integrations.degraded_grep import grep_recall

AUTO_TOP_N = 4
MAX_INJECT_CHARS = 4000
PER_NOTE_CHARS = 900


_RECALL_ID = re.compile(r"^[0-9a-f]{16}$")


def _render(hits: list[dict], recall_id: str | None = None) -> str:
    if not hits:
        return ""
    parts = ["## Auto-recalled memory (additive to core)"]
    for h in hits[:AUTO_TOP_N]:
        body = (h.get("body") or "")[:PER_NOTE_CHARS]
        parts.append(f"### {h.get('slug', 'note')}\n{body}")
    footer = ""
    if isinstance(recall_id, str) and _RECALL_ID.match(recall_id):
        # Reads that name their recall improve ranking (memd.usage).
        footer = f'\n\n[To open a note from this recall: read(slug, recall_id="{recall_id}").]'
    text = "\n\n".join(parts)
    return text[:MAX_INJECT_CHARS - len(footer)] + footer


def _remote_recall(prompt: str, profile: str) -> tuple[list[dict], str | None]:
    """Recall notes over HTTP from a remote memd server.

    Returns (note dicts in the same shape the in-process path renders, the
    server's recall_id or None).
    Raises on any failure; callers are expected to treat exceptions as fatal
    and fall back.
    """
    base = os.environ["MEMD_REMOTE"].rstrip("/")
    url = f"{base}/recall"
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("MEMD_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # `include_core: False` to match the in-process caller below. Without it the
    # server defaults it to True and returns the core set FIRST, so the blind
    # `[:AUTO_TOP_N]` head-slice at the call site took AUTO_TOP_N core notes and
    # ZERO query-relevant ones — on a corpus with many core notes they all precede
    # the first match, so this hook injected only constant stubs while looking healthy.
    payload = {"query": prompt, "profile": profile, "k": AUTO_TOP_N,
               "include_core": False}
    with httpx.Client(timeout=4.0) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    # The remote returns the same dict shape the in-process path renders.
    notes = data["notes"]
    # The in-process path builds dicts via `n.to_dict()`; the remote returns
    # plain dicts already, so feed them directly into the existing renderer.
    return [dict(n) for n in notes], data.get("recall_id")


def build_context(event: dict) -> dict:
    prompt = (event or {}).get("prompt", "") or ""
    block = ""
    if prompt.strip():
        remote = os.environ.get("MEMD_REMOTE", "")
        profile = os.environ.get("MEMD_PROFILE") or default_profile()
        if remote:
            try:
                hits, recall_id = _remote_recall(prompt, profile)
                block = _render(hits[:AUTO_TOP_N], recall_id)
            except Exception as e:
                sys.stderr.write(f"memd remote recall failed: {e}\n")
                block = ""
            # On remote failure, fall back to the existing MEMD_FALLBACK_CHECKOUT
            # grep path below only if block is still empty.
            if not block:
                checkout = os.environ.get("MEMD_FALLBACK_CHECKOUT")
                if checkout:
                    try:
                        hits = grep_recall(prompt, checkout, top_n=AUTO_TOP_N)
                        block = _render(hits)
                    except Exception:
                        block = ""
                else:
                    block = ""
        else:
            try:
                # Thread the profile into Config so it is authoritative for clone/db
                # selection and the recall guard resolves the right isolated paths.
                env = dict(os.environ)
                env["MEMD_PROFILE"] = profile
                cfg = Config.from_env(env)
                # query-relevant only — the carved MEMORY.md core is already loaded,
                # so don't double-inject the core set here.
                notes = _core_recall(prompt, profile=profile, k=AUTO_TOP_N, cfg=cfg,
                                     include_core=False)
                hits = [n.to_dict() for n in notes][:AUTO_TOP_N]
                block = _render(hits)
            except Exception:
                checkout = os.environ.get("MEMD_FALLBACK_CHECKOUT")
                if checkout:
                    try:
                        hits = grep_recall(prompt, checkout, top_n=AUTO_TOP_N)
                        block = _render(hits)
                    except Exception:
                        block = ""
                else:
                    block = ""
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": block,
        }
    }


def main() -> int:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        event = {}
    out = build_context(event)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
