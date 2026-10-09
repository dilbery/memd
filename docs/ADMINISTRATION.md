# Administration, stores and Obsidian

## Start a separate installation

From this repository, run `docker compose up -d --build`, then create the initial
administrator with `docker compose exec memd python -m memd.control bootstrap admin`.
The command prompts for a password without placing it in shell history. Open
`http://localhost:8077` and sign in. For unattended setup, mount a private password
file and set `MEMD_ADMIN_PASSWORD_FILE` and optionally `MEMD_ADMIN_USERNAME`.
The password file is used only when no administrator exists.

This deployment starts with an empty local Git-backed store and no Forgejo
dependency. Keyword retrieval works without model services. Semantic retrieval
requires a compatible 768-dimensional embedding endpoint; reranking is optional.
Expose remote installations through an HTTPS reverse proxy. The supplied Compose
file binds only to localhost. Run the container as UID 1000 with access to its data
volume and any mounted vaults.

## Accounts and administration

The Administration tab requires an administrator browser account. Agent tokens
cannot administer users, credentials or connections. Browser sessions expire
after 12 hours; cookies are HttpOnly and SameSite=Strict, and Secure over HTTPS.
Mutations require a session-bound CSRF header. Passwords use scrypt; new agent
tokens are stored as SHA-256 hashes, and their plaintext is displayed only once.

Create users, share their generated temporary password privately, and assign
read-only or read/write access to individual stores. New members have no access
until granted. Administrators can access every store. Users can change their
password from Settings; password changes and resets invalidate browser sessions.
Disabling a user immediately blocks their sessions and agent tokens.

