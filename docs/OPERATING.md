# memd — operating and deployment guide

This runbook describes an example two-store self-hosted deployment. Host names,
addresses, ports and paths below are placeholders: substitute your own. The
systemd units describe an earlier layout; verify operational claims against the
running containers and health JSON.

## Administration

The web account, token and store settings layer is documented in
[ADMINISTRATION.md](ADMINISTRATION.md). Deployments can enable it with a persistent
`MEMD_ADMIN_DB`. Existing profile directories and agent tokens are retained.

## Example layout

| Component | Location |
|---|---|
| Amber memory service | apphost, `10.10.1.10:8077`, container `memd` |
| Cobalt memory service | apphost, `10.10.1.10:8078`, container `memd-cobalt` |
| Embeddings | apphost CPU embedding service, port 8079 |
| Optional reranker | gpuhost Lemonade; configured with `MEMD_RERANK_URL` |
| Deployment directory | `~/docker/memd` on apphost; source checkout at `src/` |
| Client MCP endpoint | `https://memd.example.com` or `/mcp/` |

Each profile has its own Git clone, SQLite database and token set. The deployed
instances enforce their profile and require tokens for reads and writes. Client
machines need the HTTP integrations or stdio bridge, not local memory databases.

## Health and deployment

`GET /health` exposes `app_commit` for the application version and `checks` for
index, Git and model backends. Without a token each check is reduced to `ok` (and
`in_sync` for Git); send `Authorization: Bearer <token>` for the counts, heads,
timings and error detail. A successful HTTP status alone does not prove a
working service. In particular, require `checks.git.in_sync: true`: a readable Git
clone with a stale lexical index is not current memory.

`maint/memd-doctor` checks the remote instances, credentials, tool schemas and client
surfaces. Its full audit includes writing/updating a dedicated probe note; use
individual read-only health probes when a write is inappropriate. `mem doctor` is
a separate local CLI diagnostic for local configuration and embedding identity.

The canonical deployment script is [deploy/apphost/deploy.sh](../deploy/apphost/deploy.sh).
Install it as `~/docker/memd/deploy.sh` beside the existing Compose file and run it
there as the Docker-enabled service owner. The host needs Docker Compose, Git,
curl, Python 3 and `flock`. The script holds a deployment lock so overlapping runs
cannot replace one another's rollback images. It preserves the source clone's
`core.sshCommand` or inherited `GIT_SSH_COMMAND`; configure the clone's explicit
remote URL and deploy identity before invoking it. The script:

1. Captures each running container's image ID and tags it for rollback **before**
   a build can change `memd:local`.
2. Pulls source with `--ff-only`, builds with the Git SHA, and starts both instances.
3. Parses health JSON and requires the expected `app_commit`, a readable index and
   Git clone, and a current lexical index on both ports.
4. Allows optional embedding/reranker degradation while printing it explicitly.
5. Returns failure and restores the previous images if startup or verification
   fails. Both prior instances must exist for automatic rollback; their tags are
   retained if rollback needs operator attention.

No successful deployment message is printed merely because curl returned 200.
Application SHA and notes Git revision identify different repositories.

## Monitoring

`GET /metrics` exposes Prometheus metrics (README, "Metrics (Prometheus)"). Scrape
it with a dedicated registry token whose only operation is `stats` and whose stores
are the ones the dashboard should show; the same token reads `/stats`, but it is
refused by `/insights`, `/entities`, recall and read. A token without `stats` gets
403. Keep the token in a file readable only by the collector
(`credentials_file` in the scrape config), never in the scrape URL.

`MEMD_METRICS_PUBLIC=1` additionally lets a collector on the same host scrape
without a token. Only a loopback peer with no `X-Forwarded-For`, `Forwarded` or
`X-Real-IP` header qualifies, so leave it off when the reverse proxy and memd share
a network namespace in a way that makes proxied requests look local without those
headers. A public scrape reports only the serving store.

Suggested alerts:

