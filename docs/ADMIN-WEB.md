# memd access administration

Phase 1 manages automation access tokens. Memory browsing/editing/retraction is
reserved for a later per-user interface. The control plane never opens user stores.

## Authentication and permissions

The browser uses a separate confidential OIDC client `memd-web`, authorization
code flow with PKCE S256, state and nonce. The signed ID token must match the
browser client audience and issuer. Both ID token and userinfo must contain the
exact `memd-admins` group and `memd_admin_active: true`. That dedicated scope
mapping evaluates current group membership and user activation in authentik.
Agent OIDC, Kasm session tokens, static tokens and forwarding headers cannot
authenticate to `/admin` or `/admin/api/*`.

Browser cookies contain random opaque IDs, with Secure, HttpOnly, SameSite=Lax
and `__Host-` restrictions. Server sessions expire after 15 idle minutes or
8 hours absolute. No upstream refresh token is retained. A short-lived access
token is encrypted in the server session with a key derived from the dedicated
client secret; it is used only to recheck userinfo. Membership is rechecked at
least every five minutes, or before a mutation if the last check is over one
minute old. Provider failure fails closed and requests a new SSO login. Provider
access-token expiry also requires SSO again. CSRF tokens and an exact Origin
check protect mutations. Logout destroys the local session; the identity provider's session remains.

The HTML entry at `/` preserves POST/DELETE and event-stream MCP dispatch, and
`/mcp/` and agent discovery continue using their existing authentication.

## Token registry

`MEMD_CONTROL_DB=/data/control/control.db` enables the durable SQLite registry.
This state belongs outside `MEMD_STORES_ROOT`. Authentication then **never reads
the old token file**, including after revocation, corruption, or a missing DB.
Missing state stops container startup. Do not remove this setting to roll back.

New tokens contain a random immutable ID and a 256-bit random secret. Only a
SHA-256 verifier is stored. The credential is shown once in the creation/rotation
response and is not available in lists, audit exports or backups. It permits
explicit operations on an explicit store. Multiple stores are allowed only for
maintenance `reindex` and `stats` operations. Expiry defaults to 90 days and is
limited to 365 days. Rotation preserves scope and has a default 24-hour overlap
(0–168 hours), bounded by the old token's existing expiry. Legacy credentials
must be replaced explicitly; they cannot be rotated into new unrestricted ones.

Registry authorization is shared by REST and MCP and rechecked before each
operation. Already-running requests may finish. Last-observed use records accepted
authentication, not successful downstream completion. Changes and audit events
commit in one transaction. Reads (recall, read, export) are audited too, by actor
and store only, never the query text. Recall and read entries expire after
`MEMD_AUDIT_READ_DAYS` (default 90) and are left out of the admin page's audit list
so they do not bury administrative events; exports and admin events are kept.
Revision checks reject stale forms; UUID operation
IDs prevent duplicate issuance after a lost response. A duplicate response cannot
recover the secret: inspect the list, revoke the inaccessible credential and
create a replacement. Metadata edits do not expand permissions or extend expiry.

## Configuration and migration

Required browser settings:

```text
MEMD_CONTROL_DB=/data/control/control.db
MEMD_WEB_OIDC_ISSUER=https://auth.example.com/application/o/memd-web/
MEMD_WEB_CLIENT_ID=memd-web
MEMD_WEB_CLIENT_SECRET_FILE=/run/memd/web-client-secret
MEMD_PUBLIC_URL=https://memd.example.com
```

Provision only the new application and group binding in the identity
provider. The callback is exactly `/auth/callback`; grants
are authorization code only. The script writes its confidential client secret
to a root-only file on the identity-provider host. Transfer it directly through SSH into the application host's
mounted secrets directory with ownership readable by container UID 10001.
Never write secrets into Git, synced cloud folders, service environment values or CLI args.

Before cutover, back up the old token file and deployment configuration in a
root-only directory on the application host. In an isolated new-image container mounting the
same `/data` volume and read-only old token file, run:

```sh
memd-control --db /data/control/control.db init
memd-control --db /data/control/control.db import-legacy
```

The import hashes credentials in process, preserves existing values and access,
and records legacy status. It runs once into an empty registry. Verify every
existing credential with the new verifier before switching the service image
and enabling `MEMD_CONTROL_DB`. Do not revoke a human's legacy credential until
that person's replacement OIDC flow has been proven.

## Recovery and backups

The image ships `memd-control list`, `activity`, `issue`, `revoke` and `backup`.
Issue requires an interactive terminal to avoid sending credentials to logs.
Revoke accepts an immutable ID and required current `--revision`. The old
`memd-token` file editor refuses changes when the registry is enabled.

Use the SQLite backup API, not `cp` of a live WAL database:

```sh
memd-control backup /data/control/backups/control-TIMESTAMP.db
```

The destination must not already exist. Backups include token verifiers,
revocations and audit history, with browser sessions and unfinished logins
removed. Keep the directory and backups private (0700 / 0600), and include them
in the cluster's protected backup system. Before restore, stop the service,
preserve the current DB/WAL/SHM, verify the selected backup's integrity and
timestamp, restore ownership, then restart and test revoked credentials. A
backup predating a revocation can restore that credential's access; reconcile
post-backup revocations before serving traffic. Never restore a stale backup as
an ordinary UI rollback. Roll back browser code only to a registry-aware image,
or disable `MEMD_WEB_OIDC_ISSUER` while keeping registry authentication active.

## Validation

Use Python 3.13. `tests/test_control.py` and `tests/test_admin_web.py` exercise
transactions, scopes, legacy cutover, restoration, signed OIDC code exchange,
PKCE/state/nonce, audience, current entitlement, CSRF and isolated admin APIs.
The existing suite retains its outbound network guard and isolated environments.
The separate HTTPS fixture and Playwright check in `tests/admin_browser_*` use
synthetic data and are not shipped in the image. Production builds use pinned
`requirements.lock`; regenerate deliberately with `uv pip compile`.
