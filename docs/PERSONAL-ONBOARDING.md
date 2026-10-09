# Personal onboarding

`/memories/onboarding`, under **Your workspace → Onboarding**, uses the existing
memd-users browser session. Users create a token per device/app, see its secret
once, test the connection, and list/revoke only their own personal tokens.
Creating a token is an explicit POST with CSRF/origin checks; opening the page
does not issue credentials. Kasm's session-managed connection remains separate.

Personal tokens fix the OIDC subject and email store at issuance. These are
immutable authorization fields, separate from editable labels or owner metadata.
Every token permits only recall/read/save for that one store. Neither request
fields nor admin metadata edits can change its identity. Admins may revoke or
rotate personal tokens; rotation retains the binding. Personal tokens cannot
enter either browser console or access token administration APIs.

Users choose 7, 30 or 90 days (default 30), with at most ten active personal tokens
per subject. Expiry and revocation are checked on authentication and again at
operation dispatch. Retiring a token does not remove its memories.

## Current entitlement and schema

The existing trusted identity feed now includes the actual memd-users OIDC `sub`
as `subject` in each active directory entry. The sync uses Authentik's read-only
provider user-preview API with its existing credential; no grant is created.
The directory API's `uid` is a different hashed identifier and must not be used
for this UUID-mode provider. The feed must match the user's OIDC `sub` as well as
email. Personal authentication fails closed if either changes, membership is
removed, the file is unreadable, or its generation timestamp is older than 15
minutes (or over one minute in the future). The normal sync interval is five
minutes. The fallback Kasm UUID/email map alone cannot authorize personal tokens.
No new privileged directory credential is held by memd.

Control schema **2** adds nullable `personal_subject` and `personal_store` columns.
The entrypoint performs the additive schema-1 upgrade transactionally before
serving requests; existing credentials, scopes, expiries and revocations remain
unchanged. Take a consistent control backup before rollout. Older images do not
implement personal entitlement and must not be used for recovery with these
credentials; use a schema-2-aware image and preserve the registry. Backups retain
identity bindings and omit browser sessions.

## Client instructions

The page provides Bash and PowerShell 7 instructions, with a hidden token prompt
that sets `MEMD_TOKEN` only for that terminal. Tokens are never interpolated into
copyable setup commands/configuration, URLs, browser storage or setup downloads.
Users should retain the token in their password manager and supply it again for
a fresh terminal. A GUI/IDE process must receive the environment too.

- Claude Code: user-scoped `claude mcp add-json`, Streamable HTTP and a literal
  environment-variable placeholder in the authorization header.
- Codex: native HTTP `codex mcp add … --bearer-token-env-var MEMD_TOKEN`.
- OMP: merge the HTTP entry into the active profile's user `mcp.json`. A Python
  stdio bridge is provided for older clients without header expansion.
- Pi: download/inspect `pi-memd.ts` and load with `pi -e`. It uses REST automatic
  recall and the remember tool. An unset MEMD_PROFILE now sends no legacy override;
  an explicitly configured profile still supports older single-store setups.
- Other clients: MCP URL, bearer header, REST base/routes and stdio bridge details.

Setup uses the name `memd-personal` and never removes existing configurations.
Users with an already-managed memd connection are told not to add a duplicate.
Normal client tool approvals remain. Client instructions were checked against
the sources below on 15 September 2026; they describe configuration and do not
claim a full interactive session was run in every third-party client.

Sources: [Claude Code MCP](https://code.claude.com/docs/en/mcp),
[official Codex MCP](https://developers.openai.com/codex/mcp/),
[OMP MCP](https://github.com/can1357/oh-my-pi/blob/main/docs/mcp-config.md),
[Pi extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md).

Validation covers signed browser issuance, cross-user REST/MCP controls,
revoke/expiry/limits, no secret redisplay, directory removal/staleness/reassigned
identity, admin rotation preserving bindings, schema migration and backup restore.
The HTTPS browser acceptance also covers all client/terminal guides, connection
testing, safe label rendering, navigation and mobile width. Pi's runtime test
exercises both recall and save without a legacy profile override.
