"""MCP tools for memory recall, complete note reads and durable saves.

The stdio launcher calls the local memd core. memd.mcp_http wraps this same
Server instance for remote clients, so both transports share tool contracts.
No tool can delete notes.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
from typing import Any

import jsonschema
import mcp.types as types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server

from memd.config import Config
from memd.normalize import NormalizeError, normalize_fact, normalize_recall_args
from memd.recall import recall as _core_recall
from memd.render import render_result
from memd.profiles import resolve_profile, effective_environment
from memd.registry import enforce
from memd.read import read as _core_read
from memd.save import save as _core_save
from memd import usage

_ERROR_FIELDS = {"error": {"type": "string"}, "error_type": {"type": "string"}}
RECALL_OUTPUT = {
    "type": "object", "properties": {
        **_ERROR_FIELDS, "ok": {"type": "boolean"}, "profile": {"type": "string"},
        "query": {"type": "string"}, "truncated": {"type": "boolean"},
        "stores": {"type": "array", "items": {"type": "string"},
                   "description": "Stores a federated recall covered (stores/scope); each excerpt names its store."},
        "stores_skipped": {"type": "array", "items": {"type": "object"},
                           "description": "Stores that failed or missed the deadline, with the reason."},
        "recall_id": {"type": ["string", "null"],
                      "description": "Pass to read(recall_id=...) for a note you open from this recall."},
        "text": {"type": "string", "description": "The rendered notes: query excerpts, then the core index."},
        "returned_matches": {"type": "integer"}, "returned_core": {"type": "integer"},
        "omitted_matches": {"type": "integer"}, "omitted_core": {"type": "integer"},
        "excerpts": {"type": "array", "items": {"type": "object", "properties": {
            "slug": {"type": "string"}, "offset": {"type": "integer"},
            "end_offset": {"type": "integer"}, "total_chars": {"type": "integer"},
            "revision": {"type": ["string", "null"]}, "truncated": {"type": "boolean"},
            "store": {"type": "string", "description": "Store the note came from (federated recall)."},
            "upstream": {"type": "object", "description": "Drift of a published copy against its source."},
            "archived": {"type": "string", "description": "Date the note was archived (include_archived only)."},
        }}},
    },
}
PUBLISH_OUTPUT = {
    "type": "object", "properties": {
        **_ERROR_FIELDS, "ok": {"type": "boolean"},
        "action": {"type": "string", "description": "published, updated, unchanged or queued (for review)."},
        "queued": {"type": "boolean"}, "inbox_id": {"type": "string"},
        "source": {"type": "object"}, "target": {"type": "object"},
        "published_from": {"type": "object", "description": "Provenance stamped on the copy: store, slug, revision, by, at."},
        "receipt": {"type": ["object", "null"]},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}
SAVE_OUTPUT = {
    "type": "object", "properties": {
        **_ERROR_FIELDS, "ok": {"type": "boolean"}, "slug": {"type": "string"},
        "action": {"type": "string"}, "revision": {"type": "string"},
        "saved": {"type": "boolean", "description": "Committed to local authoritative Git."},
        "synced": {"type": "boolean", "description": "Published to the Git remote."},
        "indexed": {"type": "boolean", "description": "All active notes have current semantic embeddings."},
        "lexical_indexed": {"type": "boolean", "description": "Available to keyword recall immediately."},
        "related": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "queued": {"type": "boolean", "description": "Filed for human review instead of saved (MEMD_SAVE_MODE=inbox)."},
        "inbox_id": {"type": "string", "description": "Inbox candidate id when queued."},
        "conflicts": {
            "type": "array",
            "description": ("Existing notes this fact appears to contradict or update. Advisory: "
                            "nothing is superseded unless you save again with `supersedes`."),
            "items": {"type": "object", "properties": {
                "slug": {"type": "string"}, "title": {"type": "string"},
                "kind": {"type": "string", "enum": ["contradicts", "updates"]},
                "evidence": {"type": "string", "description": "The conflicting claims, with their dates."},
                "suggested_action": {"type": "string"},
                "method": {"type": "string", "description": "facts (deterministic) or llm (model verdict)."},
            }},
        },
    },
}
PROPOSE_OUTPUT = {
    "type": "object", "properties": {
        **_ERROR_FIELDS, "ok": {"type": "boolean"}, "proposed": {"type": "boolean"},
        "id": {"type": "string", "description": "Inbox candidate id."},
        "status": {"type": "string"}, "duplicate": {"type": "boolean"},
        "message": {"type": "string"},
        "lint": {"type": "object", "description": (
            "What saving it would do: action (create/update/supersede), slug, related, "
            "near_duplicates, conflicts, warnings, and error when save would refuse it.")},
    },
}
READ_OUTPUT = {
    "type": "object", "properties": {
        **_ERROR_FIELDS, "ok": {"type": "boolean"}, "slug": {"type": "string"},
        "title": {"type": "string"}, "body": {"type": "string"},
        "profile": {"type": "string"}, "source": {"type": ["string", "null"]},
        "store": {"type": "string", "description": "The store the note was read from."},
        "published_from": {"type": "object", "description": "Present on a note published from another store."},
        "upstream": {"type": "object", "description": (
            "For a published copy whose source you can read: status current, behind, missing or unknown.")},
        "observed_at": {"type": ["string", "null"]}, "verified_at": {"type": ["string", "null"]},
        "archived": {"type": "string", "description": (
            "Present when the note was archived by review (mem-forget): the date. Not current memory.")},
        "archived_reason": {"type": "string", "description": "Why it was archived."},
        "notice": {"type": "string", "description": "A warning to read before using the note."},
        "revision": {"type": "string"}, "git_head": {"type": "string"},
        "offset": {"type": "integer"}, "end_offset": {"type": "integer"},
        "total_chars": {"type": "integer"}, "complete": {"type": "boolean"},
        "next_offset": {"type": ["integer", "null"]},
        "continuation": {"type": ["object", "null"], "description": "Arguments for the next read call."},
    },
}


ASK_OUTPUT = {
    "type": "object", "properties": {
        **_ERROR_FIELDS, "ok": {"type": "boolean"}, "profile": {"type": "string"},
        "question": {"type": "string"},
        "answer": {"type": "string", "description": "Short answer drawn only from the cited notes."},
        "citations": {"type": "array", "items": {"type": "object", "properties": {
            "slug": {"type": "string"}, "store": {"type": "string"}, "title": {"type": "string"},
            "as_of": {"type": ["string", "null"]}, "stale": {"type": "boolean"},
            "kind": {"type": "string", "description": "summary for a current-state summary note."},
            "caveat": {"type": "string", "description": "Why a stale note may be out of date."},
        }}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "mode": {"type": "string", "enum": ["model", "extractive"], "description": (
            "model: written by the chat model from the evidence; extractive: quoted sentences, "
            "because no model answer was available (see fallback_reason).")},
        "fallback_reason": {"type": ["string", "null"]},
        "citations_dropped": {"type": "integer", "description": "Model citations outside the evidence, removed."},
        "evidence": {"type": "integer", "description": "Evidence items the answer was drawn from."},
        "stores": {"type": "array", "items": {"type": "string"}},
        "stores_skipped": {"type": "array", "items": {"type": "object"}},
        "recall_id": {"type": ["string", "null"],
                      "description": "Pass to read(recall_id=...) for a cited note you open."},
        "text": {"type": "string", "description": "The answer, caveats and a compact Sources list."},
    },
}


_FACT_ITEM = {"type": "object", "properties": {
    "subject": {"type": "string"}, "predicate": {"type": "string"}, "object": {"type": "string"},
    "valid_from": {"type": ["string", "null"]}, "valid_to": {"type": ["string", "null"]},
    "current": {"type": "boolean"}, "source": {"type": "string", "description": "Slug of the note the fact came from."},
    "revision": {"type": "string"}, "method": {"type": "string"},
    "closed_by": {"type": ["string", "null"], "description": "Slug of the note whose newer fact ended this one."},
    "note_changed": {"type": "boolean"},
}}
TIMELINE_OUTPUT = {
    "type": "object", "properties": {
        **_ERROR_FIELDS, "ok": {"type": "boolean"}, "profile": {"type": "string"},
        "subject": {"type": "string"}, "predicate": {"type": ["string", "null"]},
        "at": {"type": ["string", "null"]},
        "match": {"type": "string", "description": "exact, partial (subject named more briefly) or none."},
        "text": {"type": "string"},
        "facts": {"type": "array", "items": _FACT_ITEM},
    },
}


def _cfg_for(profile: str) -> Config:
    """Profile-resolved Config for the in-process core call.

    Honors an explicit MEMD_CLONE/MEMD_DB override (set by tests / the unit
    file) but falls back to the requested profile's isolated clone+db so a
    recall/save bound to one profile can never read another's (design §9.5).
    """
    from memd.store_bootstrap import ensure_store

    env = effective_environment()
    env["MEMD_PROFILE"] = profile
    cfg = Config.from_env(env, env_file=None)
    # A per-user store is created on first use. entrypoint.py wires the single
    # clone of a single-store deployment at startup and cannot do this: users
    # appear one at a time. No-op unless MEMD_STORES_ROOT is set, and the common
    # path is one stat.
    ensure_store(cfg)
    return cfg


def _drop_profile_when_multitenant(tools: list[types.Tool]) -> list[types.Tool]:
    """Stop advertising `profile` on a multi-tenant instance (Plan 2, §9a).

    §9a: the `profile` argument is "dropped or forced to equal the caller's
    own". Both, and they do different jobs. Forcing equality in
    `_call_tool_sync` is the SECURITY control, because a client can send the
    field whether or not it is advertised. Dropping it from the schema is a
    USABILITY control: an advertised argument is one a model will eventually
    populate, and every time it guesses another store the call is refused for
    no reason the model can see.

    Single-tenant instances keep the field, so the shipped behaviour and the
    existing suite are untouched.
    """
    from memd.stores import is_multitenant

    if not is_multitenant():
        return tools
    out = []
    for tool in tools:
        schema = dict(tool.input_schema)
        properties = {k: v for k, v in schema.get("properties", {}).items()
                      if k != "profile"}
        schema["properties"] = properties
        # model_copy(update=) takes the field name; the camelCase alias is
        # silently ignored, which would leave `profile` advertised.
        out.append(tool.model_copy(update={"input_schema": schema}))
    return out


async def list_tools() -> list[types.Tool]:
    """Advertise recall, save, timeline and read with deliberately permissive schemas.

    `required` is empty on each, and the common aliases are advertised as real
    properties, because the client validates against this schema and refuses to
    dispatch on a mismatch. Saves from a real agent client once died exactly
    there -- `{"slug":..., "content":...}` and friends were rejected locally
    with a bare "title is required", so the call never reached
    this server and no amount of leniency in `call_tool` could have rescued it.
    Widening the ADVERTISED schema is therefore the only fix that works; the
    normalisation in `call_tool` is the second half of the same fix.

    The canonical names stay first with the clearest descriptions, so a model
    that reads carefully still emits `title`/`body`/`query`.
    """
    tools = [
        types.Tool(
            name="recall",
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
            outputSchema=RECALL_OUTPUT,
            description=(
                "Retrieve the most relevant durable memory notes for a query. "
                "Read-only. Returns query-centered excerpts plus a compact core index; use read(slug) for complete notes. "
                "Preferred argument is `query`; `q`/`question`/`topic`/`search`/`text` are "
                "accepted as aliases. Calling with no query at all returns the core set "
                "rather than failing. "
                "Using the results: rank is not confidence, so check that a note actually answers the "
                "question before relying on it. If nothing fits, retry ONCE with a sharper query (the "
                "specific host, service, identifier or error, with pronouns resolved) before concluding "
                "nothing is stored. An excerpt is partial: read(slug) before acting on details "
                "(pass this result's recall_id along). "
                "Headings show when a note was true ('as of'); notes flagged as possibly stale describe "
                "changeable state, so verify the live system first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "include_core": {"type": ["boolean", "string"], "description": "Include the compact core index (default true)."},
                    "include_archived": {"type": ["boolean", "string"], "description": (
                        "Also search notes archived by review (mem-forget), marked ARCHIVED; "
                        "for explicit digging into history (default false).")},
                    "max_chars": {"type": ["integer", "string"], "description": "Rendered text budget, 256..100000 (default 14000)."},
                    "core_limit": {"type": ["integer", "string"], "description": "Maximum core index entries, 0..50 (default MEMD_CORE_LIMIT or 8)."},
                    "host": {"type": "string", "description": "Restrict query matches to this host."},
                    "tags": {"type": ["array", "string"], "items": {"type": "string"}, "description": "Restrict matches by tags; list or comma-separated string."},
                    "query": {"type": "string", "description": "What to recall about."},
                    "profile": {"type": "string", "description": "Owner profile (defaults to the serving instance)."},
                    "stores": {"type": ["array", "string"], "items": {"type": "string"}, "description": (
                        "Recall across these stores you have access to (list or comma-separated); "
                        "each result is labelled [store]. Omit for your own store only.")},
                    "scope": {"type": "string", "enum": ["all"], "description": (
                        "\"all\": recall across every store you have access to.")},
                    "k": {
                        "type": ["integer", "string"],
                        "description": "Max query matches before the character budget (default 8, max 50).",
                    },
                    # Aliases: advertised so client-side validation lets them through.
                    "q": {"type": "string", "description": "Alias for query."},
                    "question": {"type": "string", "description": "Alias for query."},
                    "topic": {"type": "string", "description": "Alias for query."},
                    "search": {"type": "string", "description": "Alias for query."},
                    "text": {"type": "string", "description": "Alias for query."},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="save",
            annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
            outputSchema=SAVE_OUTPUT,
            description=(
                "Save one durable fact. Dedups/upserts by slug, host-aware grounding, "
                "commits to memd's dedicated clone. Never deletes; conflicts supersede. "
                "The receipt's `conflicts` names existing notes this fact appears to "
                "contradict or update; nothing is retired unless you save again with "
                "`supersedes`. "
                "Preferred arguments are `title` + `body`. If you send the text under "
                "`content`/`text`/`fact`/`note` it is accepted, and if you omit the title "
                "one is derived from the body -- a fact is never rejected for using the "
                "wrong field name."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Short summary for the core index."},
                    "expected_revision": {"type": "string", "description": "Revision from read/save; reject the write if the note changed."},
                    "supersedes": {"type": "string", "description": "Existing slug this fact explicitly replaces; old content remains in Git."},
                    "source": {"type": "string", "description": "Where the fact came from (command, document, or user statement)."},
                    "observed_at": {"type": "string", "description": "When the fact was observed, ISO timestamp or date."},
                    "verified_at": {"type": "string", "description": "When the fact was verified, ISO timestamp or date."},
                    "pinned": {"type": ["boolean", "string"], "description": "Explicitly prioritize in the core index; do not pin routine task notes."},
                    "volatility": {"type": "string", "description": (
                        "How long the fact stays true: durable (identity, preferences, decisions, hardware), "
                        "state (true now but changes: versions, what is deployed or running, config values), "
                        "volatile (in-progress work, today's plan). Recall flags old state/volatile notes as "
                        "possibly stale. Omit it when unsure; no verdict is better than a wrong one.")},
                    "verify": {
                        "type": ["array", "null"],
                        "items": {"type": ["object", "string"]},
                        "description": (
                            "Optional read-only probes mem-verify re-checks on a schedule, each one of: "
                            "{tcp: 'host:port'}, {http: 'https://host/health', status?: 200, json?: 'key' or "
                            "'key=value'} (GET only), {dns: 'name -> 10.10.1.10'}, {command: 'binary'} "
                            "(existence on PATH, never run), {path: '/abs/path'} (existence). "
                            "Unknown kinds are rejected; [] or null clears them."),
                    },
                    "title": {"type": "string", "description": "Short human title."},
                    "body": {"type": "string", "description": "Fact body (markdown)."},
                    "host": {"type": "string", "description": "Host the fact is scoped to."},
                    "profile": {"type": "string", "description": "Owner profile (defaults to the serving instance)."},
                    "importance": {
                        "type": ["integer", "string"],
                        "description": "1..5 (default 3).",
                    },
                    "tags": {
                        "type": ["array", "string"],
                        "items": {"type": "string"},
                        "description": "List, or a comma-separated string.",
                    },
                    # Aliases: advertised so client-side validation lets them through.
                    "content": {"type": "string", "description": "Alias for body."},
                    "text": {"type": "string", "description": "Alias for body."},
                    "fact": {"type": "string", "description": "Alias for body."},
                    "note": {"type": "string", "description": "Alias for body."},
                    "slug": {
                        "type": "string",
                        "description": "Alias for title; also pins the dedup slug.",
                    },
                    "name": {"type": "string", "description": "Alias for title."},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="timeline",
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
            outputSchema=TIMELINE_OUTPUT,
            description=(
                "Exact lookup of time-bounded facts extracted from notes: what is true about a subject NOW "
                "(facts not ended by a newer one), or what was true on a date with `at`. Read-only. Each "
                "fact gives subject | predicate | object, valid_from..valid_to and its source slug; "
                "read(source) before acting on a detail. No facts does not mean nothing is stored: "
                "facts come from a background job, so fall back to recall."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "The thing asked about: a service, host, device or setting."},
                    "predicate": {"type": "string", "description": "Optional relation, e.g. 'runs on', 'version', 'ip address'."},
                    "at": {"type": "string", "description": "Optional ISO date (YYYY-MM-DD): facts valid on that date instead of now."},
                    "profile": {"type": "string", "description": "Owner profile (defaults to the serving instance)."},
                    # Alias: advertised so client-side validation lets it through.
                    "query": {"type": "string", "description": "Alias for subject."},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="ask",
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
            outputSchema=ASK_OUTPUT,
            description=(
                "Answer a question from memory in a few sentences, citing the notes it comes from, "
                "instead of returning raw excerpts. Read-only. Uses recall, current timeline facts and "
                "current-state summaries; the answer uses only that evidence. `mode` says whether a chat "
                "model wrote it or it was extracted verbatim (no model, timeout or unusable reply). "
                "Check `confidence`, `gaps` and stale caveats; read(slug) a source before acting on a "
                "detail. Use recall instead when you want the notes themselves."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "What to answer from memory."},
                    "k": {"type": ["integer", "string"], "description": "Recall matches to draw on (default 6, max 12)."},
                    "host": {"type": "string", "description": "Restrict evidence to this host."},
                    "tags": {"type": ["array", "string"], "items": {"type": "string"}, "description": "Restrict evidence by tags; list or comma-separated string."},
                    "profile": {"type": "string", "description": "Owner profile (defaults to the serving instance)."},
                    "stores": {"type": ["array", "string"], "items": {"type": "string"}, "description": (
                        "Answer from these stores you have access to (list or comma-separated). "
                        "Omit for your own store only.")},
                    "scope": {"type": "string", "enum": ["all"], "description": (
                        "\"all\": answer from every store you have access to.")},
                    # Aliases: advertised so client-side validation lets them through.
                    "query": {"type": "string", "description": "Alias for question."},
                    "q": {"type": "string", "description": "Alias for question."},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="read",
            description="Read the complete authoritative note by slug, including source, dates and revision. Long notes return continuation arguments; call read again with them until complete. This also reads superseded notes for history.",
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
            inputSchema={
                "type": "object", "required": [], "properties": {
                    "slug": {"type": "string", "description": "Exact note slug from recall or save."},
                    "store": {"type": "string", "description": "Store the slug came from (a federated recall's [store]); defaults to your own."},
                    "profile": {"type": "string", "description": "Owner profile; defaults to the serving instance."},
                    "offset": {"type": ["integer", "string"], "description": "Body character offset (default 0)."},
                    "limit": {"type": ["integer", "string"], "description": "Body characters per page, 1..32000 (default 8000)."},
                    "revision": {"type": "string", "description": "Previous page revision; prevents mixing pages from different revisions."},
                    "recall_id": {"type": "string", "description": "recall_id of the recall this slug came from, if any; improves future ranking."},
                },
            },
            outputSchema=READ_OUTPUT,
        ),
    ]
    # Same arguments as save, so a proposal can be approved without reshaping it.
    save_schema = next(t.input_schema for t in tools if t.name == "save")
    tools.append(types.Tool(
        name="propose",
        annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        outputSchema=PROPOSE_OUTPUT,
        description=(
            "Propose one durable fact for human review instead of saving it. Takes the same "
            "arguments as save. Nothing is written until a person approves it in the inbox; the "
            "reply gives the candidate id and a preview of what saving would do (related notes, "
            "near-duplicates, conflicts). Use it for facts you are unsure of, or inferred ones."
        ),
        inputSchema=copy.deepcopy(save_schema),
    ))
    tools.append(types.Tool(
        name="publish",
        annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        outputSchema=PUBLISH_OUTPUT,
        description=(
            "Publish one of your notes to a shared (team) store you can write, with provenance "
            "(published_from: source store, slug, revision, publisher, time). Publishing the same note "
            "again updates the earlier copy; an unchanged note is a no-op. The target store may hold "
            "it for review first (action: queued). Personal fields (pinned, usage, saved_by) never travel."
        ),
        inputSchema={
            "type": "object", "required": [], "properties": {
                "slug": {"type": "string", "description": "Slug of the note to publish."},
                "target_store": {"type": "string", "description": "Store to publish into."},
                "store": {"type": "string", "description": "Store the note is in; defaults to your own."},
                "profile": {"type": "string", "description": "Alias for store (the source store)."},
                "allow_decrypted_publish": {"type": ["boolean", "string"], "description": (
                    "Required to publish from an encrypted store into an unencrypted one: the copy is plaintext.")},
            },
        },
    ))
    return _drop_profile_when_multitenant(tools)


def _log_recall(profile: str, query: str, shaped: dict) -> str | None:
    """Record a served recall in the store's usage log; its recall_id, or None."""
    try:
        return usage.record_recall(_cfg_for(profile), profile, query, shaped)
    except Exception:
        return None     # usage logging must never fail a recall