Issue tokens for a named user and store, with read or read/write access, an
optional expiry and optional additional stores (see [Team stores and
sharing](#team-stores-and-sharing)). Token permissions are intersected with
the user's current store
permissions on each request, including MCP tools. Removing a grant also removes
the token's access. The token table shows masked identifiers, names, owners,
scope, last-use time and revocation/expiry status. Revocation applies immediately
to new requests; it does not cancel an operation already in progress.

Existing file/environment tokens continue to work for their original serving
profile. They have no administrative rights. They appear in the table as legacy
tokens and can be revoked through the same screen. A revocation tombstone is
checked before the legacy credential source. CLI-issued/revoked file tokens are
reconciled when the admin list refreshes. Keep the administration database in
backups: restoring an old copy also restores its earlier revocation state.

Administrative actions are recorded in the activity list. Raw credentials and
note bodies are not logged there. Local operator password recovery is available
with `python -m memd.control reset-password USERNAME` in the configured container.

## Memory stores

Settings supports local Markdown, Git repositories (GitHub, Forgejo, GitLab or
another SSH/HTTPS Git host), and mounted Obsidian vaults. New stores receive their
own generated clone and SQLite index paths. User-supplied paths cannot override
these. Store IDs are stable, lowercase identifiers and are also used by agent
tokens. Existing store types cannot be changed in place.

Configure repository URL, branch, optional HTTPS token or SSH key, folder filters,
sync interval and model endpoints. Credentials live in private server files,
outside the notes repository, and are not sent back to the browser. SSH keys use
persisted known hosts with trust on first connection. Connection tests and sync
operations run as bounded background jobs and report completion or failure in
Settings. Automatic sync can be disabled with interval 0; otherwise the minimum
interval is 60 seconds. Conflicted/divergent Git histories fail with an error and
retain the local files. Settings never force-pushes or resets a repository.

Use a new store to change the embedding model of an existing index. Embeddings
from different models are not interchangeable, even if their dimensions match.
Reindex refreshes keyword data and schedules vector processing. Export downloads
the current notes as a Markdown ZIP without credentials, databases or Git internals.

## Team stores and sharing

A team store is an ordinary store that several users are granted. Members
publish notes into it from their own store and recall across both (see the
README section on team sharing).

1. Create the store in Settings (local or Git), for example with ID `team`.
2. Grant each member **read/write** on `team` to publish into it, or
   **read-only** to recall and read it only. Leave each member's grant on their
   own store as it is; nobody gains access to another member's personal store.
3. Decide review. **Published notes need review** (Settings → the store's
   **Configure**; `publish_review` in its configuration) is on by default: a
   published note waits in the team store's review inbox until a member with
   write access approves it. Clear the box and save to have published notes
   saved directly; `PUT /admin/stores/team` with `{"config":
   {"publish_review": false}}` does the same. A store configured by
   environment uses `MEMD_<STORE>_PUBLISH_REVIEW=false`; for a registered store
   without the setting (such as the existing serving store) Settings shows that
   environment value and keeps using it until you change the box.
4. Issue agent tokens that reach both stores. A token has one main store and
   optional additional stores, each read or write. In Administration →
   **Issue token**, choose the owner, the main store and its access, then set
   each store under **Additional stores** to read only or read and write. Only
   stores the owner is granted are listed, with write only where the owner has
   write. Through the API:

   ```sh
   curl -X POST https://memory.example.com/admin/tokens -b session.cookies \
     -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
     -d '{"label":"laptop","user_id":"<id>","store_id":"amber","scope":"write",
          "extra_stores":{"team":"write"}}'
   ```

   The main store stays the default for every call; the token reaches `team`
   only when a call names it (`publish(..., target_store="team")`,
   `recall(stores=["amber","team"])`, `read(slug, store="team")`). Additional
   stores are limited to the user's grants when issued and are intersected with
   the user's current grants on every request, exactly like the main store:
   removing the user's `team` grant removes the token's reach there at once. The
   token table lists them after the main store (`amber + team (write)`).
   Revocation and expiry work as for any token. Additional stores cannot be
   changed after issue: issue a new token and revoke the old one.

Publishing needs read on the source and write on the target; federated recall
needs read on every store it names, and `scope: "all"` covers only granted
stores. An encrypted store's notes are published into an unencrypted store only
with an explicit `allow_decrypted_publish`. Run `mem-share status --store team`
in the container to list published notes whose source changed since (`behind`);
a member who can also read the source sees the same in `read` and `recall`
(`upstream`).

## Encrypted stores

A local or Git store can be encrypted so its repository holds only ciphertext
and may live on an untrusted Git host. Create a key on the server with
`mem-crypt keygen /data/keys/<store>.key` (32 random bytes, written mode 0600;
memd refuses a group- or world-readable key), then set the store's
**Encryption key file** in Settings (`key_file` in the store configuration).
The path must be absolute and readable by the service user only; the key is
never sent to the browser, logged or stored in the notes repository.

A new store created with a key file starts encrypted: its first commit adds the
`.memd-encrypted` marker. For a store that already holds plaintext notes, run
`MEMD_PROFILE=<store> mem-crypt encrypt --dry-run`, then without `--dry-run`, in
the container. Until then the store is refused with a health error rather than
written in plaintext. The migration converts every note in one commit and
removes mem-carve output (`MEMORY.md`, `MEMORY-full.md`), which lists note
titles in plaintext. Envelopes are written at the clone root; the store's
include/exclude filters apply to plaintext files only. A killed migration is recorded only in the
clone's `.git` directory and leaves the store refused until the same command is
run again. `mem-crypt decrypt` converts it back; remove the key setting
afterwards. Encrypting does not rewrite earlier commits, which still hold the
plaintext notes: publish the encrypted store to a new repository with fresh
history (see the README section on encrypted stores for the steps and the full
threat model).

**Back up the key separately from the store** (for example in a password
manager or an offline copy). Losing it loses every note in the store; neither
the Git host nor memd can recover it. Keep it out of the notes repository and
out of backups that travel with the repository. A missing, wrong or too
permissive key, or plaintext notes or mem-carve output beside the encrypted
notes, makes the store unavailable and `/health` reports `checks.encryption` as
failed; nothing is read or written until it is fixed. `/health` is
unauthenticated and does not give the reason: find it in the server log or run
`MEMD_PROFILE=<store> mem-crypt status` in the container. Obsidian vault stores
cannot be encrypted, because the vault keeps the notes in plaintext.

The Git host still sees the number of notes, their approximate sizes, which
notes each commit touches, commit times and the marker's key id. Commit messages
in an encrypted store are generic (`memd: update memory`, `memd: proposed
changes for review`) and carry no caller label.

## Obsidian

Mount the vault under `/vaults` (or an explicitly configured `MEMD_VAULT_ROOTS`
directory), then add an Obsidian store using the mounted path. This connects to
the vault's files; it does not log into Obsidian Sync or require an Obsidian plugin.
For a vault on another device, arrange filesystem synchronisation or a mount first.

Choose inclusion/exclusion globs and a write folder such as `Memories`. Existing
notes are mirrored into memd's private Git clone without changing the source
files. Properties and wiki-link text are retained. Hidden folders and symlinks
are excluded. Imports accept Markdown files up to 4 MiB; attachments are not
indexed. Imported notes outside the write folder are read-only to agents. Notes
in the write folder are always included, so newly saved agent memories remain
searchable. Original imported note identities are derived from relative paths;
renaming a source path creates a new imported identity.

Agent memories are committed to memd and exported to the designated vault folder.
If the vault copy has changed since the last sync, export is stopped and both
versions are retained. Settings → Resolve conflicts previews the copies and asks
which to keep. Resolution checks both revisions again and archives both versions
under the clone's `.git/memd-conflicts/` before applying the choice. A file changed
since the preview requires a fresh preview. Sync resumes after conflicts are
resolved. Vault paths and write folders cannot be changed in place; connect a new
store instead. An optional dedicated Git remote can also publish the vault mirror.

## Existing deployments

Administration is opt-in via `MEMD_ADMIN_DB`. Mount its parent directory on a
persistent volume. The configured serving profile is registered without moving
its clone or database, and legacy bearer tokens remain scoped to that profile.
For several instances sharing an admin database, mount the registered store paths
at the same container locations in each instance. The existing profile lock still
applies to legacy tokens; account sessions and new scoped tokens use explicit
store grants. Keep admin data and store files readable only to the service owner.

Passwords are hashed with scrypt at n=2**17, which holds about 128 MB per
hash while it runs. At most `MEMD_SCRYPT_CONCURRENCY` (default 2) run at once;
further sign-ins wait their turn. Older hashes still verify and are upgraded on the
next successful sign-in.

Back up the complete control directory (accounts, token hashes/revocations,
settings, recoverable Git credentials, managed clones and indexes) together with
legacy stores and the original vault, and the key file of every encrypted store,
kept apart from the store's repository. A consistent SQLite backup or a stopped
service is required when copying live databases. Git repositories alone do not
contain user accounts or access configuration.
