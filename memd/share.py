"""Team sharing with provenance: publish between stores, federated recall, upstream drift.

Three operations, each built on the access control that already confines a
single-store call (memd.profiles.resolve_profile over memd.registry.enforce, the
account grants of memd.access, the bound identity and the instance lock). No
store is reachable here that the same caller could not already reach by naming
it as ``profile`` on an ordinary call, and every store is resolved separately:

* **publish** copies one note from a store the caller may read (the source,
  default their own) into a store they may write (the target) through the
  target's normal save path, or its review inbox when the target has
  ``publish_review`` on (the default) or agent saves are queued
  (MEMD_SAVE_MODE=inbox). The copy carries ``published_from: {store, slug,
  revision, by, at}``; publishing the same note again updates that copy (found
  by its provenance, never by title), and an unchanged source is a no-op.
* **federated recall** runs recall once per requested store (``stores`` list, or
  ``scope: "all"`` for every store the caller is granted), in parallel with a
  cap and one shared deadline, fuses the per-store rankings by reciprocal rank
  and labels every result with its ``store``. Without either argument recall is
  exactly the single-store call it always was.
* **upstream drift**: a published copy whose source note has changed since is
  ``behind``. Read and recall report it (``upstream``) only when the caller may
  also read the source store; ``mem-share status`` lists every published note of
  a store with its state.

What travels with a published note (TRAVELS): title, body, host, tags,
importance, description, source, observed_at, verified_at, volatility and the
``verify`` probes. What never travels: the author's ``saved_by`` (the copy is
stamped with the publisher), ``pinned`` (a personal core-index choice),
``last_used`` and anything from the usage log (personal usage data lives outside
notes and is never read here), ``superseded_by`` and ``grounding`` (the target
decides its own lifecycle and grounding), mem-verify's ``verification`` marker
(host-specific check state), a ``published_from`` the source itself carried, and
every other frontmatter key (summary ``sources``, ``cluster`` and the like): the
list is an allowlist, so a field added later stays private until it is added.

An encrypted source is published into a plaintext target only with an explicit
``allow_decrypted_publish``: that copy exposes the plaintext to whoever can read
the target's repository.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextvars
import datetime
import json
import sqlite3
import sys
from pathlib import Path

TRAVELS = ("title", "body", "host", "tags", "importance", "description", "source",
           "observed_at", "verified_at", "volatility", "verify")
NEVER_TRAVELS = ("saved_by", "pinned", "last_used", "superseded_by", "grounding", "profile",
                 "verification", "published_from")
MAX_STORES = 16             # stores one federated recall may name or expand to
FEDERATION_WORKERS = 4      # per-store recalls running at once
FEDERATION_SLACK_MS = 1500  # beyond the embed + rerank deadlines, for SQLite and Git
RRF_K = 60


# Set only by main(): the process is the local operator's CLI (see authorized_store).
_operator: contextvars.ContextVar[bool] = contextvars.ContextVar("memd_share_operator", default=False)


class StoreRefused(PermissionError):
    """A store the caller may not use for this operation (or no such store).

    Deliberately one message for "not granted" and "does not exist", so a
    refused name is not an existence oracle, and the rejected value is never
    echoed: it can be a control-character or traversal probe.
    """


class DecryptedPublishRefused(ValueError):
    """An encrypted source note would be written into a plaintext store."""


# --------------------------------------------------------------------------- access


def authorized_store(requested: str | None, operation: str, *, what: str = "store") -> str:
    """The store `requested` resolves to for `operation`, through the ordinary guards.

    Exactly the path a single-store call takes for its ``profile`` argument:
    the token registry's scope (memd.registry.enforce), then the bound identity,
    the instance lock and the account grants (memd.profiles.resolve_profile).
    """
    from memd import access
    from memd.profiles import resolve, resolve_profile
    from memd.registry import enforce
    if requested is not None and (not isinstance(requested, str) or not requested.strip()):
        raise StoreRefused(f"{what} is not a store this caller may use")
    try:
        if _operator.get() and not access.remote_request.get() and access.current.get() is None:
            # The local mem-share CLI: the operator already has every store on
            # disk, and MEMD_ENFORCE_PROFILE confines remote callers of the
            # serving instance, not them. The name must still resolve to a
            # registered store, and its paths are still guarded (_paths).
            from memd.config import Config
            return resolve((requested or Config.from_env().profile).strip())["profile"]
        return resolve_profile(enforce(operation, requested))
    except (PermissionError, KeyError, ValueError) as exc:
        # ProfileMismatch/PermissionError (not granted), registry Denied and
        # InvalidStoreName (ValueError), UnknownProfile (KeyError).
        raise StoreRefused(f"{what} is not a store this caller may use") from exc


def store_argument(arguments: dict) -> str | None:
    """`store` (memd.share's name) or its older spelling `profile`; both must agree."""
    store, profile = arguments.get("store"), arguments.get("profile")
    if store is not None and (not isinstance(store, str) or not store.strip()):
        raise ValueError("store must be a store name")
    if store is not None and profile is not None and store != profile:
        raise ValueError("store and profile name different stores; send one")
    return store if store is not None else profile


def writable_store(requested: str | None, *, what: str = "target_store") -> str:
    from memd.access import authorize
    store = authorized_store(requested, "save", what=what)
    try:
        authorize(store, write=True)
    except PermissionError as exc:
        raise StoreRefused(f"{what} is not a store this caller may write") from exc
    return store


def may_read(store: str, operation: str = "read") -> str | None:
    """`store` resolved when this caller may read it, else None (never raises)."""
    try:
        return authorized_store(store, operation)
    except StoreRefused:
        return None


def _cfg(profile: str):
    from memd.mcp import _cfg_for
    return _cfg_for(profile)


def _paths(profile: str):
    from memd.profiles import guard_paths
    cfg = _cfg(profile)
    clone, db = guard_paths(profile, cfg.clone, cfg.db)
    return cfg, clone, db


def publish_review(store: str) -> bool:
    """Whether a note published into `store` waits in its review inbox (default True).

    The store's ``publish_review`` setting (Settings / PUT /admin/stores), else
    ``MEMD_<STORE>_PUBLISH_REVIEW`` for a store configured by environment.
    """
    from memd import control
    if control.enabled():
        row = control.store(store)
        if row and "publish_review" in row["config"]:
            return row["config"]["publish_review"] is not False
    key = "MEMD_" + "".join(c if c.isalnum() else "_" for c in store.upper()) + "_PUBLISH_REVIEW"
    from memd.profiles import effective_environment
    value = (effective_environment().get(key) or "").strip().lower()
    return value not in {"0", "false", "no", "off"}


# --------------------------------------------------------------------------- provenance


def provenance_of(metadata) -> dict | None:
    """A well-formed ``published_from`` mapping from note metadata, else None."""
    value = (metadata or {}).get("published_from") if isinstance(metadata, dict) else None
    if not isinstance(value, dict):
        return None
    if not all(isinstance(value.get(k), str) and value.get(k) for k in ("store", "slug", "revision")):
        return None
    return value


class _Revisions:
    """Current revision of source notes, looked up once per store per call.

    ``tree`` reads the authoritative Git working tree (read, mem-share status);
    ``index`` reads the source's SQLite index read-only (recall's hot path,
    where decrypting a whole store per result is too slow; it may lag a moment).
    """

    def __init__(self, mode: str):
        self.mode = mode
        self.cache: dict[str, dict[str, str | None] | None] = {}

    def lookup(self, store: str, slug: str) -> tuple[bool, str | None]:
        """(known, revision): known False when the store could not be consulted."""
        if store not in self.cache:
            try:
                self.cache[store] = self._load(store)
            except Exception:
                self.cache[store] = None
        table = self.cache[store]
        if table is None:
            return False, None
        if slug in table:
            return True, table[slug]
        return self.mode == "tree", None

    def _load(self, store: str) -> dict[str, str | None]:
        _, clone, db = _paths(store)
        if self.mode == "index":
            if not Path(db).exists():
                raise FileNotFoundError(db)
            conn = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                return {slug: blob for slug, blob in conn.execute(
                    "SELECT slug, git_blob FROM notes WHERE superseded_by IS NULL")}
            finally:
                conn.close()
        from memd.store import assert_readable_tree, clone_lock, list_notes
        with clone_lock(clone):
            assert_readable_tree(clone)
            return {n.slug: (None if n.superseded_by else n.git_blob) for n in list_notes(clone)}


def upstream(provenance: dict | None, *, own: str, operation: str, revisions: _Revisions) -> dict | None:
    """Drift of a published copy against its source, or None.

    None unless the caller may ALSO read the source store: a published copy
    names its source, but whether that note changed, moved or vanished is
    information about the source store and is shown only to its readers.
    """
    if provenance is None:
        return None
    source = may_read(provenance["store"], operation)
    if source is None or source == own:
        return None
    known, current = revisions.lookup(source, provenance["slug"])
    if not known:
        status = "unknown"
    elif current is None:
        status = "missing"
    else:
        status = "current" if current == provenance["revision"] else "behind"
    out = {"store": source, "slug": provenance["slug"], "published_revision": provenance["revision"],
           "current_revision": current, "status": status}
    if status == "behind":
        out["hint"] = f"the source changed after publishing; publish({provenance['slug']!r}, {own!r}, store={source!r}) updates this copy"
    return out


def annotate_upstream(notes: list[dict], *, operation: str = "recall", default_store: str | None = None) -> None:
    """Add ``upstream`` to recalled note dicts that are published copies (in place).

    Never raises: drift is advisory and must not fail a recall.
    """
    revisions = _Revisions("index")
    for note in notes:
        try:
            prov = provenance_of(note.get("metadata"))
            if prov is None:
                continue
            state = upstream(prov, own=note.get("store") or default_store or "", operation=operation,
                             revisions=revisions)
            if state:
                note["upstream"] = state
        except Exception:
            continue


def annotate_read(receipt: dict, *, store: str) -> dict:
    """Label a read receipt with its store and, for a published copy, its drift."""
    receipt["store"] = store
    try:
        state = upstream(provenance_of({"published_from": receipt.get("published_from")}), own=store,
                         operation="read", revisions=_Revisions("tree"))
        if state:
            receipt["upstream"] = state
    except Exception:
        pass
    return receipt


# --------------------------------------------------------------------------- publish


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _free_slug(base: str, taken: set[str]) -> str:
    from memd.slug import slugify, validate_slug
    try:
        validate_slug(base)
    except ValueError:
        base = slugify(base) or "published-note"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def travelling_fact(note, warnings: list[str] | None = None) -> dict:
    """The fields of `note` that travel to another store (TRAVELS), as a save fact."""
    fact = {"title": note.title, "body": note.body, "host": note.host or "any",
            "tags": list(note.tags), "importance": note.importance}
    for key in ("description", "source", "observed_at", "verified_at", "volatility"):
        value = getattr(note, key)
        if isinstance(value, str) and value.strip():
            fact[key] = value
    probes = (note.metadata or {}).get("verify")
    if probes:
        from memd.verify import ProbeError, canonical
        try:
            fact["verify"] = canonical(probes)
        except ProbeError:
            if warnings is not None:
                warnings.append("The source note's verify probes are malformed and were not published.")
    return fact


def publish(slug: str, target_store: str, *, source_store: str | None = None,
            allow_decrypted_publish: bool = False) -> dict:
    """Copy one note to another store with provenance; see the module docstring.

    Raises StoreRefused (no read on the source, no write on the target),
    FileNotFoundError (no such live note), DecryptedPublishRefused and
    ValueError (bad arguments; save's own refusals, including RevisionConflict
    when the published copy changed during the call).
    """
    from memd.actor import get_actor
    from memd.codec import is_encrypted
    from memd.store import RETRACTED, assert_readable_tree, clone_lock, list_notes

    if not isinstance(slug, str) or not slug.strip():
        raise ValueError("publish requires the slug of a note in the source store")
    if not isinstance(target_store, str) or not target_store.strip():
        raise ValueError("publish requires target_store: the store to publish into")
    if not isinstance(allow_decrypted_publish, bool):
        raise ValueError("allow_decrypted_publish must be true or false")
    slug = slug.strip()
    source = authorized_store(source_store, "read", what="store")
    target = writable_store(target_store)
    if source == target:
        raise ValueError("the target store is the note's own store; publish copies between stores")

    _, src_clone, _ = _paths(source)
    tgt_cfg, tgt_clone, _ = _paths(target)
    with clone_lock(src_clone):
        assert_readable_tree(src_clone)
        note = next((n for n in list_notes(src_clone) if n.slug == slug), None)
    if note is None or note.superseded_by == RETRACTED:
        raise FileNotFoundError(f"no memory note with slug {slug!r} in the source store")
    if note.superseded_by:
        raise ValueError(f"{slug!r} was superseded by {note.superseded_by!r}; publish the live note")
    warnings: list[str] = []
    if is_encrypted(src_clone) and not is_encrypted(tgt_clone):
        if not allow_decrypted_publish:
            raise DecryptedPublishRefused(
                "the source store is encrypted and the target store is not: publishing writes this "
                "note's plaintext where the target's Git host can read it. Pass "
                "allow_decrypted_publish=true to publish it anyway")
        warnings.append("Published the plaintext of a note from an encrypted store into an unencrypted store.")

    with clone_lock(tgt_clone):
        assert_readable_tree(tgt_clone)
        existing_notes = list_notes(tgt_clone)
    copy = next((n for n in existing_notes if not n.superseded_by
                 and (p := provenance_of(n.metadata)) is not None
                 and p["store"] == source and p["slug"] == note.slug), None)
    src_ref = {"store": source, "slug": note.slug, "revision": note.git_blob}
    if copy is not None and provenance_of(copy.metadata)["revision"] == note.git_blob:
        return {"ok": True, "action": "unchanged", "queued": False, "source": src_ref,
                "target": {"store": target, "slug": copy.slug, "revision": copy.git_blob},
                "published_from": provenance_of(copy.metadata), "receipt": None,
                "warnings": warnings + ["Already published at this revision; nothing to do."]}

    fact = travelling_fact(note, warnings)
    if copy is not None:
        fact["slug"] = copy.slug
        fact["expected_revision"] = copy.git_blob
    else:
        fact["slug"] = _free_slug(note.slug, {n.slug for n in existing_notes})
    provenance = {**src_ref, "by": get_actor() or "local", "at": _now()}
    target_ref = {"store": target, "slug": fact["slug"]}

    from memd import inbox
    if publish_review(target) or inbox.save_routes_to_inbox():
        proposed = inbox.propose(fact, target, cfg=tgt_cfg, source="publish", proposer=get_actor(),
                                 provenance=provenance)
        return {"ok": True, "action": "queued", "queued": True, "inbox_id": proposed["id"],
                "duplicate": proposed.get("duplicate", False), "source": src_ref,
                "target": {**target_ref, "revision": None}, "published_from": provenance,
                "receipt": None, "lint": proposed.get("lint"),
                "warnings": warnings + [f"Waiting for review in the {target!r} inbox (candidate "
                                        f"{proposed['id']}); nothing is written until it is approved."]}
    from memd.save import publishing, save
    with publishing(provenance):
        receipt = save(fact, target, cfg=tgt_cfg).to_dict()
    ok = receipt.get("saved") is not False and receipt.get("ok") is not False
    return {"ok": ok, "action": "updated" if copy is not None else "published", "queued": False,
            "source": src_ref, "target": {**target_ref, "slug": receipt.get("slug") or fact["slug"],
                                          "revision": receipt.get("revision")},
            "published_from": provenance, "receipt": receipt,
            "warnings": warnings + list(receipt.get("warnings") or [])}


# --------------------------------------------------------------------------- federated recall


def readable_stores(operation: str = "recall") -> list[str]:
    """Every store this caller may recall from, their own (default) store first.

    An administered account or token expands to its granted stores, each still
    resolved through the ordinary guards; any other caller (a bound identity, a
    registry or legacy token, the local CLI) has exactly its one store.
    """
    from memd import access
    first = authorized_store(None, operation)
    out = [first]
    principal = access.current.get()
    if principal is not None and not principal.id.startswith("legacy:"):
        for name in sorted(principal.grants):
            if len(out) >= MAX_STORES:
                break
            resolved = may_read(name, operation)
            if resolved is not None and resolved not in out:
                out.append(resolved)
    return out


def resolve_stores(stores=None, scope=None, *, operation: str = "recall") -> list[str] | None:
    """The stores a federated recall covers, or None for an ordinary recall.

    `stores` is a list (or comma-separated string) of store names; each one is
    resolved exactly like a ``profile`` argument and one refusal refuses the
    whole call. ``scope: "all"`` expands to readable_stores().
    """
    if stores is None and scope in (None, ""):
        return None
    if stores is not None and scope not in (None, ""):
        raise ValueError("send either stores or scope, not both")
    if stores is not None:
        if isinstance(stores, str):
            stores = stores.split(",")
        if not isinstance(stores, list) or not stores:
            raise ValueError("stores must be a nonempty list of store names")
        if len(stores) > MAX_STORES:
            raise ValueError(f"at most {MAX_STORES} stores per recall")
        out: list[str] = []
        for i, name in enumerate(stores):
            if not isinstance(name, str) or not name.strip():
                # None would otherwise mean "the caller's default store".
                raise StoreRefused(f"stores[{i}] is not a store this caller may use")
            resolved = authorized_store(name, operation, what=f"stores[{i}]")
            if resolved not in out:
                out.append(resolved)
        return out
    if scope != "all":
        raise ValueError('scope must be "all" (or send stores)')
    return readable_stores(operation)


def deadline_ms() -> int:
    """One budget for the whole federated recall: embed + rerank deadlines + slack."""
    from memd.config import Config
    from memd.profiles import effective_environment
    env = effective_environment()
    override = (env.get("MEMD_FEDERATED_DEADLINE_MS") or "").strip()
    if override:
        try:
            return max(100, min(30000, int(float(override))))
        except (TypeError, ValueError, OverflowError):
            pass
    cfg = Config.from_env(env, env_file=None)
    return cfg.embed_deadline_ms + cfg.rerank_deadline_ms + FEDERATION_SLACK_MS


def _as_dict(note) -> dict:
    to_dict = getattr(note, "to_dict", None)
    return dict(to_dict()) if callable(to_dict) else dict(note)


def federate(stores: list[str], recall_one, *, k: int, budget_ms: int | None = None
             ) -> tuple[list[dict], list[dict]]:
    """Run ``recall_one(store)`` per store in parallel; fuse and label the results.

    Returns (notes, skipped). Each note dict gains ``store``. Core entries keep
    store order; query matches are fused by reciprocal rank across stores
    (1/(RRF_K + rank) within their own store, earlier store on a tie) and cut
    to `k`. A store that fails or misses the shared deadline is skipped and
    reported, never allowed to fail the others.
    """
    budget = deadline_ms() if budget_ms is None else budget_ms
    results: dict[str, list] = {}
    skipped: list[dict] = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(FEDERATION_WORKERS, len(stores))),
                                                 thread_name_prefix="memd-federated")
    try:
        # Each worker runs in a copy of the caller's context, so any code that
        # consults the request's identity sees the same caller.
        futures = {pool.submit(contextvars.copy_context().run, recall_one, store): store for store in stores}
        done, _ = concurrent.futures.wait(futures, timeout=budget / 1000)
        for future, store in futures.items():
            if future not in done:
                skipped.append({"store": store, "reason": "deadline"})
                continue
            try:
                results[store] = list(future.result())
            except Exception as exc:
                skipped.append({"store": store, "reason": type(exc).__name__})
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    core: list[dict] = []
    ranked: list[tuple[float, int, int, dict]] = []
    for order, store in enumerate(stores):
        rank = 0
        for note in results.get(store, []):
            item = _as_dict(note)
            item["store"] = store
            try:
                important = int(item.get("importance") or 0) >= 4
            except (TypeError, ValueError):
                important = False
            matched = bool(item["matched"]) if "matched" in item else not important
            if matched:
                ranked.append((1.0 / (RRF_K + rank + 1), order, rank, item))
                rank += 1
            else:
                core.append(item)
    ranked.sort(key=lambda row: (-row[0], row[1], row[2]))
    return core + [row[3] for row in ranked[:max(1, k)]], skipped