| Condition | Meaning |
|---|---|
| `memd_index_in_sync == 0` for 15m | The lexical index is behind Git; see Recovery below. |
| `memd_index_pending_vectors > 0` for 30m | Vector refresh is not catching up (embedding service down or slow). |
| `rate(memd_backend_errors_total[10m]) > 0` | A model backend is failing; `kind` says how. |
| `rate(memd_recall_fallbacks_total{reason=~"embed_.*"}[10m]) > 0` | Recall is running keyword-only. |
| `rate(memd_http_requests_total{status="5xx"}[5m]) > 0` | Server errors. |
| `rate(memd_saves_total{outcome!="saved"}[1h]) > 0` | Saves are failing. |
| `memd_inbox_pending > 20` | Review backlog. |

Counters reset when the process restarts; use `rate()`/`increase()`, which handle
resets. `process_start_time_seconds` and `memd_build_info{app_commit}` show restarts
and deployments.

## Configuration and secrets

Server configuration comes from the container environment and `Config.from_env()`.
For locally installed commands the implicit `~/.config/memd/env` file is a base
layer; process environment overrides it. An **explicit** `MEMD_ENV_FILE` overrides
inherited process values, so a current secret file wins over a stale shell snapshot.

| Variable | Purpose |
|---|---|
| `MEMD_PROFILE` | Registered profile (`amber` or `cobalt` in the supplied registry). |
| `MEMD_ENFORCE_PROFILE` | Lock an instance to its configured profile. |
| `MEMD_REQUIRE_RECALL_TOKEN` | Require authorization for remote recall. |
| `MEMD_TOKENS_FILE` / `MEMD_TOKEN` | Per-client accepted tokens / legacy token. |
| `MEMD_EMBED_URL`, `MEMD_EMBED_MODEL` | 768-dimensional embedding backend. |
| `MEMD_RERANK_URL`, `MEMD_RERANK_MODEL` | Optional reranker backend. |
| `MEMD_REMOTE` | Remote MCP/recall server for bridge and hook consumers. |
| `MEMD_URL` | Remote API server used by pi. |
| `MEMD_ENV_FILE` | Explicit credential/configuration file. |
| `MEMD_CORE_LIMIT` | Default maximum rendered core entries (8 when unset). |
| `MEMD_ACTIVITY_HOOK` | `1` makes `onboard.sh` install the Claude Code activity hook (like `--activity-hook`); `0` turns an installed hook off. |
| `MEMD_ACTIVITY_DEADLINE_MS` | Activity hook recall deadline (1500 by default, 100-10000). |
| `MEMD_ACTIVITY_TOP_N` | Maximum notes the activity hook injects per tool call (3 by default, up to 8). |
| `MEMD_ACTIVITY_MAX_CHARS` | Activity hook injection cap (2000 by default, 400-6000). |
| `MEMD_ACTIVITY_MAX_RECALLS` | Recalls the activity hook makes per Claude Code session (40 by default, 1-1000). |
| `MEMD_ACTIVITY_ERRORS` | `0` stops the activity hook recalling on failed `Bash` calls ("memd: seen this error before?"); on by default when the hook is installed. |
| `MEMD_HANDOFF` | `1` makes `onboard.sh` install the Claude Code session handoff hook (like `--handoff`); `0` turns an installed hook off. |
| `MEMD_HANDOFF_DEADLINE_MS` | Handoff hook deadline at session end, including any model summary (8000 by default, 500-60000). |
| `MEMD_HANDOFF_START_DEADLINE_MS` | Handoff hook deadline for fetching the latest handoff at session start (2000 by default, 200-10000). |
| `MEMD_HANDOFF_MAX_AGE_DAYS` | Oldest handoff injected at session start, in days (14 by default, 1-365). |
| `MEMD_LLM_URL`, `MEMD_LLM_MODEL` | Optional chat model for `mem-summarize` proposals; off when unset, never used by recall. |
| `MEMD_METRICS_PUBLIC` | `1` serves `/metrics` without a token to loopback callers without forwarding headers (off by default). |
| `MEMD_BACKGROUND_REFRESH` | Background refresh enabled by default; disable only for controlled local/test use. |

