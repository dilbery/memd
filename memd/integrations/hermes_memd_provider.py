"""Hermes forgejo-memory backend swap: talk to memd over HTTP, behind a config
override.

Activated by setting `memory.provider: forgejo-memory` AND
forgejo_memory.json {"backend": "memd", "url": "...", "token": "...",
"fallback_checkout": "~/.local/share/memd/notes"} in the Hermes config (see
docs/integration-registration.md). Keeps the same tool surface Hermes already
calls (memory_recall / memory_save). When memd is unreachable, recall degrades
to grep over a SEPARATE read-only checkout (the Hermes store clone), never
memd's dedicated write clone; save is read-only-skipped.
"""
from __future__ import annotations

import json

import httpx

from memd.integrations.degraded_grep import grep_recall


class MemdHttpProvider:
    name = "memd-http"

    def __init__(self, client: httpx.Client, token: str, fallback_checkout: str | None):
        self._client = client
        self._token = token
        self._fallback = fallback_checkout

    def _recall_http(self, query: str, top_k: int) -> list[dict] | None:
        try:
            r = self._client.post("/recall", json={"query": query, "k": top_k}, timeout=2.0)
            r.raise_for_status()
            return r.json().get("notes", [])
        except (httpx.HTTPError, httpx.TransportError):
            return None

    def _recall_fallback(self, query: str, top_k: int) -> list[dict]:
        if not self._fallback:
            return []
        try:
            return grep_recall(query, self._fallback, top_n=top_k)
        except Exception:
            return []

    def handle_tool_call(self, tool_name: str, args: dict) -> str:
        if tool_name == "memory_recall":
            query = args.get("query", "")
            top_k = int(args.get("top_k", 5) or 5)
            notes = self._recall_http(query, top_k)
            degraded = notes is None
            if degraded:
                notes = self._recall_fallback(query, top_k)
            return json.dumps(
                {
                    "degraded": degraded,
                    "results": [
                        {"slug": n.get("slug"), "body": (n.get("body") or "")[:1600]}
                        for n in notes
                    ],
                }
            )

        if tool_name == "memory_save":
            payload = {
                "title": args.get("title", "note"),
                "body": args.get("content", "") or args.get("body", ""),
            }
            try:
                r = self._client.post(
                    "/save",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=5.0,
                )
                r.raise_for_status()
                return json.dumps(r.json())
            except (httpx.HTTPError, httpx.TransportError):
                return json.dumps({"status": "skipped: memd unavailable (read-only degraded mode)"})

        return json.dumps({"error": f"unknown tool {tool_name}"})