def federated_recall(args: dict, stores: list[str], recall_one, *, budget_ms: int | None = None) -> dict:
    """Recall across `stores` and render: {notes, shaped, stores, stores_skipped}.

    `args` is memd.normalize.normalize_recall_args output; ``recall_one(store,
    query, k, **filters)`` runs one ordinary single-store recall.
    """
    from memd.render import render_result
    kw = {} if args.get("include_core", True) else {"include_core": False}
    for key in ("host", "tags", "include_archived"):
        if args.get(key):
            kw[key] = args[key]
    query, k = args["query"], args["k"]
    notes, skipped = federate(stores, lambda store: recall_one(store, query, k, **kw), k=k,
                              budget_ms=budget_ms)
    annotate_upstream(notes, operation="recall")
    shaped = render_result(notes, top_n=k, max_chars=args.get("max_chars", 14000), query=query,
                           core_limit=args.get("core_limit"), include_core=args.get("include_core", True))
    missed = {row["store"] for row in skipped}
    return {"notes": notes, "shaped": shaped, "stores": [s for s in stores if s not in missed],
            "stores_skipped": skipped}


# --------------------------------------------------------------------------- CLI


def status(store: str) -> dict:
    """Every published note in `store` with its drift against the source."""
    from memd.store import assert_readable_tree, clone_lock, list_notes
    store = authorized_store(store, "read")
    _, clone, _ = _paths(store)
    with clone_lock(clone):
        assert_readable_tree(clone)
        notes = list_notes(clone)
    revisions = _Revisions("tree")
    items = []
    for note in notes:
        prov = provenance_of(note.metadata)
        if prov is None or note.superseded_by:
            continue
        state = upstream(prov, own=store, operation="read", revisions=revisions)
        items.append({"slug": note.slug, "title": note.title, "published_from": prov,
                      "status": state["status"] if state else "unknown",
                      "current_revision": state["current_revision"] if state else None})
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"ok": True, "store": store, "published": items, "counts": counts}