The bridge can read either `MEMD_TOKEN=value` or `export MEMD_TOKEN=value`. Its
`--selftest` validates the complete authenticated JSON-RPC tool-discovery response,
including required tools. A JSON-RPC error inside HTTP 200 fails the check. Tokens
are not printed by that diagnostic.

The session handoff hook (`onboard.sh --handoff`) files each handoff through
`POST /propose` and reads the newest one back through `GET /handoff?repo=`, both
with the client token and write access to its store. Pending handoffs are returned
only to the credential that proposed them and never from another store; review or
reject them in the inbox like any candidate (`mem-inbox list` shows the `handoff`
channel).

Onboarding writes a private `~/.config/memd/client.env`; managed clients may instead
point to their existing secrets-manager file. Do not copy credentials into source or
logs. A 401 can mean rotation or revocation: the bridge reloads its file and retries
once when the token changed. Restart clients to discover new tools after an update.

## Reading and saving reliably

Use `recall` for focused context, then `read(slug, offset?, limit?, revision?)` for
full details or text beyond an excerpt. Read pages default to 8,000 body characters
and allow up to 32,000; continuation arguments include the note revision. Pass that
revision back to avoid combining different revisions across pages. A changed note
returns a conflict; restart from offset 0.
MCP recall and HTTP `format: "context"` share canonical rendering and a bounded
core index; the latter avoids transferring every complete core note to a client.
`k` defaults to 8 and caps at 50 query matches; `max_chars` defaults to 14,000.
`core_limit` defaults to 8 or `MEMD_CORE_LIMIT`. Core entries consume remaining
character space after query excerpts, and omissions are explicit. Pinned notes
rank first among core candidates; importance >=4 also qualifies for the core.
Query matches retain `matched` provenance regardless of importance.

A save receipt reports local persistence (`saved`), the note's blob identity
(`revision`), keyword visibility (`lexical_indexed`), vector-index completeness
(`indexed`) and remote durability (`synced`)
separately. Pending embedding or remote sync does not undo a committed note. Read
its warnings before assuming every stage finished. On an ambiguous transport
failure, inspect recall/read before replaying a save; the bridge does not retry
that write automatically.
For an update based on a prior read, supply `expected_revision` so a concurrent
edit is rejected instead of overwritten. Use explicit `supersedes` for replacing
another note; related-note suggestions do not retire it automatically.

## Recovery

**Memory is found, but a detail is missing.** Check excerpt/continuation metadata
and read the note by slug. Rephrasing the same query is unnecessary.

**Recall seems less relevant.** Inspect model health and vector refresh state.
Keyword search can remain healthy during an embedding or reranker outage. Check
`MEMD_EMBED_URL` and the configured model from the service's environment, rather
than assuming the client's shell matches the server.

**Git is readable but `in_sync` is false.** The lexical index is behind the clone.
Recall can still serve the previous snapshot while a background refresh runs.
Inspect the service's refresh logs and invoke authenticated `/reindex` if repair is
needed. The index is derived, but never remove a SQLite database or its WAL files
while the service is running. Preserve the authoritative Git clone and repair/rebuild
the index with the service stopped if necessary.

**Embeddings changed.** Rebuild vectors when changing model identity; two
768-dimensional models need not share a vector space. The canary string in
`memd/embed.py` is a fingerprint input, so changing its wording also changes its
identity. Do not rename it merely because its old model name looks stale.

**Onboarding reports incomplete.** It exits nonzero after reporting failed steps.
Correct the named download/CLI/configuration problem and rerun. Codex configuration
uses its own CLI and keeps a backup; both Codex and Claude MCP registration restore
previous configuration when registration fails. A detected Codex config without its CLI is an explicit setup
failure, not a pretend completed integration.

## Backups

The Git remote is one copy of the notes, but not a backup: a force-push or a lost
host takes history with it, and the review inbox, usage log and administration
and token databases exist only on the server's disk. `mem-backup` (README,
"Backups and restore drills") writes all of them into one encrypted bundle.

