# Personal memory console

Users can also connect personal tools through [Onboarding](PERSONAL-ONBOARDING.md),
which adds subject-bound personal tokens without granting token administration.

`/memories` lets current **memd-users** browse/search their own memories, edit
title/content/summary/tags, and retract active memories. It uses a separate
confidential OIDC application (`memd-users`), callback (`/user-auth/callback`),
scope (`memd-user-console`) and opaque cookie from the administrator console.
Admins need memd-users membership to use this surface too. Automation bearer
credentials and forwarded identity headers cannot create a browser session.

`/memories/inbox` shows memories proposed for the user's own store (the `propose`
tool, `MEMD_SAVE_MODE=inbox`, `mem-inbox distill`) with what saving each would do,
and lets the user correct and approve or reject them; see the README's Review inbox
section. Decisions use the same session, origin and CSRF checks as edits.

Configuration, in addition to the existing web service and control registry:

```yaml
MEMD_USER_OIDC_ISSUER: https://auth.example.invalid/application/o/memd-users/
MEMD_USER_CLIENT_ID: memd-users
MEMD_USER_CLIENT_SECRET_FILE: /run/memd/user-web-client-secret
MEMD_STORES_ROOT: /data/stores
```

Provision a dedicated confidential OIDC application in the identity provider. It binds only
memd-users and publishes current `groups` and `memd_user_active` claims. Keep
the secret outside the checkout; mount it read-only and readable by UID 10001.
The signed ID token and live userinfo must agree on the user's email and subject.
Current entitlement is rechecked after five minutes for reads or one minute for
mutations. Expired upstream access requires signing in again. Email changes
invalidate the session; they never retarget an existing session to another store.

The verified email selects the store using the same path guard as agent access.
No request can select a profile or another owner. An unprovisioned store returns
an empty list; merely browsing never provisions a repository. Searches are
keyword matches over the owner's Git notes, with 25 results per page. Content is
rendered as plain text, so saved markup cannot run scripts or load remote assets.

Edits and retractions require the last-read Git blob revision. Both hold the
existing clone lock, retain the filename, identity and source metadata, and commit
the verified owner and OIDC subject to history. Edits mark grounding unverified.
Stale forms receive 409 and preserve the draft until the user chooses to reload.

Retraction records the reserved `superseded_by: memd:retracted` marker. Such notes
are excluded from indexing/recall, agent reads and ordinary saves to that identity.
The browser still lists them under Retracted. The cache entry is evicted before
the Git write; even a failed post-commit indexing attempt cannot serve the old
content on subsequent recalls. In-flight recalls may already hold a prior
snapshot. Git sync and search status are reported separately from the durable
local save. A failed remote push leaves a pending local commit for normal retry.

Retraction is **not permanent erasure**: history, backups, and content already
copied into conversations remain. There is no restore or purge button in this
phase. Operational recovery must retain the control registry and use images that
understand retraction; an older agent read implementation can expose retired notes.

## Administrator owner suggestions

`/admin/api/owners` is gated by the existing administrator session. It reads
names, email and username from the `directory` extension of the existing trusted
`MEMD_KASM_USER_MAP` sync feed. Kasm UUIDs and active session identifiers are never
returned. The original map remains compatible with Kasm authentication. The
admin owner field suggests directory email addresses, while retaining free text
for service/team owners. This metadata does not grant permissions or choose stores.

Older feeds fall back to mapped email addresses; an unavailable feed leaves the
manual field usable. `memd.owners.backfill_legacy(Registry())` is an explicit,
audited deployment operation: it replaces only the migration placeholder when
the legacy label exactly matches one unique directory username or email. It
preserves reviewed/service owners, ambiguous labels, token verifiers and scopes.
Kasm's short-lived workspace access is separate from these automation records.

## Validation

`test_user_web.py`, `test_user_memory.py` and `test_owners.py` cover signed OIDC,
entitlement and identity changes, cookie separation, CSRF, owner isolation,
revision conflicts, source/history retention, retraction with failed indexing,
agent read/save refusal, path guards, directory fallback and conservative backfill.
`user_browser_fixture.py` / `user_browser_check.py` are isolated synthetic HTTPS
acceptance fixtures for browser behavior. Never expose their test login route in
production; they are not imported by the shipped service.