def main(argv: list[str] | None = None) -> int:
    from memd.config import Config
    ap = argparse.ArgumentParser(prog="mem-share", description=(
        "Publish notes between stores with provenance, and show which published copies are "
        "behind their source."))
    sub = ap.add_subparsers(dest="command", required=True)
    st = sub.add_parser("status", help="published notes of a store and whether each is behind its source")
    st.add_argument("--store", help="store to inspect (default MEMD_PROFILE)")
    st.add_argument("--json", action="store_true")
    pb = sub.add_parser("publish", help="publish (or update) one note into another store")
    pb.add_argument("slug")
    pb.add_argument("target_store")
    pb.add_argument("--from", dest="source", help="source store (default MEMD_PROFILE)")
    pb.add_argument("--allow-decrypted-publish", action="store_true",
                    help="allow an encrypted store's note to be written into a plaintext store")
    pb.add_argument("--json", action="store_true")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    marker = _operator.set(True)
    try:
        if args.command == "status":
            out = status(args.store or Config.from_env().profile)
            if args.json:
                print(json.dumps(out))
                return 0
            print(f"{out['store']}: {len(out['published'])} published note(s)"
                  + "".join(f", {n} {s}" for s, n in sorted(out["counts"].items())))
            for item in out["published"]:
                prov = item["published_from"]
                print(f"  {item['status']:<8} {item['slug']}  <- {prov['store']}/{prov['slug']} "
                      f"(published {prov.get('at') or '?'} by {prov.get('by') or '?'})")
            if out["counts"].get("behind"):
                print("republish with: mem-share publish <source slug> <store> --from <source store>")
            return 0
        out = publish(args.slug, args.target_store, source_store=args.source or Config.from_env().profile,
                      allow_decrypted_publish=args.allow_decrypted_publish)
        if args.json:
            print(json.dumps(out))
        else:
            target = out["target"]
            print(f"{out['action']}: {out['source']['store']}/{out['source']['slug']} -> "
                  f"{target['store']}/{target['slug']}")
            for warning in out.get("warnings") or []:
                print(f"  warning: {warning}")
        return 0 if out["ok"] else 1
    except (FileNotFoundError, PermissionError, RuntimeError, ValueError, OSError) as exc:
        print(f"mem-share: {exc}", file=sys.stderr)
        return 1
    finally:
        _operator.reset(marker)


if __name__ == "__main__":
    raise SystemExit(main())