**Set up.** Choose `MEMD_BACKUP_DIR` on a different disk from the stores, create
the key with `mem-backup keygen` (`MEMD_BACKUP_KEY_FILE`, mode 0600; a looser
mode is refused, as for store keys) and add `backup,drill` to
`MEMD_NIGHTLY_STEPS` (for example `facts,summarize,backup,drill,health`).
`MEMD_BACKUP_KEEP` (default 14) is how many bundles stay in the directory; older
ones are deleted after each successful create. Run `mem-backup create` and
`mem-backup drill` once by hand and read their output before relying on the timer.

| Variable | Purpose |
|---|---|
| `MEMD_BACKUP_DIR` | Directory bundles are written to (required). |
| `MEMD_BACKUP_KEY_FILE` | Backup key file, 32 random bytes, mode 0600 (required). |
| `MEMD_BACKUP_KEEP` | Bundles kept after each create (14 by default). |

**Off-site copy.** A bundle next to the data it protects dies with that disk.
Copy `MEMD_BACKUP_DIR` to another machine or object store after each run, for
example with `rsync -a --ignore-existing` or `rclone copy` from a separate timer.
Bundles are encrypted and authenticated, so the destination only needs to keep
them, not to be trusted with their contents; keep retention there at least as
long as locally. Run `mem-backup verify <file>` on a copied bundle now and then
to prove the copy itself is intact.

**Key custody.** Keys are never written into a bundle; the manifest records only
where each store key is configured and its key id. Keep the backup key and every
store key (`MEMD_<PROFILE>_KEY_FILE`, an administered store's `key_file`) outside
the backup directory and its off-site copy: for example in the password manager
or on offline media held by two administrators. A bundle without the backup key
is unreadable, and an encrypted store restored without its key is ciphertext.
Losing a key is losing that data; keeping it with the bundles gives both away
together. Rotating the backup key needs a fresh `keygen` to a new path; keep the
old key while bundles made with it are retained.

**Drills.** The nightly `drill` step restores the newest bundle into a temporary
directory, clones each store, checks HEADs, refs and note counts against the
manifest, integrity-checks every database, rebuilds a lexical index for one
store and runs a sample recall, then removes the directory. A failed drill fails
the nightly run and names the check: treat it like a failed backup. A drill
proves the bundle is sound; it does not check that the off-site copy exists.

**Restore procedure.** Never restore over live data; `restore` refuses a
non-empty target.

1. Stop memd (the service or container) so nothing writes to the stores.
2. `mem-backup verify <bundle>`, then `mem-backup restore <bundle> --to <new dir>`.
   Every member hash is checked; a failure removes what was written.
   `<new dir>/manifest.json` lists stores, HEADs and key-file references.
3. For each store, `git clone <new dir>/stores/<name>/repo.bundle <clone path>`,
   then recreate its review branches from `origin/*` if needed and set `origin`
   back to the store's real remote (`git remote set-url origin <repo url>`).
4. Copy `stores/<name>/inbox.db` and `usage.db` to `memd.inbox.db` and
   `memd.usage.db` next to the store's index path, mode 0600.
5. Copy `control/admin.db` and `control/registry.db` to `MEMD_ADMIN_DB` and
   `MEMD_CONTROL_DB`, mode 0600. Browser sessions were removed, so everyone
   signs in again; tokens and grants are as they were at backup time.
6. Put each encrypted store's key file back where the manifest's `key_file`
   says (or update the setting), from its separate custody copy.
7. Start memd; it rebuilds the index from Git. Check `/health` for
   `checks.git.in_sync: true` and a recall, as after any deployment.

## Contributor verification

Run `.venv/bin/python -m pytest -q` from the checkout. Tests must use temporary Git
clones, private temporary client configurations and mocked model/network endpoints.
`tests/conftest.py` strips all inherited `MEMD_*` variables, redirects the real env
file default, and installs the network socket guard. Subprocess tests must provide
their own fake commands or network guard too; the parent process's monkeypatches
do not protect child processes.

The client/deployment regression tests exercise JSON-RPC failures inside HTTP 200,
SSE conversion, ambiguous save retries, stale Git health, rollback image provenance,
wrong deployed SHAs, failed onboarding and pi save receipts. They invoke no live
Docker daemon or production service.
