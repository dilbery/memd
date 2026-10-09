# Portable pilot deployment

The Compose deployment runs one `teama` instance as UID 10001 with a dedicated
named data volume. It binds the API only to the host loopback address.

## Bootstrap

On the deployment host, the administrator creates `secrets/memd-tokens` using
this format (one label and random token per line):

```text
pilot REPLACE_WITH_A_NEW_RANDOM_TOKEN_AT_LEAST_32_CHARACTERS
```

Generate the value with a password/secret manager. Never use the literal example.
For local file-backed Compose secrets, the container user must be able to read the
mounted file: a host `secrets/` directory mode 0700 and token file mode 0444 is one
pilot option. The directory prevents other host users from traversing to the file;
the file is mounted read-only into the container. Use your platform's managed secret
facility in production. Do not place secret values in tracked config or image layers.

The server rereads its token file for each request. File-backed bind mounts can
retain an old inode after atomic replacement: recreate the container to guarantee
rotation, then test that the old token fails and the new token succeeds.

Run `docker compose up --build -d`. Set `GIT_SHA` from the source commit if you want
`/health.app_commit` to identify the build. `MEMD_BIND_PORT` changes the loopback
port (default 8077). The entrypoint refuses empty token sets, short tokens, unlocked
profiles and nonempty directories that are not Git repositories.

The named volume contains `/data/clone` (the authoritative notes and Git history)
and `/data/memd.db` (rebuildable search data). Initial startup creates an empty Git
commit; no notes are pre-loaded.

## Model backends

Set `MEMD_EMBED_URL` and `MEMD_EMBED_MODEL` for your 768-dimensional embedding
service. The current adapter posts to `/v1/embeddings`; inspect `memd/embed.py`
for the request/response contract. Switching models requires vector rebuilding,
even when dimensions match. Add an authenticated adapter if your gateway requires
credentials; memd does not inject an API key into model requests.

The optional reranker is configured with `MEMD_RERANK_URL` and
`MEMD_RERANK_MODEL` and follows the adapter in `memd/rerank.py`.
Defaults point to an unused container loopback port: keyword recall still works,
but `/health.status` reports `degraded`. The Compose health check accepts a usable
degraded service; monitor `checks.embed`, `checks.rerank`, and pending vectors
separately if semantic retrieval is required.

## Git synchronization and backup

Provision a **separate empty private data repository** for this team's notes,
configure the data clone's `origin`, Git identity and tracking branch, and mount
the required read/write credential and verified SSH known_hosts. The source code
repository must never be the data remote. Test an authenticated push, pull, save
receipt and restore before relying on upstream sync.

Without a remote, the service can commit and retrieve locally. `saved: true` plus
`synced: false` is expected in the standalone pilot and is not an off-host backup.
Stop/quiesce writes or use a consistent filesystem snapshot when backing up the
whole data volume; SQLite's backup API is another option for the index. Test a
restore into a new isolated instance. Do not scale independent writers against
the same clone or configure multiple clones as if they were a replicated database.

## Network and identity boundary

Put TLS and your organisation's authentication/authorization in front before remote access.
Route authorized identities to isolated locked instances. A profile or host field
inside the request cannot grant access. `/stats` takes a bearer token. `/health` answers
without one, but only with `ok`, `status`, `app_commit` and each check's pass/fail; the note
count, Git HEAD, embedding dimension, timings and error detail need a token. Keep it internal
or protect it in the gateway anyway. Remote callers without a token are refused unless
`MEMD_ALLOW_UNAUTHENTICATED=1` opts into the old open-read mode.

Do not assume the loopback-only pilot Compose file already implements Kubernetes,
SSO, team roles, high availability, a complete audit trail or production retention.
