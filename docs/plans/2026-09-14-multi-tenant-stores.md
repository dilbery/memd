# Plan 2, piece 1: multi-tenant stores

Implements the tenancy substrate from `the multi-tenant assessment (not published)` §9a.
It does **not** implement §9b's OIDC layer, and it ships no way for a caller to
prove who they are. Read "What this does not do" before wiring anything to it.

## What changed

| Area | Before | After |
|---|---|---|
| Registry | Two code-defined rows, `teama` and `teamb` | Those two rows, plus every valid store name under `MEMD_STORES_ROOT` |
| Store selection | Request `profile`, then the instance lock | Bound identity first; request and lock only when unbound |
| Path guard | Deny-by-default against two registered roots | Same, plus a structural rule inside the stores root |
| `profile` tool argument | Advertised and honoured | Not advertised when multi-tenant; refused when it disagrees with the caller |
| `MEMD_CLONE` / `MEMD_DB` | Global override wins | Not consulted at all when multi-tenant |

## Configuration

| Variable | Meaning |
|---|---|
| `MEMD_STORES_ROOT` | Turns multi-tenancy on. Stores live at `<root>/<store>/{clone,memd.db}`. Unset means the shipped single-store behaviour, unchanged. |
| `MEMD_STORES_REPO_TEMPLATE` | Optional. A git remote carrying `{store}`, for example `ssh://git@git.example.com:22/memory/{store}.git`. Empty means a store has no remote and is local-only, rather than being pointed at somebody else's repository by accident. |

Multi-tenancy is switched on by the presence of `MEMD_STORES_ROOT` rather than a
separate flag, so no existing deployment changes behaviour by taking this code.

## The security property

> User A cannot reach user B's store by any request field.

Three independent layers, in the order a request meets them.

1. **Name validation** (`memd/stores.py`). An identity becomes exactly one path
   segment or it is refused. Strict lower-case allowlist, no separators, no
   leading dot, length capped; then a resolved-parent check that the store is a
   direct child of the root.
2. **Selection** (`profiles.resolve_profile`). With an identity bound, the store
   IS the identity. A `profile` field is accepted only when it names that same
   store, and refused otherwise, including when it names `teama` and including
   when the instance lock says something else.
   With NO identity bound, the caller is a static token or the CLI. The
   instance lock still confines it: `MEMD_ENFORCE_PROFILE` is not merely a
   default that stops applying once a `profile` field appears. The pilot runs
   with the lock on and per-person static tokens live, so a lock that only
   supplied a default would let any of those tokens address any store the
   moment a stores root was set. Broad addressing, which piece 3's onboarding
   job needs, is a property of an unlocked instance.

3. **Path guard** (`profiles.assert_no_cross_profile`). Inside the stores root,
   ownership is decided by the first path component: `<root>/<owner>/...`
   belongs to `<owner>` and to nobody else. This runs before any override root
   is considered, so a clone handed in by a caller cannot rescue a path from it.

Layer 3 is deliberately structural rather than an enumeration of directories
that exist. If it enumerated, a store nobody had saved into yet would be owned
by no one, so whether A could reach B's path would depend on whether B had ever
written a note.

`tests/test_multitenant_cross_tenant.py` drives all of this through the REST
surface, the MCP tool surface and the real recall/save core against two seeded
git clones.

## What this does not do

- **No identity source.** `memd.identity.bind_identity` has no production call
  site. Every request today is unbound, so memd serves the one configured store
  exactly as it does now. §9b's OIDC resource-server layer is what will call it,
  with the `email` claim of a bearer whose signature, issuer, expiry and `memd`
  scope have been checked.
- **No trusted-proxy mode.** §9b item 5, for Open WebUI. Piece 2.
- **No token-label-to-store mapping, and none should be added.** Labels
  (`memd/actor.py`) are client attribution and decide nothing. Label-to-store is
  Option A in §9a, marked "fallback only".
- **No onboarding job.** Creating the Forgejo repo, the deploy key and the store
  directory is piece 3. A store directory here is created on first save.

## Deployment note, before turning this on

The pilot's compose file set `MEMD_CLONE` and `MEMD_DB` alongside
`MEMD_TEAMA_CLONE` and `MEMD_TEAMA_DB`. On a single-tenant instance both name the
same directory and it is harmless. The code now ignores the two global variables
whenever `MEMD_STORES_ROOT` is set, precisely so that turning multi-tenancy on
cannot collapse every identity onto one clone; remove them from the unit anyway,
so the file states one intent rather than two.

`teama`/`teamb` keep resolving from their `MEMD_<PROFILE>_*` variables even when
a stores root is configured. That is deliberate: the pilot store must not
silently relocate. It also means a mixed instance works, one shared operational
store beside the per-user ones, if that is what the shared-store decision lands
on.

## Known limitations, recorded not fixed

- `save.LAST_INDEX_ERROR` is a module-level global, not keyed by store. It is
  set and read within one save call, so it does not affect a receipt, but any
  future out-of-band reader of it would see the last store to fail, whichever
  that was.
- `server._health_cache` is documented "single profile per process" and still
  is. `/health` reports the configured store, not per-store health.
- The vector-refresh worker (`memd/refresh.py`) already keys its state on
  `(profile, db)`, and `store.clone_lock` is a flock on the clone path, so write
  serialisation is per store and needs no change. Reindex fan-out across many
  stores is bounded only by that lock; §9a flags it and it is not addressed here.