def _federation(arguments: dict) -> list[str] | None:
    """Stores of a federated recall (memd.share), or None for an ordinary one."""
    from memd.share import resolve_stores
    if arguments.get("stores") is None and arguments.get("scope") in (None, ""):
        return None
    if isinstance(arguments.get("profile"), str) and arguments["profile"].strip():
        raise ValueError("send profile or stores/scope, not both")
    return resolve_stores(arguments.get("stores"), arguments.get("scope"), operation="recall")


def _federated_recall(args: dict, stores: list[str]) -> types.CallToolResult:
    from memd.share import federated_recall

    def one(store, query, k, **kw):
        return _core_recall(query, profile=store, k=k, cfg=_cfg_for(store), **kw)
    out = federated_recall(args, stores, one)
    shaped = out["shaped"]
    shaped["text"] = shaped.get("text") or "No matching memory notes."
    # One recall_id, logged in each store's usage log with that store's matches.
    try:
        recall_id = usage.record_federated(_cfg_for, args["query"], shaped)
    except Exception:
        recall_id = None
    shaped.update(ok=True, profile=stores[0], query=args["query"], recall_id=recall_id,
                  stores=out["stores"], stores_skipped=out["stores_skipped"])
    return _result(shaped, text=shaped["text"])


def _ask(arguments: dict) -> types.CallToolResult:
    """ask (memd.ask): recall's authorization and store rules, then one cited answer."""
    from memd.ask import DEFAULT_K, ask
    args = normalize_recall_args({"k": DEFAULT_K, **arguments})
    stores = _federation(arguments)
    federated = stores is not None
    if stores is None:
        stores = [resolve_profile(enforce("recall", args.get("profile")))]

    def one(store, query, k, **kw):
        return _core_recall(query, profile=store, k=k, cfg=_cfg_for(store), **kw)
    out = ask(args["query"], stores=stores, recall_one=one, cfg_for=_cfg_for,
              federated=federated,
              k=args["k"], host=args.get("host"), tags=args.get("tags"))
    return _result(out, text=out["text"])


def _result(payload: dict, *, text: str | None = None, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text if text is not None else json.dumps(payload))],
        structuredContent=payload, isError=error,
    )


def _propose(fact: dict, profile: str, *, source: str = "agent") -> dict:
    """File a candidate in the store's review inbox (memd.inbox)."""
    from memd import inbox
    from memd.actor import get_actor
    return inbox.propose(fact, profile, cfg=_cfg_for(profile), source=source, proposer=get_actor())


def _call_tool_sync(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    if name == "recall":
        args = normalize_recall_args(arguments or {})
        stores = _federation(arguments or {})
        if stores is not None:
            return _federated_recall(args, stores)
        profile = resolve_profile(enforce("recall", args.get("profile")))
        kw = {}
        if not args.get("include_core", True):
            kw["include_core"] = False
        for key in ("host", "tags", "include_archived"):
            if args.get(key):
                kw[key] = args[key]
        notes = [n.to_dict() for n in
                 _core_recall(args["query"], profile=profile, k=args["k"], cfg=_cfg_for(profile), **kw)]
        from memd.share import annotate_upstream
        annotate_upstream(notes, operation="recall", default_store=profile)
        shaped = render_result(
            notes, top_n=args["k"], query=args["query"],
            max_chars=args.get("max_chars", 14000), core_limit=args.get("core_limit"),
            include_core=args.get("include_core", True),
        )
        # The rendered notes MUST live in structuredContent: clients that honour
        # outputSchema (Claude Code) show the model only the structured payload.
        from memd.recall import CURRENT_INTENT
        if args["query"] and CURRENT_INTENT.search(args["query"]):
            from memd.facts import append_current_facts
            from memd.profiles import guard_paths
            try:
                cfg = _cfg_for(profile)
                append_current_facts(shaped, query=args["query"], max_chars=args.get("max_chars", 14000),
                                     db_path=guard_paths(profile, cfg.clone, cfg.db)[1])
            except Exception:
                pass    # the facts block is optional; recall itself already succeeded
        shaped["text"] = shaped.get("text") or "No matching memory notes."
        shaped.update(ok=True, profile=profile, query=args["query"],
                      recall_id=_log_recall(profile, args["query"], shaped))
        return _result(shaped, text=shaped["text"])

    if name == "ask":
        return _ask(arguments or {})

    if name == "save":
        from memd.access import authorize
        fact = normalize_fact(arguments or {})
        profile = resolve_profile(enforce("save", fact.pop("profile", None)))
        authorize(profile, write=True)
        from memd import inbox
        if inbox.save_routes_to_inbox():
            # MEMD_SAVE_MODE=inbox: an agent token's save waits for review.
            return _result(inbox.queued_receipt(_propose(fact, profile, source="save")))
        receipt = _core_save(fact, profile=profile, cfg=_cfg_for(profile)).to_dict()
        failed = receipt.get("saved") is False or receipt.get("ok") is False
        return _result(receipt, error=failed)

    if name == "propose":
        from memd.access import authorize
        fact = normalize_fact(arguments or {})
        profile = resolve_profile(enforce("save", fact.pop("profile", None)))
        authorize(profile, write=True)
        return _result(_propose(fact, profile))

    if name == "timeline":
        from memd.facts import timeline
        arguments = arguments or {}
        profile = resolve_profile(enforce("recall", arguments.get("profile")))
        subject = arguments.get("subject") or arguments.get("query") or ""
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("subject is required: the service, host or setting to look up")
        out = timeline(subject.strip(), arguments.get("predicate") or None, arguments.get("at"),
                       profile=profile, cfg=_cfg_for(profile))
        return _result(out, text=out["text"])

    if name == "read":
        from memd.share import annotate_read, store_argument
        profile = resolve_profile(enforce("read", store_argument(arguments)))
        cfg = _cfg_for(profile)
        receipt = _core_read(
            arguments.get("slug", ""), profile=profile, cfg=cfg,
            offset=arguments.get("offset", 0), limit=arguments.get("limit", 8000),
            revision=arguments.get("revision"),
        )
        usage.record_read(cfg, profile, receipt, arguments.get("recall_id"))
        return _result(annotate_read(receipt, store=profile))

    if name == "publish":
        from memd.normalize import _optional_bool
        from memd.share import publish, store_argument
        arguments = arguments or {}
        allow = _optional_bool(arguments.get("allow_decrypted_publish", False))
        if allow is None:
            raise ValueError("allow_decrypted_publish must be true or false")
        out = publish(arguments.get("slug"), arguments.get("target_store"),
                      source_store=store_argument(arguments), allow_decrypted_publish=allow)
        return _result(out, error=not out.get("ok"))

    raise ValueError(f"unknown tool {name}")


async def call_tool(name: str, arguments: dict[str, Any], *, request: Any = None) -> types.CallToolResult:
    """Run one tool call; `request` is the HTTP request it arrived on, if any.

    Every failure, including refused authentication, becomes an error-flagged
    result rather than a protocol error, so the calling model can read it.
    """
    import time
    from memd import metrics
    start = time.perf_counter()
    result = await _call_tool(name, arguments, request=request)
    metrics.observe("memd_mcp_tool_duration_seconds", time.perf_counter() - start, tool=name)
    metrics.inc("memd_mcp_tool_calls_total", tool=name,
                outcome="error" if getattr(result, "is_error", False) else "ok")
    return result


async def _call_tool(name: str, arguments: dict[str, Any], *, request: Any = None) -> types.CallToolResult:
    # The core uses synchronous SQLite/Git/HTTP APIs. Run the entire operation
    # off the ASGI event loop so another client's tool call remains responsive.
    from memd import access
    from memd.actor import set_actor
    marker = remote = delegated = None
    try:
        # MCP can dispatch tools in a transport-owned task. Re-establish the
        # identity from its actual HTTP request, never from tool arguments.
        if request is not None:
            headers = dict(request.headers)
            principal = await asyncio.to_thread(access.authenticate_headers, headers)
            if principal is not None:
                set_actor(principal.label)
            else:
                # Token registry, Kasm or OIDC bearer. Authenticate in THIS task
                # (not a worker thread) so the registry principal and the bound
                # identity it sets are inherited by the tool thread below.
                from memd.mcp_http import _authenticate
                label = _authenticate(headers.get("authorization"))
                if not label:
                    raise PermissionError("Invalid or revoked access token.")
                set_actor(label)
                delegated = access.delegated.set(True)
            marker = access.current.set(principal)
            remote = access.remote_request.set(True)
        return await asyncio.to_thread(_call_tool_sync, name, arguments or {})
    except Exception as exc:
        return _result({"ok": False, "error": str(exc), "error_type": type(exc).__name__}, error=True)
    finally:
        if marker is not None:
            access.current.reset(marker)
        if remote is not None:
            access.remote_request.reset(remote)
        if delegated is not None:
            access.delegated.reset(delegated)


def _text_error(text: str) -> types.CallToolResult:
    """A text-only error result, the shape 1.x gave failures outside call_tool."""
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


async def _on_list_tools(ctx: ServerRequestContext, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    return types.ListToolsResult(tools=await list_tools())


async def _on_call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    """Protocol adapter: validate against the advertised schema, then dispatch.

    The SDK's lowlevel server no longer validates arguments or turns handler
    exceptions into error results, so both are done here to keep the wire
    contract unchanged. The advertised schema is what the client validated
    against, so a call that reaches this point and fails it is malformed.
    """
    arguments = params.arguments or {}
    try:
        tool = next((t for t in await list_tools() if t.name == params.name), None)
        if tool is not None:
            try:
                jsonschema.validate(instance=arguments, schema=tool.input_schema)
            except jsonschema.ValidationError as exc:
                return _text_error(f"Input validation error: {exc.message}")
        return await call_tool(params.name, arguments, request=ctx.request)
    except Exception as exc:
        return _text_error(str(exc))


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("memd")
    except PackageNotFoundError:
        return ""


# One Server instance serves both transports; memd.mcp_http wraps this object.
server = Server("memd", version=_version(), on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


def main() -> None:
    import asyncio

    async def _run() -> None:
        from memd import refresh
        import logging
        try:
            cfg = _cfg_for(resolve_profile())
            await asyncio.to_thread(refresh.ensure_lexical, cfg)
            refresh.request_refresh(cfg)
        except Exception as exc:
            logging.getLogger(__name__).warning("startup memory refresh unavailable: %s", exc)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
