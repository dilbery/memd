# memd — shared durable memory for agents

memd is a self-hosted memory server for AI coding agents and assistants. It is for
individuals and small teams who run agents on several machines and want them to share
one memory they own: facts, decisions and how-tos that stay readable, correctable and
under version control instead of locked inside one tool.

memd stores durable notes as Git-backed Markdown and retrieves useful context through
MCP or HTTP. The Git repository is authoritative; SQLite, FTS5 and sqlite-vec provide
a rebuildable search index. Notes remain readable, diffable and reviewable.

Clients connect remotely; they do not need their own memory clone or embedding
service. Separate profiles use isolated stores and credentials. An optional
reranker improves result ordering; keyword retrieval works without model services.

See [OPERATING.md](docs/OPERATING.md) for deployment, configuration and recovery.

## Install

Run `docker compose up -d --build`, then create your administrator account with
`docker compose exec memd python -m memd.control bootstrap admin` (it prompts for the
password; `reset-password USERNAME` recovers an account). Open `http://localhost:8077`.
The default store is local Markdown with Git history; no remote Git service is
required. The Compose file publishes the port on `127.0.0.1` only, so put an HTTPS
reverse proxy in front of it before other machines connect.

Model services are optional. Point `MEMD_EMBED_URL` (and `MEMD_RERANK_URL`) at
compatible endpoints in `compose.yaml` or per store in Settings to turn on semantic
search; until then recall is keyword-only. For unattended setup, set
`MEMD_ADMIN_PASSWORD_FILE` (and optionally `MEMD_ADMIN_USERNAME`) instead of running
the bootstrap command.

See [Administration and Obsidian](docs/ADMINISTRATION.md) for accounts, access tokens,
Git connections, vault mounts, model services, and migration of existing installs.
A two-instance deployment with image rollback and health verification
(`deploy/apphost/deploy.sh`) is described in [OPERATING.md](docs/OPERATING.md).

## Web dashboard

Open the server URL in a browser. **Memory index** shows live service health,
index coverage, host and importance breakdowns, paginated notes (filterable by tag)
and semantic search. Unlock note access with an existing token; it stays only in the
current tab's memory. Reloading or locking clears it. Full notes open with
revision-guarded pagination. Notes are rendered as text, never executable HTML.

**Memory health** shows where the store is rotting or wasted: notes no recall
returned in the usage window, notes shown again and again but never read, the most
read notes, stale changeable-state notes, failed verifications, contradicting
current facts, supersede candidates, stale summaries, pending review and index
coverage, plus an age-by-tag (or host) heatmap with its counts written in each
cell. Titles open the note reader. It uses the same token or session as the
memory index; see [Memory health](#memory-health) for what each figure means.

**Entities** is one page per host, service or tag ("everything about vmhost"),
from explainable rules: a note's `host` field and the one-word object of a
`runs on` fact are hosts, other [fact](#timeline-facts-optional) subjects are
services, and a single-word tag on at least two notes that is not a generic topic
word (`handoff`, `todo`, `ops`, ...) is a tag entity. A searchable list shows each
entity's notes, current facts, stale notes, failed verifications and last
activity; its page shows current facts with their source note and since date, the
fact timeline, the notes scoped to it, tagged with it, stating facts about it or
mentioning it (whole tokens, from the keyword index) with stale and verification
labels, related entities (shared notes or linked by a fact), conflicting current
facts, and pending inbox candidates that mention it (a count only, unless the
caller may review the inbox). Note titles open the note reader. `GET /entities`
and `GET /entities/{kind}/{name}` (`kind` is `host`, `service` or `tag`) are
read-only, authorized like `/insights` (a read, never a stats-only token), cached
per store for 30 seconds (`?fresh=true` bypasses it), and answer 404 for a name
that is not an entity of that kind in the store. The list keeps the 150 strongest
entities (notes scoped, tagged or stating facts, plus current facts), and at most
40 tag entities, so the work per request stays bounded; an entity past the cap
still has its page.

**Onboarding** provides portable setup instructions, copyable commands and client
downloads. Commands use the current server address and serving profile.
`/help` retains a plain text reference. Both root-path MCP and `/mcp/` remain
available. The dashboard has no external assets or build dependencies and supports
small screens and system light/dark mode. **Settings** connects local, Git and
Obsidian stores and sets whether notes published into a store need review.
**Administration** manages users, store grants, scoped expiring tokens (with
optional additional stores for [team sharing](#team-sharing-optional)),
revocation and audit history. The audit trail records administrative actions and,
as actor, action and target only (never query text), recalls, reads and exports;
recall and read entries expire after `MEMD_AUDIT_READ_DAYS` (90). Browser accounts
use password login and HttpOnly sessions; agent tokens cannot administer the server.

## Connect a client

Issue a client token on the server, one per machine: **Administration → Access
tokens → Issue token** in the dashboard, `deploy/memd-token issue <label>` for a
legacy token file (`MEMD_TOKENS_FILE`), or `memd-control issue` once the token
registry is on. Then on the client:

```sh
curl -fsS https://memory.example.com/clients/onboard.sh -o onboard.sh
bash onboard.sh [server-url]          # prompts for the token
```

The dashboard's **Onboarding** page prints the same steps with this server's address
filled in. Prefer the prompt, `--token-file PATH` or an already exported
`MEMD_TOKEN` to a token argument: an argument stays in shell history and shows in
the process list, so `onboard.sh` warns about it. It also refuses to send the token
over plain `http://` to anything but this machine unless `MEMD_ALLOW_INSECURE_HTTP=1`. The store profile is read from the token
(`mem_<profile>_...`, as the dashboard issues them).

Onboarding validates authenticated MCP tool discovery before changing client files.
It configures detected Claude Code, Claude Desktop (Linux config path) and Codex
installations, and installs the pi extension. Codex configuration uses `codex mcp
add`; both Codex and Claude MCP registration preserve backups and restore them on
failure. Unrelated settings and MCP servers are preserved. The
bridge reads its credential from a private env file instead of embedding it in
Codex configuration. For pi, source the printed Bash/Zsh env file before launch.
A failed setup step produces a nonzero exit and an incomplete summary.

A machine that holds the store itself needs no onboarding: `mem-mcp` is the stdio
MCP server over the local store, and `python -m memd.hooks.auto_recall` is the matching
prompt hook (it recalls in process, or from `MEMD_REMOTE` when that is set, and falls
back to a keyword grep over a separate read-only checkout, `MEMD_FALLBACK_CHECKOUT`,
when memd is unreachable). The Hermes Agent memory provider and the opencode sync
sidecar are in `memd/integrations/`; see
[integration-registration.md](docs/integration-registration.md).

### Claude Code hooks

Onboarding installs `memd-recall-hook` as a `UserPromptSubmit` hook: each prompt's
text is recalled and relevant notes are added as context (`MEMD_TOP_N` matches, 8 by
default; `MEMD_MAX_CHARS`, 14,000; `MEMD_TIMEOUT`, 4 seconds). Any failure adds
nothing and never blocks the prompt. An optional second hook
recalls on what the agent is *doing*. Install it with
`bash onboard.sh --activity-hook [server-url]` (or `MEMD_ACTIVITY_HOOK=1`); it
is off otherwise. `memd-activity-hook` runs as a `PostToolUse` hook on `Bash`,
`Edit`, `Write`, `MultiEdit`, `Read` and `NotebookEdit` and extracts the call's
targets:

- `ssh`/`mosh`/`scp`/`sftp`/`rsync` destinations, `docker -H` and `systemctl -H`
  hosts, and `curl`/`wget` URL hosts, mapped through the host aliases and
  `MEMD_HOST_NAMES`;
- `docker`/`docker compose`, `systemctl`, `journalctl -u`, `service` and `kubectl`
  service, container and workload names (also inside `ssh host '...'` and
  `bash -c '...'`);
- a touched file's basename and its repository (the nearest `.git`).

The first time a session touches a target, the hook recalls with `k` of
`MEMD_ACTIVITY_TOP_N` + 2 (5 by default), `include_core: false` and a `host` filter
when a single known note host was named. A note is injected only when it names a
target or is scoped to that host.
The result is a capped excerpt block (2,000 characters by default) with the
`recall_id`. Each target triggers recall and each note is injected at most once per
session. The record of seen targets is kept per session in
`$XDG_CACHE_HOME/memd/hook-state` (default `~/.cache/memd/hook-state`) and pruned
after three days. The recall has a hard deadline of 1,500 ms, and a failed or
timed-out recall pauses further recalls for a minute. Any error exits 0 with no
output, so a tool call is never failed and never waits longer than the deadline.
Tune it with `MEMD_ACTIVITY_DEADLINE_MS`, `MEMD_ACTIVITY_TOP_N` (default 3),
`MEMD_ACTIVITY_MAX_CHARS` (default 2,000, at most 6,000) and
`MEMD_ACTIVITY_MAX_RECALLS` (recalls per session, default 40). `MEMD_ACTIVITY_HOOK=0`,
in the environment or in the client env file, turns off an installed hook.

When a `Bash` call fails, the same hook recalls on the error instead. Onboarding
also registers it as a `PostToolUseFailure` hook on `Bash` (the event Claude Code
sends for a failed call, with the output in `error`); a `PostToolUse` response
with a nonzero exit-code field, or an `Exit code N`/`exit status N` line in
stderr, counts too. Output on stderr alone and an interrupted call do not. The
query is the failing command's name, the one or two most distinctive error lines
(exception, errno and error-code lines first) with paths, URLs, hex ids, IP
addresses, timestamps, line numbers and PIDs removed, and the call's targets,
bounded to 240 characters. The same deadline, cap, backoff and relevance bar
apply: a note must mention the error's exception or errno name, error code, a
known error phrase ("connection refused"), a quoted module or package name, or a
target. When more notes pass than fit, those tagged or titled as troubleshooting,
fix, incident, runbook or postmortem take the places; notes keep recall order.
The block is headed `memd: seen this error before?`. Each normalised error
signature (command name plus error lines) recalls at most once per session and
counts against `MEMD_ACTIVITY_MAX_RECALLS`; a repeat falls back to the ordinary
target recall. `MEMD_ACTIVITY_ERRORS=0` turns off only this part. Onboarding
keeps a backup of the previous `~/.claude/settings.json` and restores it if the
merge fails. `python -m memd.hooks.activity_recall` is the same hook for a
machine with memd installed; it recalls in process when `MEMD_REMOTE` is unset.

A third optional hook keeps **session handoff notes**: where a session left off
in a repository, shown to the next session there. Install it with
`bash onboard.sh --handoff [server-url]` (or `MEMD_HANDOFF=1`); it is off
otherwise. `memd-handoff-hook` runs as a `SessionEnd` hook and as a `SessionStart`
hook for `startup` and `resume` (not `clear` or `compact`):

- **Repository.** The nearest enclosing `.git`, keyed by its normalised remote
  (`origin`, else the first: `git@git.example.com:team/Widget.git` ->
  `git.example.com/team/widget`, without credentials, port or `.git`), or by its
  directory name when it has no remote. Git runs read-only (`GIT_OPTIONAL_LOCKS=0`,
  no prompts) with a two-second timeout per call. Outside a repository nothing
  happens.
- **At session end** the hook reads the transcript's last 2 MB and the Git state
  (branch, last commit subject, `git status --porcelain` counts of staged,
  modified, untracked and conflicted files) and writes a handoff of at most 4,000
  characters. The standalone hook writes it deterministically: the last three
  requests, the last agent update, the files edited in the repository (from
  `Edit`/`Write`/`MultiEdit`/`NotebookEdit` calls) and the Git state.
  `python -m memd.hooks.handoff`, the same hook for a machine with memd
  installed, asks the chat model (`MEMD_LLM_URL`) instead for a summary, what was
  done, what is unfinished and next steps, as strict JSON: only user and
  assistant text is sent, redacted like transcript distillation and bounded to its
  last 12,000 characters; a failed, slow or malformed reply falls back to the
  deterministic handoff. Everything is redacted before it leaves the machine and
  again on the server. A session with no requests, no edits and a clean tree
  files nothing.
- **Storage.** The handoff is proposed like any memory (`POST /propose` with the
  client token, or in process without `MEMD_REMOTE`): a review-inbox candidate on
  the `handoff` channel titled `Handoff: <repo>`, tagged `handoff` and
  `repo:<repo>`, with `source: handoff`, importance 1 and volatility `state`. A new
  handoff deletes the same credential's older pending one for that repository, so
  they do not pile up. Approving it saves an ordinary note; the fixed title keeps
  one note per repository, updated by each later approval.
- **At session start** the hook asks `GET /handoff?repo=<repo>` for the newest
  handoff at most `MEMD_HANDOFF_MAX_AGE_DAYS` (14) old and adds it as context
  under `memd: where the last session left off in <repo>` with its "as of" time
  and whether it is pending review or a saved note. The route takes the same
  authorisation as `/propose` (write access to the store) and answers from the
  caller's own store only, with text only: a pending handoff is returned only to
  the credential that proposed it; an approved one while its note exists.
- **Rails.** Session end has a hard deadline of `MEMD_HANDOFF_DEADLINE_MS` (8,000;
  the model gets what is left after 2.5 s are kept for filing) and session start
  `MEMD_HANDOFF_START_DEADLINE_MS` (2,000). Any error or timeout exits 0 with no
  output, so a session never fails or waits longer than that. `MEMD_HANDOFF=0`, in
  the environment or the client env file, turns an installed hook off.
  Onboarding merges both entries idempotently with the same settings backup and
  restore as the other hooks.

Clients that use a managed secret file can set `MEMD_ENV_FILE` to that file instead.
The bridge rereads a rotated token after a 401. Existing sessions may need a
restart to discover newly added tools. `memd-mcp-bridge --selftest` runs an
authenticated tool-discovery round trip and reports which credential source it used
without printing the token. Codex's configuration mechanism is described in the
[official MCP documentation](https://developers.openai.com/codex/mcp/).

## Use memory

The MCP server (stdio through `mem-mcp`, HTTP at `/mcp/` and the bare server URL)
offers seven tools. None can delete a note.

| Operation | Purpose |
|---|---|
| `recall(query, k?)` | Search for relevant notes and receive compact excerpts plus a bounded core index. |
| `read(slug, offset?, limit?, revision?, store?, recall_id?)` | Read an authoritative note by slug, with revision metadata and continuation for longer bodies. Pass the `recall_id` of the recall the slug came from; see [Learning from usage](#learning-from-usage-optional). |
| `save(title, body, ...)` | Store a durable fact and return a persistence/index/sync receipt. |
| `propose(title, body, ...)` | File a fact for human review instead of saving it; see [Review inbox](#review-inbox-optional). |
| `ask(question, k?, stores?, host?, tags?)` | One short answer drawn only from your notes, with the notes it cites, a confidence and any gaps. See [Cited answers](#cited-answers-ask). |
| `timeline(subject, predicate?, at?)` | Exact lookup of extracted facts: what is true about a subject now, or on a date. See [Timeline facts](#timeline-facts-optional). |
| `publish(slug, target_store, store?)` | Copy one of your notes into a shared (team) store with provenance; see [Team sharing](#team-sharing-optional). |

Canonical argument names are preferred, but recall also accepts
`q`, `question`, `topic`, `search` and `text`. Save accepts text under
`body`, `content`, `text`, `fact` or `note`; a missing title is derived from the
body. `slug` and `name` are accepted aliases for a title, and an explicit slug
identifies the intended note.

Recall excerpts are deliberately compact. When a needed detail is omitted, use
`read` with the returned slug and continuation rather than repeatedly rephrasing
the same search. `k` defaults to 8 query matches and accepts up to 50, subject to
the `max_chars` budget (14,000 by default). `core_limit` separately caps core entries
at 8 by default, or the server's `MEMD_CORE_LIMIT` value. Both sections share the
character budget; omission metadata explains what did not fit. Use `include_core:
false` to omit core entries. Optional `host` and `tags` filters narrow the search.
Notes archived by review ([Forgetting with review](#forgetting-with-review-optional))
are left out unless `include_archived: true` asks for them; they are then marked
`ARCHIVED <date>` and never enter the core index.
Optional `stores` (a list) or `scope: "all"` recalls across several stores you have
access to, each result labelled with its store; `read` then takes the matching
`store` ([Team sharing](#team-sharing-optional)).

Core eligibility is `importance >= 4` or `pinned: true`, with pinned notes shown
first. A high-importance note that matches the query appears as a query excerpt,
not only a core stub. Source, observation/verification dates, grounding, tags and
descriptions survive indexing. Read continuations include a revision guard to
prevent mixing pages if a note changes between calls.

A save receipt separates independent outcomes:

- `saved`: the note was committed locally to the authoritative Git clone.
- `revision`: the note's Git blob identity, usable for guarded reads and updates.
- `lexical_indexed`: current content is searchable through keywords.
- `indexed`: the vector index has caught up at receipt time.
- `synced`: the commit reached the remote Git repository.
- `warnings` and `related`: pending work or related notes worth reviewing.
- `conflicts`: existing notes this save appears to contradict or update, each with
  `slug`, `title`, `kind` (`contradicts` or `updates`), `evidence` (the conflicting
  claims and their dates), `suggested_action` and `method` (`facts` or `llm`). Each
  is also summarised in `warnings`.

A committed note can be saved successfully while vector indexing or remote sync is
pending. A timeout is an **unknown outcome**, so inspect recall/read before retrying
a save. The stdio bridge retries read operations on transient connection failures;
it does not automatically replay saves after an ambiguous network failure.
Use `expected_revision` when updating a previously read note to reject stale writes.
Use `supersedes` to explicitly replace an older fact while retaining its history.

Conflicts are advisory and never supersede anything. The save is committed first;
a failed or slow check adds a "Conflict check skipped" warning instead. The default
check runs the deterministic [timeline fact](#timeline-facts-optional) patterns over
the new body: a fact whose subject and relation match a current fact of another live
note with a different value is reported, as `updates` when the new fact is dated
later and `contradicts` when it is undated or the same day (an older fact is history
and is not reported). Other notes' facts come from the notes naming that subject and
from `mem-facts`' table. With `MEMD_CONFLICT_CHECK=llm` and `MEMD_LLM_URL` set, the
new note and up to three related notes also go to the chat model for a JSON verdict
per note, bounded by `MEMD_CONFLICT_DEADLINE_MS`. The note being written and the note
it supersedes are never reported. To act on a conflict, save again with the new
note's `slug` and `supersedes=<other slug>`: the note is updated in place and the
other is retired.

## HTTP clients

The deployed instances require a bearer token for recall, ask, timeline, read, save,
propose, publish and reindex. MCP is served at `/mcp/` and the bare server URL.
`/health` reports component status and the running application commit; the
per-component detail needs a token. Remote requests without a token are refused
unless `MEMD_ALLOW_UNAUTHENTICATED=1` is set.

The routes mirror the tools: `POST /recall`, `/read`, `/save`, `/propose`, `/ask`,
`/timeline` and `/publish`, plus `POST /reindex` (`{"pull"?, "profile"?}`),
`GET /handoff?repo=` and the review-inbox routes. Read-only reports are `GET /health`,
`/stats`, `/insights`, `/entities`, `/entities/{kind}/{name}` and `/metrics`. `GET /help`
prints a plain-text reference, and `GET /clients/<file>` serves the onboarding script
and client hooks without a token.

```sh
curl -fsS https://memory.example.com/recall \
  -H "Authorization: Bearer $MEMD_TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"gateway DNS","k":5,"format":"context"}'
```

Recall retains the structured `notes` response for existing consumers. Compact
consumers can request `format: "context"` and use the canonical `context` string
and `rendering` metadata. Set `include_core: false` if your client already supplies
a core index. Every recall response also carries `recall_id` (null when the usage
log is off); send it back as `recall_id` on `POST /read`.

## Cited answers (ask)

`ask(question, k?, stores?/scope?, host?, tags?)` is an MCP tool and `POST /ask`,
read-only and authorised exactly like recall, including federated `stores` or
`scope: "all"` ([Team sharing](#team-sharing-optional)). It returns one short
answer instead of excerpts:

```sh
curl -fsS https://memory.example.com/ask \
  -H "Authorization: Bearer $MEMD_TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"When do the nightly archive backups start?"}'
```

```json
{"answer": "Nightly archive backups start at 02:30 on lapbox.",
 "citations": [{"slug": "archive-backup-window", "title": "Archive backup window",
                "as_of": "2026-09-20", "stale": false}],
 "confidence": "high", "gaps": [], "mode": "model", "recall_id": "3f9c0a1b2c3d4e5f"}
```

The evidence is recall's top matches (no core index, `k` 6 by default, at most 12),
current [timeline facts](#timeline-facts-optional) for subjects the question names,
and matching [current-state summaries](#current-state-summaries-optional); a summary
whose sources still match leads, one whose sources changed is marked stale. Each item
carries its slug, store, title, as-of date, staleness and a query-centred excerpt,
within a fixed character budget. With `MEMD_LLM_URL` set the chat model answers from
that evidence alone under one deadline (`MEMD_ASK_DEADLINE_MS`), in a strict JSON
schema; citations of anything outside the evidence are dropped
(`citations_dropped`), and an answer that claims confidence while citing nothing is
rejected. Without a model, or on a timeout, error or unusable reply, the answer is
**extractive**: the one or two sentences of the evidence that best cover the
question, each with its slug, and `mode: "extractive"` with `fallback_reason` says
so. Confidence is `high`, `medium` or `low`; a stale citation caps it at `medium`.

A cited note that is past its freshness window or failed a recent `mem-verify`
check is `stale: true` with a `caveat`, and the MCP text says so explicitly. That
text puts the answer first, then confidence, caveats and gaps, then a compact
**Sources** list. The ask is recorded in the [usage log](#learning-from-usage-optional)
like a recall, with the cited notes as the matches shown; pass its `recall_id` to
`read`. Note text is treated as data: the prompt says so, each note is fenced so
that its text cannot close the fence, and only the answer, known citations, the
confidence word and gap strings are taken from the reply.

## Develop or self-host

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest -q
```

memd needs Python 3.13 (`pyproject.toml` pins `>=3.13,<3.14`) and runs `git` (and
`ssh` for SSH remotes) as subprocesses, so both must be installed. Embeddings use an
OpenAI-compatible `/v1/embeddings` endpoint with **768 dimensions** by default
(`MEMD_EMBED_DIM`). The optional reranker uses the llama.cpp/Lemonade
`/api/v1/reranking` request shape; `MEMD_RERANK_PATH` selects another path, such as
the Cohere-shaped `/v1/rerank`. Changing the embedding model requires rebuilding its
vectors; matching dimensions alone does not imply compatible embeddings.

With administration enabled, profiles/stores are registered dynamically in the
persistent control database. The web interface creates isolated clone/index paths
and assigns access through users and scoped tokens. Legacy environment-configured
profiles remain supported for existing deployments; their bearer tokens stay
bound to their serving instance. See [the administration guide](docs/ADMINISTRATION.md)
for shared-control deployments and vault mounts.

CI runs the test suite, gitleaks and `maint/privacy-scan` on every push to `main` and
every pull request. It also runs the recall eval on a public synthetic corpus
(`eval/public`, with a hashed stand-in embedder and no reranker) and fails when a
change makes recall worse than the committed baseline; see
[eval/README.md](eval/README.md). The privacy scan fails when a file mentions a term
from the `PRIVATE_TERMS` repository secret (names, domains, hostnames: one per line,
or comma-separated; the scan is skipped where the secret is not set, as on forks); the
terms never live in the repository and the log reports only term numbers. Locally,
put the same list in `~/.config/memd/private-terms` and run
`maint/privacy-scan --install-hook` to check each commit before it is made, or
`maint/privacy-scan --history` to audit every commit on every branch.

`mem doctor` checks a locally installed service configuration and embedding canary.
`maint/memd-doctor` audits a remote two-instance deployment and its client surfaces
(read its header for the `MEMD_DOCTOR_*` settings), and `maint/memd-triage` runs it
daily. On a failure a model (the command in `MEMD_MAINT_MODEL_CMD`) picks one fixed
remediation from `maint/remediations` or the run escalates by Pushover; see
[the design note](docs/2026-08-21-memd-maint-design.md).

The package installs these commands (see `[project.scripts]` in `pyproject.toml`):

| Command | Purpose |
|---|---|
| `mem recall`, `mem save`, `mem reindex`, `mem doctor` | Local CLI over the store selected by `MEMD_PROFILE`/`MEMD_CLONE`/`MEMD_DB`. |
| `mem-mcp` | The stdio MCP server over the local store. |
| `mem-carve`, `mem-budget-lint` | Write a byte-budgeted `MEMORY.md` core index (24,985 bytes; `--core` for a tiny one) plus the complete `MEMORY-full.md`, and fail when `MEMORY.md` is at or over budget. |
| `mem-sweep` | One-shot backfill of `host:` and grounding frontmatter, and a report of duplicate pairs; a dry run unless `--write`. |
| `mem-summarize`, `mem-facts`, `mem-verify`, `mem-forget` | Maintenance jobs that propose changes for review; see the sections below. |
| `mem-usage`, `mem-health`, `mem-inbox`, `mem-import` | Usage log, memory health report, review inbox and importers. |
| `mem-crypt`, `mem-share`, `mem-backup`, `mem-nightly` | Encrypted stores, team sharing, backups and the nightly runner. |
| `memd-control` | Token registry administration over SSH: `init`, `import-legacy`, `list`, `activity`, `backup`, `issue`, `revoke` (see [ADMIN-WEB](docs/ADMIN-WEB.md)). |

`python -m memd.reflect` is the propose-only nightly tidy: it writes a dedup and
staleness draft to a fresh Forgejo branch, opens a pull request (`FORGEJO_API`,
`FORGEJO_TOKEN` or `~/.config/forgejo/token`, `MEMD_REPO_PATH` as `owner/repo`) and
never merges or deletes anything.

## Current-state summaries (optional)

"What is the current X" is recall's hardest question: a topic gathers many dated
notes with near-identical wording, and the answer is the newest fact, often inside
a long note. `mem-summarize` writes one "where things stand now" note per topic and
**proposes** it for review; it never writes to the store itself.

It groups live notes that share a tag or slug stem (the slug without dates and
filler words) and have similar titles or openings. A group qualifies with at least
three dated notes and one note marked `volatility: state` or `volatile`. For each
group, up to twelve of its newest notes go to a chat model, which must answer with
claims that each cite a source slug. Claims citing no known source are dropped;
a reply without any cited claim proposes nothing. The resulting note has
`kind: summary`, the topic `cluster`, `sources`, each source's Git blob
(`source_revisions`), `as_of` and `observed_at` set to the newest source date, and a
body of **Now** (cited claims), **History** and **Sources**.

Proposals are committed to the `memd/summaries` branch of the clone (built from a
private index: the checkout, HEAD and concurrent saves are untouched). Review the
branch and merge it to approve, for example `git merge memd/summaries` in the clone
or through a pull request (`--push`, `--pr` reuses reflect's Forgejo settings). Each
run rebuilds the branch from the current store, so re-running updates proposals
rather than duplicating them, and an unchanged pending proposal is reused without
another model call. An approved summary stays current while its sources' blobs are
unchanged. When a source changes, is superseded or deleted, or a new note joins the
topic, the next run reports why and proposes a replacement at the same path.
`--dry-run` prints the plan and proposed notes (the plan alone when no model is
configured); `--max-clusters` (10) caps model calls per run. Run it nightly beside
reflect, e.g. from a systemd timer. Importance defaults to 3 so summaries compete on
relevance; `--importance 4` would also put them in the always-on core index.

## Timeline facts (optional)

`mem-facts` extracts time-bounded facts from notes, such as
`nginx proxy | runs on | vmhost | 2026-08-19 | (open)`, so "what is the current X"
and "what was X on date D" become exact lookups rather than search guesses. Facts
are a derived cache in the index database (tables `facts` and `fact_sources`); the
notes are never written, and a schema change simply re-extracts them.

- **Extraction.** A deterministic pass reads explicit phrasings ("X moved to Y",
  "X was replaced by Y", "X runs on/uses/points to Y since/until DATE", "since
  DATE, X ..."; ISO dates). With `MEMD_LLM_URL` set, the chat model is also asked
  for facts under a strict JSON schema (falling back to the plain prompt when the
  backend refuses `response_format`); malformed replies are dropped and retried on
  the next run. A fact without a date in the text takes the note's `observed_at`,
  then `verified_at`, then the date in its title or slug. Superseded, retracted
  and summary notes are skipped.
- **Incremental.** Each note is extracted per Git blob: a re-run touches only
  changed notes and notes the model has not yet read. `--max-notes` (20) caps model
  calls per run, newest notes first; the rest are reported as deferred.
- **Closing.** When a newer fact has the same subject and predicate but a different
  object, the older fact's `valid_to` becomes the newer `valid_from`; nothing is
  deleted. An "until" date in the text also closes a fact. Undated facts neither
  close nor are closed. Subjects and objects are compared casefolded and trimmed,
  host names through the same aliases as host filters (`MEMD_HOST_NAMES`), and
  common relations are folded ("moved to", "hosted on" -> "runs on").
- **Supersede candidates.** Notes whose every fact was closed by newer facts from
  other notes are listed in the report (`--dry-run` shows them with the extracted
  facts). Nothing is superseded automatically; use `save` with `supersedes`.

`timeline(subject, predicate?, at?)` is an MCP tool and `POST /timeline`
(`{"subject", "predicate"?, "at"?}`), read-only and authorised exactly like
recall. It returns current facts (open `valid_to`), or with `at: "YYYY-MM-DD"`
the facts valid that day, each with `valid_from`, `valid_to`, `source` slug,
`revision` and `closed_by`. `match` is `exact`, `partial` (the subject was named
more briefly than written) or `none`. When a recall query shows present-state
intent ("current", "now", "latest", ...) and facts exist for a subject the query
names, a short **Current facts** block is appended after the notes, only if it
fits the `max_chars` budget; ranking and excerpts are unchanged and
`rendering.current_facts` counts the lines.

```sh
mem-facts --dry-run            # extract and report; store no facts
mem-facts                      # store facts (patterns only unless MEMD_LLM_URL is set)
mem-facts --no-llm             # deterministic pass only
mem-facts --subject "nginx proxy" --at 2026-05-01
```

Run it nightly beside `mem-summarize`. It exits 1 only when every attempted model
extraction failed.

## Self-checking facts (optional)

A note about something that changes (a port, a version, a DNS record, a service
being up) can declare how to check it, and `mem-verify` re-checks it on a schedule:
all probes pass and the note is dated `verified_at: <today>`; any probe fails and it
gets a `verification: {status: failed, checked_at, since, failed: [...]}` marker.
Probes are declared explicitly in a `verify:` frontmatter list (or save's `verify`
argument); nothing is inferred from the body.

```yaml
verify:
- tcp: gpuhost:8077                          # connect, then close
- http: https://memd.example.com/health       # one GET; 2xx unless status is given
  status: 200
  json: status=ok                             # dotted key present (=value: equal)
- dns: host.example.com -> 10.10.1.10       # resolves, and includes that address
- command: some-binary                         # on PATH (shutil.which); never run
- path: /etc/foo                              # exists
```

Every probe is read-only: no shell, no writes, no request bodies or credentials,
at most ten probes per note and `MEMD_VERIFY_MAX_PROBES` per run (least recently
checked notes first, the rest deferred), each step bounded by
`MEMD_VERIFY_TIMEOUT_S`. Save rejects an unknown kind or malformed probe with a
message naming the accepted kinds; `verify: []` removes the declaration, and
changing it drops an old failed marker.

Network targets are fenced by `MEMD_VERIFY_ALLOW`. Without it only loopback,
RFC 1918 and ULA addresses, and names that resolve **only** to them, may be probed;
link-local and public addresses are refused. When set (addresses, CIDRs and domains,
comma-separated; a domain also covers its subdomains) it replaces that default. A
target outside the fence is reported as `blocked` and never contacted, and a note
with a blocked probe gets no change. HTTP redirects are followed by hand, at most
three, and each hop is checked again, so a redirect cannot leave the fence;
environment proxies are not used. `tcp` host names pass through `MEMD_HOST_NAMES`.

Run `mem-verify` **on the host that can reach the targets** (a probe of
`gpuhost:8077` or `/etc/foo` means something only there), e.g. from a daily
systemd timer beside `mem-summarize`. By default the changes are proposed on the
`memd/verify` branch (`MEMD_VERIFY_BRANCH`) exactly as `mem-summarize` proposes:
built from a private index under the clone lock, never touching the checkout, and
rebuilt each run; merge it to approve (`--push` pushes it). `--apply` commits them
to the store instead, through save's locked write, commit, index and sync steps,
skipping any note that changed after it was probed. `--dry-run` probes and prints
the changes without writing; `--json` emits the report.

Recall labels a note whose last check failed within 30 days, whatever its
volatility: `(as of D; verification failed D: <probe> — may be stale, verify live
state before acting on it)`, and counts it in the stale banner. A later
`verified_at` clears the label.

```sh
mem-verify --dry-run           # probe from this machine; write nothing
mem-verify                     # propose on memd/verify
mem-verify --apply             # commit directly
```

The HTTP fence resolves a name before the request and the HTTP client resolves it
again, so a record that changes between the two is not caught; allowlist names you
control.

## Learning from usage (optional)

memd records what agents do after a recall, so real queries can become evaluation
data and, if you opt in, a gentle ranking signal. Recall over HTTP and MCP returns
a `recall_id`; `read` accepts it back (`recall_id`, optional).

- **Usage log.** Per store, a derived SQLite file next to the index
  (`memd.db` -> `memd.usage.db`, owner-only, safe to delete) records each served
  recall (id, time, caller label, normalised query text and its hash, the match
  slugs shown in rank order) and each successful read (slug, caller, and the recall
  it followed: the `recall_id` passed back, else that caller's latest recall within
  15 minutes that returned the slug). Rows older than `MEMD_USAGE_RETENTION_DAYS`
  are pruned on every write. The local CLI and the recall eval do not log.
- **Privacy.** Queries can contain anything a user typed. The text is stored only
  in that file, never in logs or responses; `MEMD_USAGE_LOG=hash` keeps only a hash
  (no golden export then) and `MEMD_USAGE_LOG=off` records nothing. `mem-usage stats`
  prints counts and slugs, never query text; exported candidates contain queries,
  so the export file is created owner-only.
- **Signals.** Only a recall followed by a read says anything (an excerpt may have
  been enough). A returned slug read within 15 minutes is a positive; unread slugs
  ranked above a read one, or in the top 3, are weak negatives ("seen and passed
  over").
- **Boost (off by default).** With `MEMD_USAGE_BOOST=on`, each judged note's read
  rate, smoothed towards the store-wide rate with 5 pseudo-observations, becomes a
  fusion weight between 0.85 and 1.15; notes without evidence keep 1.0, so new
  notes are not penalised. The weighted fused order may move a note at most two
  places, and the reranker still judges the head. It is off because its effect
  cannot be measured offline, and it risks rich-get-richer: a note read because it
  ranked first ranks first again. Skip-above negatives, smoothing, the cap and
  retention bound that; compare `mem-usage stats` read rates by rank before and
  after enabling it.
- **Eval growth.** `mem-usage export-golden` writes candidate golden rows: each
  logged query with its read slugs as grade-2 gold, `category: usage`,
  `source: usage`, `needs_review: true`, and the eval's one-in-three `test` split
  keyed on the query hash (`--split fresh` to mark them all fresh). A read is not
  proof of an answer: review every row before adding it to a golden set (see
  [eval/README.md](eval/README.md)).

```sh
mem-usage stats                                   # counts, read rate by rank, most read/skipped notes
mem-usage export-golden --out /tmp/candidates.jsonl --exclude eval/golden-mine.jsonl
mem-usage --usage-db /path/to/memd.usage.db stats --json
```

## Review inbox (optional)

An agent can *propose* a memory instead of saving it, so a wrong note is stopped
before it is written. A candidate waits in the store's inbox until a person
approves it (optionally after correcting it) or rejects it; nothing reaches the Git
store before approval.

- **Propose.** The `propose` MCP tool and `POST /propose` take exactly save's
  arguments and authorisation (a token that may save may propose). The reply gives
  the candidate `id` and a `lint` preview computed at proposal time by save's
  non-writing checks (`memd.save.dry_run`): the `action` (`create`, `update` or
  `supersede`) and `slug`, `related` notes, `near_duplicates` (keyword or vector),
  `conflicts` and `warnings`, and `error` when save would refuse the fact as
  proposed (a missing `supersedes` target, a stale `expected_revision`). An
  identical pending candidate is returned rather than filed twice.
- **Storage.** Per store, a side file next to the index (`memd.db` ->
  `memd.inbox.db`, owner-only) holds each candidate: title, body, proposed metadata,
  channel (`agent`, `save`, `transcript`, `handoff` or `import:<kind>`), proposer
  label, status (`pending`/`approved`/`rejected`), reviewer, decision time, rejection reason,
  the resulting slug, revision and full save receipt, and the original text when
  the reviewer edited it. Decided candidates are pruned after 180 days; at most
  1,000 may be pending.
- **Review.** `GET /inbox?status=`, `GET /inbox/{id}`,
  `POST /inbox/{id}/approve {"edits"?}` and `POST /inbox/{id}/reject {"reason"?}`
  need a signed-in account session (password login, CSRF-checked) with write access
  to the store. Agent tokens get 403 unless `MEMD_INBOX_TOKEN_REVIEW` allows them:
  `others` lets a write token review candidates proposed under a different label,
  but change only their tags and importance (a token must not rewrite another's
  text and approve it alone); `all` also its own, with any edit, and a token's
  rewrite is then credited to that token. Approval sends the (edited) fact through
  the normal `save` and records the receipt; otherwise the note's `saved_by` is the
  proposer, and the inbox records the reviewer and any original text. A failed save leaves the candidate pending with the error shown.
  Editable fields are save's: title, body, tags, importance, host, description,
  source, dates, pinned, volatility, verify, supersedes, slug and
  expected_revision (`null` removes one). A deployment without accounts has no
  review session: review with `mem-inbox` on the server, or opt tokens in.
- **Personal console.** With the personal memory console enabled, **Inbox** at
  `/memories/inbox` lists the signed-in user's pending candidates with the lint
  preview, lets them correct the title, summary, text, tags and importance, and
  approve or reject. Content is rendered as text only.
- **Save mode.** `MEMD_SAVE_MODE=inbox` makes the MCP and HTTP `save` of a remote
  agent token file a candidate instead of writing. The reply is a save-shaped
  receipt with `saved: false`, `queued: true`, `inbox_id` and `action: proposed`
  (HTTP status 202), so agents do not retry. Account sessions and local callers
  (the CLI, stdio MCP) still save directly. The default, `direct`, leaves save
  unchanged.
- **Handoffs.** Session handoff notes from the Claude Code handoff hook arrive
  on the `handoff` channel (see [Claude Code hooks](#claude-code-hooks)); a newer
  one from the same credential replaces the older pending one for its repository.
- **Transcripts.** `mem-inbox distill <transcript.jsonl|->` reads a Claude Code
  transcript, keeps only user and assistant text (no tool calls, tool output,
  thinking or system reminders), redacts obvious secrets (private keys, bearer and
  API tokens, `*_TOKEN=`/`password:` values, URL credentials, long key-like
  strings), and sends it to the chat model (`MEMD_LLM_URL`) in chunks of 12,000
  characters, at most `--max-chunks` (20), asking for a strict JSON list of durable
  facts. Malformed lines and replies are counted and skipped. Each new fact is filed
  with channel `transcript` and `source: transcript <file>`; facts that repeat an
  existing note (a near-duplicate or the same identity), a pending candidate or
  another fact of the run are skipped. `--dry-run` prints them instead. Off unless
  `MEMD_LLM_URL` is set.

```sh
mem-inbox list                                   # pending candidates, with lint flags
mem-inbox show <id>                              # full text and lint preview
mem-inbox approve <id> --title "Better title" --tags backup,archive
mem-inbox reject <id> --reason "not true"
mem-inbox distill ~/.claude/projects/<project>/<session>.jsonl --dry-run
```

The CLI works on the local store (`MEMD_PROFILE`/`MEMD_CLONE`/`MEMD_DB`) and acts
as its operator; `--reviewer` sets the recorded reviewer (default `cli:$USER`).
Redaction is pattern-based and conservative: review distilled candidates before
approving them.

## Importing memories (optional)

`mem-import` brings existing knowledge in through the [review inbox](#review-inbox-optional):
every item becomes a candidate (channel `import:<kind>`, proposer `mem-import`) and
nothing reaches the store until a person approves it.

| Subcommand | Reads | Files as candidates |
|---|---|---|
| `chatgpt EXPORT` | a ChatGPT data export: the zip, its extracted folder or one JSON file | saved memories (`memor*.json`/`.txt` members; a list of strings or objects with `content`/`text`); `conversations.json` only with `--distill` (the visible branch of each chat, user and assistant text only) |
| `claude EXPORT` | a Claude data export, same forms | memory (`memories.json`, one candidate per headed section, project memories included) and project knowledge (`projects.json`: description and instructions, and each document); `conversations.json` only with `--distill` |
| `markdown DIR` | a folder of `.md`/`.markdown` files | one candidate per file: title from frontmatter `title`, the first `# ` heading or the file name; tags and `importance` from frontmatter; the date from frontmatter (`observed_at`, `updated`, `modified`, `date`, `created`) or, in a Git work tree, the file's last commit |
| `github-prs OWNER/REPO` | merged pull requests from the GitHub REST API (`GITHUB_TOKEN` optional, `GITHUB_API_URL` for another server) | the title and only the description sections that read as decisions, rationale, alternatives or breaking changes, with the PR URL |

- **Provenance.** Each candidate's `source` is `<label> <ref>`: the label
  (`--source-label`, default `chatgpt export <file>`, `claude export <file>`,
  `markdown <folder>`, nothing for GitHub) and the memory or project id, the
  conversation id, the file path or the pull request URL. Tags add `import`,
  `import:<kind>` and the item's own (`chatgpt-memory`, `claude-project`,
  frontmatter tags, `decision` and PR labels).
- **Never twice.** An item is skipped when its source was already proposed (a
  candidate of any status, until decided candidates are pruned) or saved (a note's
  `source`), when the same text is already a candidate or another item of the run,
  or when save's dry run calls it a near-duplicate or an update of an existing note.
  Re-running an import files only what is new; a changed file under an already
  imported path is not re-proposed.
- **Secrets.** Text is redacted with the inbox's patterns before it is compared,
  printed, stored or sent to the model. Review imported candidates anyway.
- **Limits.** `--max N` (100) candidates per run, never more than the inbox's
  pending limit (1,000) leaves room for. Markdown skips hidden paths, symlinks,
  binaries, non-UTF-8 files, files over `--max-bytes` (256 KiB), paths matching
  `--exclude` (repeatable, `.gitignore`-style: `drafts/`, `*.tmp.md`, `/top/only.md`)
  and, in a Git work tree, files Git ignores. GitHub pages newest first and stops
  at `--since` or the cap.
- **Conversations.** `--distill` sends each conversation, redacted and in the same
  bounded chunks as `mem-inbox distill` (`--max-chunks` per conversation), to the
  chat model (`MEMD_LLM_URL`); a conversation already distilled is not sent again.
  Without it conversations are counted, not read.
- **Output.** `--dry-run` lists what would be filed and writes nothing; `--json`
  prints the report (filed, skipped with the reason or what an item repeats,
  errors for malformed entries). Malformed entries are reported and skipped; the
  exit status is 1 when nothing usable was found or GitHub refused.
  Only `github-prs` uses the network (and `--distill`, the chat model).

```sh
mem-import chatgpt ~/Downloads/chatgpt-export.zip --dry-run
mem-import claude ~/Downloads/claude-data.zip --distill --max 50
mem-import markdown ~/notes --exclude drafts/ --exclude '*.private.md' --source-label "notes wiki"
GITHUB_TOKEN=... mem-import github-prs example-org/widgets --since 2026-01-01
mem-inbox list                                   # then review as usual
```

## Memory health

`GET /insights` (a bearer token or account session that may read the store; a
stats-only monitoring token is refused, since the report names notes; `?profile=`,
`?limit=` items per list, 1-100, default 20, `?fresh=true` to bypass the cache)
and `mem-health` report where a store's memory is rotting or wasted. The report is
read-only: the index, usage and inbox files are opened read-only and Git is asked
for one tree listing. It is cached per store for 60 seconds.

- **Never recalled.** Live notes no logged recall returned. Only notes that
  already existed when the usage window began are judged (from Git history; the
  file modification time when Git cannot say), so a new note is never counted.
  The window is the retained usage log, at most its newest 20,000 recalls.
- **Shown, never read.** Notes recall returned at least 3 times that were never
  read. **Most useful** lists the most read notes with their read rate.
- **Stale.** Notes whose `volatility` is `state` or `volatile` and whose as-of date
  is past its window, or with a recent failed `mem-verify` check. **Failed
  verifications** lists those checks. The heatmap counts live notes by tag or host
  and by the age of their as-of date (`verified_at`, `observed_at`, a date in the
  title or slug; otherwise `undated`), with each row's stale count.
- **Contradictions.** One subject and predicate with different current values in
  different live notes (`mem-facts`). **Supersede candidates** are notes whose
  every fact was closed by newer notes. Neither is ever acted on here.
- **Stale summaries** (sources changed or retired), **awaiting review** (pending
  inbox candidates) and **index pending** (notes without a current note vector or
  chunk vectors).
- **Archived** notes (`mem-forget`, approved) and archive proposals still waiting
  on the forget review branch. Archived notes are not live, so no other section
  counts them.

Every list carries its full `total` and at most `limit` items. A section whose
source is missing (usage logging off or empty, no facts extracted, no summaries,
no verify probes, no volatility declared) says `available: false` with a `reason`,
never a misleading zero.

```sh
mem-health                 # text report, for cron mail
mem-health --json --limit 50
```

## Metrics (Prometheus)

`GET /metrics` serves the Prometheus text format (version 0.0.4) for Prometheus
or Grafana Agent/Alloy. It is authorised like `/stats`: any token that may read
`/stats` may scrape, including a registry token with only the `stats` operation
(a monitoring token); a token without it gets 403. Store gauges cover only the
stores that token could ask `/stats` about. The output never contains note text,
queries, slugs, tokens or credential labels: the only free-form label is `store`,
and store names are chosen by the operator.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `memd_build_info` | gauge | `version`, `app_commit` | Always 1; the running version and application commit (as `/health`). |
| `process_start_time_seconds` | gauge | | Process start, Unix time. |
| `memd_http_requests_total` | counter | `route`, `status` | HTTP requests by route (`recall`, `read`, `save`, `propose`, `ask`, `publish`, `timeline`, `mcp`, `health`, `stats`, ...; anything else is `other`) and status class (`2xx`, `4xx`, ...). |
| `memd_http_request_duration_seconds` | histogram | `route` | HTTP request latency. |
| `memd_mcp_tool_calls_total` / `memd_mcp_tool_duration_seconds` | counter / histogram | `tool`, `outcome` | MCP tool calls (`ok` or `error`) and latency. |
| `memd_recall_arm_duration_seconds` | histogram | `arm` | Recall stages: `embed`, `vector`, `bm25`, `fresh`, `rerank`, `total`. |
| `memd_recall_fallbacks_total` | counter | `reason` | `embed_timeout`, `embed_failed`, `vector_empty`, `rerank_skipped`, `rerank_failed`. |
| `memd_recall_order_total` | counter | `order` | Which ordering served a recall: `rerank`, `vector`, `bm25` or `core_only`. |
| `memd_backend_calls_total` | counter | `backend`, `outcome` | Embedding, rerank and chat model calls (`embed`, `rerank`, `chat`). |
| `memd_backend_errors_total` | counter | `backend`, `kind` | Failures by kind: `timeout`, `connect`, `http_4xx`, `http_5xx`, `bad_response`, `other`. |
| `memd_backend_call_duration_seconds` | histogram | `backend` | Model call latency. |
| `memd_saves_total` | counter | `outcome` | `saved`, `not_saved` or `error` (the save raised). |
| `memd_save_receipts_total` | counter | `stage` | Saved receipts reporting `lexical_indexed`, `indexed` or `synced`. |
| `memd_save_conflicts_found_total` | counter | | Advisory conflicts reported by saves. |
| `memd_usage_log_events_total` | counter | `kind`, `result` | Usage log writes (`recall`, `read`; `logged`, `failed`). |
| `memd_index_notes` | gauge | `store` | Live notes in the index. |
| `memd_index_pending_vectors` / `memd_index_pending_chunks` | gauge | `store` | Live notes without a current note vector / chunk vectors. |
| `memd_index_in_sync` | gauge | `store` | 1 when the lexical index is at the clone's Git HEAD. |
| `memd_index_head_age_seconds` | gauge | `store` | Age of the commit the lexical index is at. |
| `memd_index_size_bytes` | gauge | `store` | Index database size. |
| `memd_inbox_pending` | gauge | `store` | Review inbox candidates waiting for a decision. |

Counters and histograms live in the process and restart from zero with it.
Store gauges are computed at scrape time from read-only queries and cached per
store for 15 seconds. Histogram buckets run from 5 ms to 30 s.

```yaml
# prometheus.yml
scrape_configs:
  - job_name: memd
    scrape_interval: 30s
    metrics_path: /metrics
    scheme: https
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/memd-monitoring-token   # a stats-only token
    static_configs:
      - targets: ["memory.example.com"]
```

Issue the monitoring token from the token registry (`MEMD_CONTROL_DB`) with `stats`
as its only operation, for example with `memd-control issue --operations stats
--stores a,b ...` or in the `/admin` console; list every store it should report.
To let a collector on the same host scrape without a token, set `MEMD_METRICS_PUBLIC=1`:
an unauthenticated request is then served only from a loopback address carrying no
`X-Forwarded-For`, `Forwarded` or `X-Real-IP` header (so nothing arriving through
a reverse proxy qualifies), and it reports only the serving store's gauges.

Useful queries: `rate(memd_recall_fallbacks_total[5m])` for degraded recall,
`histogram_quantile(0.95, sum by (le) (rate(memd_http_request_duration_seconds_bucket{route="recall"}[5m])))`
for recall latency, `memd_index_in_sync == 0` and `memd_index_pending_vectors > 0`
for indexing that is behind, and `memd_inbox_pending` for review backlog.

## Forgetting with review (optional)

`mem-forget` keeps a store lean without ever deleting on its own. It proposes
archiving notes nothing uses any more, on a review branch; nothing changes until a
human merges it. A note is proposed only when **all** of these hold, and each
candidate is listed with its reasons:

- it is live (not superseded, retracted or already archived) and committed;
- its last activity is older than `--days` (180): the newest of `verified_at`,
  `observed_at`, a date in its title or slug, and the last commit touching its
  file, so any edit, re-save or passing re-check keeps it;
- its importance is at most `--max-importance` (2; 3 at most, since 4 and above
  is core-eligible);
- it is not protected: not pinned, not core-eligible, not a current-state summary,
  not a source a live summary cites, not the note another was superseded by (or
  names in `supersedes`), not the source of a note published from it
  (`published_from`); protected notes are listed as kept, with the reason;
- and at least one signal says it is dead weight: never recalled or read in the
  usage window (only when the usage log covers 30 days or more and the note
  existed before the window began), stale by the staleness rules (changeable
  state past its window, or a recent failed `mem-verify` check), or every fact it
  states was closed by newer notes (`mem-facts`).

An archive is not a delete and not a move. The proposal adds `archived: <date>`
and `archived_reason` to the note's frontmatter in place, so the file keeps its
path and `git log` on it reads as one history (a move to an `archive/` directory
would make every archive a rename to follow, and an encrypted store's opaque file
names have no directory to move into). Proposals are committed to
`memd/forget` (`MEMD_FORGET_BRANCH`) exactly as `mem-summarize` proposes: a
throwaway index under the clone lock, the checkout untouched, envelopes and a
generic message on an encrypted store. An unchanged pending proposal is kept as
it is on later runs; `--max` (25) caps proposals per run.

Once merged, an archived note is excluded from recall and the core index (recall
takes `include_archived: true`, `mem recall --include-archived`, for explicit
digging), and `read` still returns it with `archived`, `archived_reason` and a
notice saying it is history. `mem-forget restore <slug>` brings one back (a
direct commit through the save path, like `mem-verify --apply`); saving to the
note does too, and so does reverting the merge commit.

```sh
mem-forget --dry-run            # candidates, reasons and kept notes; write nothing
mem-forget                      # propose on memd/forget
git -C "$MEMD_CLONE" merge memd/forget   # approve
mem-forget restore old-relay-port        # bring one back
```

## Team sharing (optional)

A user can publish a note from their own store into a shared (team) store, and
recall can span every store the caller is granted, each result labelled with the
store it came from. Nothing changes until a caller asks for it: recall without
`stores`/`scope` reads exactly the caller's own store, as before. Set up a team
store and grants as described in
[Administration](docs/ADMINISTRATION.md#team-stores-and-sharing).

**Publish.** `publish(slug, target_store, store?)` (MCP tool, `POST /publish`,
`mem-share publish`) needs read access to the source store (`store`, default your
own) and write access to the target, through the same grants, token scopes,
bound identity and instance lock as any other call. The note goes through the
target's normal save path, or its review inbox when the target store's
`publish_review` setting (Settings: **Published notes need review**) is on
(the default; the reply is `action: queued` with
the candidate id, HTTP 202) or `MEMD_SAVE_MODE=inbox` queues agent saves. The copy
gets provenance frontmatter:

```yaml
published_from:
  store: amber
  slug: archive-backup-window
  revision: 8d3dd718...     # the source note's Git blob when published
  by: laptop-agent          # the publishing credential's label
  at: '2026-09-28T10:00:00Z'
```

Publishing the same note again updates that copy, found by its provenance (never
by title), with a revision guard; an unchanged source is a no-op (`action:
unchanged`). A new copy keeps the source slug unless the target already uses it
for another note, which is then left alone and the copy becomes `<slug>-2`.
`save` and `propose` payloads cannot set `published_from`.

Only these fields travel: title, body, host, tags, importance, description,
source, observed_at, verified_at, volatility and the `verify` probes. Everything
else stays behind: `saved_by` (the copy is stamped with the publisher), `pinned`
(a personal core-index choice), `last_used` and the usage log (personal usage
data, never read by publish), `superseded_by` and `grounding` (decided by the
target), mem-verify's `verification` marker (host-specific check state), any
`published_from` the source carried, and every other frontmatter key. The list is
an allowlist in `memd/share.py` (`TRAVELS`), so a new field stays private until
it is added there. A field the source drops later (say its description) is not
removed from an existing copy by a republish.

Publishing from an [encrypted store](#encrypted-personal-stores-optional) into an
unencrypted one is refused unless the call passes `allow_decrypted_publish: true`,
because the copy puts the plaintext in the target's repository.

**Federated recall.** `recall` takes `stores` (a list, or a comma-separated
string, of at most 16 stores) or `scope: "all"` (every store the caller is
granted, their own first). Each named store is resolved exactly like a `profile`
argument, one refused name refuses the whole call with 403, and the reply never
says whether a refused store exists. Recall runs once per store, four at a time,
under one deadline (embed + rerank deadlines + 1.5 s, or
`MEMD_FEDERATED_DEADLINE_MS`). Query matches are fused by reciprocal rank across
stores (each store's best match, then each store's second, ...) and cut to `k`.
Every note carries `store`, excerpts carry `store`, and the rendered context
marks each heading `### [store] slug`. The reply lists the `stores` searched and
`stores_skipped` (a store that failed or missed the deadline, with the reason).
A federated recall gets one `recall_id`, logged in each store's usage log with the
matches shown from that store (a `read` that names its `store` finds it there). It
has no current-facts block.

`read` takes `store` (or its older name `profile`) and checks it the same way,
so reading a federated result needs the matching grant: `read(slug, store=team)`.

**Upstream drift.** When the source note changes after publishing, the copy is
*behind*. `read` of a published copy returns its `published_from` and, if the
caller may also read the source store, `upstream: {store, slug,
published_revision, current_revision, status}` with `status` `current`, `behind`,
`missing` or `unknown`; recall notes and excerpts carry the same `upstream`
(checked against the source's index). A caller who cannot read the source learns
nothing about it. Publish again to update the copy.

```sh
mem-share status --store team           # published notes and whether each is behind
mem-share publish archive-backup-window team --from amber
```

`mem-share` is the local operator's tool: it is not confined by
`MEMD_ENFORCE_PROFILE`, but every store name must still be registered and its
paths are still guarded.

Limits: an OIDC- or Kasm-bound identity addresses only its own store, and a
`MEMD_CONTROL_DB` registry token only its one store, so neither can publish or
federate; administered accounts and their tokens can (a token reaches other
stores only when issued with additional stores: `extra_stores`, or
**Additional stores** in Administration → Issue token).

## Encrypted personal stores (optional)

A store can keep its notes in Git as ciphertext, so a personal store can live on
any Git host, including an untrusted or public one. Only the memd server holding
the store's key decrypts it; the local SQLite index stays plaintext, so recall,
read, save, revision guards, the inbox, facts and health work exactly as on a
plaintext store.

```sh
mem-crypt keygen /srv/memd/keys/personal.key   # 32 random bytes, mode 0600; prints the key id
export MEMD_AMBER_KEY_FILE=/srv/memd/keys/personal.key   # legacy store (or the store's key_file setting)
mem-crypt encrypt --dry-run                    # list the files it would convert
mem-crypt encrypt                              # every note, one commit
mem-crypt status                               # key id, note count, health of the key
mem-crypt decrypt [--dry-run]                  # back to plaintext notes, one commit
```

- **Format.** Each note is `<hex>.md.enc`, where the name is the first 32 hex
  characters of HMAC-SHA256 over the note's slug (with a key derived from the
  store key), so file names say nothing about topics. The file is one base64
  line: magic, format version, key id, a random 96-bit nonce and the AES-256-GCM
  ciphertext of the note's Markdown together with its original file name, slug
  and title. The associated data binds each ciphertext to its path, so a file
  moved, swapped or edited fails authentication. A tracked `.memd-encrypted`
  marker at the root records the key id; it is what makes memd treat the clone
  as encrypted, never configuration alone. The only slug-to-file mapping in
  plaintext is the local index.
- **Keys.** A key file holds 32 random bytes (64 hex characters) and must be
  mode 0600 or stricter; memd refuses a looser one. Legacy stores read
  `MEMD_<PROFILE>_KEY_FILE`; administered stores have a `key_file` setting
  (Settings, or `config.key_file` through the admin API). HKDF derives separate
  keys for encryption, file names and the public key id from it. Key material
  and decrypted notes are never logged or put in error messages.
- **Back up the key**, apart from the store and its Git host (a password manager
  or offline copy). Losing it loses every note: there is no recovery, and the
  Git history holds only ciphertext. There is no in-place rotation: decrypt, then
  encrypt with a new key.
- **Fail closed.** A missing, unreadable, too permissive or wrong key (its id
  differs from the marker's), an envelope that fails authentication, plaintext
  notes or mem-carve output (`MEMORY.md`, `MEMORY-full.md`) in an encrypted
  store, or envelopes without the marker all make the store unavailable: reads,
  saves, reindex and the review branches refuse, nothing is written, and
  `/health` reports `checks.encryption` as failed with status `down`. `/health`
  is unauthenticated, so it says only "encryption check failed; see server log":
  the reason is in the server log and in `mem-crypt status`. A key configured for
  a clone that is not encrypted is refused too, until `mem-crypt encrypt` has
  run. Nothing in the committed marker relaxes these checks: a migration in
  progress is recorded only inside the clone's `.git` directory, so the Git host
  cannot claim one, and a marker that says `migrating` is refused outright.
- **Migration.** `mem-crypt encrypt` and `decrypt` work on the store
  `MEMD_PROFILE` selects, under the clone lock, and commit everything at once
  (`--dry-run` prints the file mapping only). Every blob changes, so vectors are
  re-embedded once. Envelopes are written at the clone root; the note's original
  path is inside, and an administered store's include/exclude filters choose
  which plaintext files are notes but never hide envelopes. Line endings are
  normalised to LF. Encrypting also removes `MEMORY.md` and `MEMORY-full.md`
  (mem-carve output, which lists every note's title and first line) in the same
  commit. If a migration fails its files are restored; if it is killed, the store
  is refused until a re-run of the same command finishes it. Decrypting restores
  each note's original path, then asks you to remove the key setting; it does not
  recreate mem-carve output (run `mem-carve` again if you use it).
- **Review branches.** `mem-summarize`, `mem-verify` and `mem-forget` write envelopes on
  `memd/summaries`, `memd/verify` and `memd/forget` too, and `mem-verify --apply`
  and `mem-forget restore` commit through save's encrypted write.
- **Not supported.** Obsidian vault stores cannot be encrypted (the vault holds
  the notes in plaintext; memd refuses the setting and the marker). Per-user
  stores under `MEMD_STORES_ROOT` have no key setting. `mem-carve`, `mem-sweep`
  and `reflect` work on plaintext files and refuse an encrypted store, and the
  client-side degraded grep fallback finds nothing in an encrypted checkout.

**Threat model.** The encryption protects note contents, titles, slugs, tags,
metadata and file names from whoever holds the Git repository. The Git host
still sees how many notes there are, each file's approximate size (sizes are not
padded), which files each commit touches (so how often a note changes), commit
times and the commit author configured in the clone, the marker (key id and
cipher) and the review branch names. Commit messages are fixed: store commits
are `memd: update memory` with no `Saved-By` trailer (the writer is recorded
inside the note), review branch commits are `memd: proposed changes for review`,
and migrations are `memd: encrypt store` / `memd: decrypt store`. Authentication
stops edited or swapped files, but a host can still withhold, delete or roll back
whole commits or files to an older valid version; review the history if that
matters. The memd server, its key file and its local index (plaintext, like the
usage log and inbox) are trusted: protect that disk as before.

**Existing history.** `mem-crypt encrypt` does not rewrite history: earlier
commits still hold every plaintext note. Before pushing an encrypted store to an
untrusted host, give it fresh history and a new, empty repository: stop memd,
then in the clone run `git checkout --orphan fresh && git commit -m "memd: update
memory" && git branch -D main && git branch -m main`, point `origin` at the new
repository and push. Delete or keep private the old repository and any other
clones, which still hold the plaintext. A store that starts encrypted (a new
administered store with a `key_file`) never has plaintext history.

## Backups and restore drills (optional)

Git holds the notes, but not everything memd keeps: the review inbox, the usage
log and the administration and token databases live only on the server's disk.
`mem-backup` backs all of it up in one encrypted file and proves, on a schedule,
that the file restores.

```sh
export MEMD_BACKUP_DIR=/srv/memd-backups            # bundles are written here
export MEMD_BACKUP_KEY_FILE=~/.config/memd/backup.key
mem-backup keygen                  # once: a new 0600 key file (never overwritten)
mem-backup create                  # one bundle, then prune to MEMD_BACKUP_KEEP (14)
mem-backup verify                  # authenticate the newest bundle; writes nothing
mem-backup restore --to /tmp/memd-restore   # a new or empty directory only
mem-backup drill                   # restore into scratch space and check it works
```

A bundle (`memd-backup-<UTC time>-<random>.mbk`) holds, for every store the
process can see (legacy profiles, administered stores and stores under
`MEMD_STORES_ROOT`): a `git bundle --all` of the clone (every committed note and
its history, review branches included), consistent SQLite snapshots of its
`memd.inbox.db` and `memd.usage.db`, and the store's key-file *reference* (path,
setting, key id). It also holds snapshots of `MEMD_ADMIN_DB` and
`MEMD_CONTROL_DB` with browser sessions removed. The index is not included: it is
rebuilt from Git (`mem reindex`). Uncommitted working-tree edits are not included.

The file is AES-256-GCM in 1 MiB chunks under a per-bundle key derived (HKDF)
from the backup key. Each chunk's nonce carries its position and a final-chunk
flag, and the header is authenticated with every chunk, so a flipped bit,
reordered or missing chunks, truncation, trailing bytes and a wrong key are all
refused. Inside, a manifest lists the stores, their HEADs, refs and note counts,
and each member's size and SHA-256; `verify` and `restore` check every one.

**Keys are never in a bundle.** Neither the backup key nor any store key is
written into it; back them up separately (see docs/OPERATING.md). Without the
backup key no bundle can be read; without a store's key its restored notes stay
ciphertext.

`mem-backup drill` restores the newest bundle into a temporary directory, clones
every Git bundle and checks its HEAD, refs and note count against the manifest,
opens every restored database (integrity check), rebuilds a lexical index for
one store (a plaintext one, else an encrypted one whose key file is present) and
runs a sample recall on it, then deletes the directory. It exits 1 and names each
failed check. Add `backup` and `drill` to `MEMD_NIGHTLY_STEPS` to run both nightly.

## Nightly jobs

`mem-nightly` runs the background jobs in order and prints one summary:
`facts`, `summarize`, `verify`, `forget`, `backup`, `drill`, `health`. The default set is
`facts,summarize,health`; choose others with `--steps` or `MEMD_NIGHTLY_STEPS`.
`summarize` is skipped (not failed) without `MEMD_LLM_URL`. `verify` is opt-in
because its probes go out from whichever host runs the job; `forget` is opt-in so
a store's first archive proposals are reviewed by hand (`mem-forget --dry-run`).
`backup` (`mem-backup create`) and `drill` (`mem-backup drill`) are opt-in and
need `MEMD_BACKUP_DIR` and `MEMD_BACKUP_KEY_FILE`.
`--dry-run` reaches every step that has one, and `--push` pushes the summarize,
verify and forget review branches. Each step runs even when an earlier one failed; the exit status is 1
if any failed.

`systemd/memd-nightly.service` and `.timer` are example user units: they read
`~/.config/memd/env` and run at 04:05 (after reflect), with a randomised delay. The
other units in `systemd/` (service, index sync, guard, reflect) describe an earlier,
non-container layout; check them against [OPERATING.md](docs/OPERATING.md) before use.

## Design

Recall serves its available lexical index while a stale Git snapshot or pending
vectors trigger background refresh. Empty caches seed keyword data without bulk
embedding. Each retrieval arm contributes up to 50 candidates, forming a pool of
at most 100, fused by weighted reciprocal rank. A query that asks about the present
("current", "latest", "right now") adds a third, newest-first arm. The vector arm
searches overlapping ~1000-character chunks of each note (split on headings, then
paragraphs, each embedded with the note's title) and scores a note by its best chunk;
the reranker sees that chunk when it lies past the note's opening. The top 15 fused
candidates, with three seats held for keyword hits, are reranked and that order is
blended with the fused order; the remaining fused order fills larger requests.
Unavailable model services leave keyword search usable. Superseded and archived notes
remain in Git but are excluded from normal search. Full-note reads preserve
provenance and revision information.

Writes are serialized on the dedicated clone. Explicit note identity is separate
from related-note discovery, and receipts report whether persistence, indexing and
remote synchronization succeeded. Background maintenance can propose cleanup for
review without replacing the human-readable source of truth.

Tests use temporary Git/SQLite stores, mocked endpoints and a socket guard. They
must never use production tokens, connect to real services or edit live
client configuration. The code, deployment configuration and current health
responses are the operational authority.

## OIDC, token registry and per-user stores (optional)

Everything in this section is off unless its environment variable is set, so an
existing deployment behaves exactly as before.

- **Agent OIDC** (`MEMD_OIDC_ISSUER`, with `MEMD_PUBLIC_URL`): memd acts as an OAuth
  2.1 resource server. A signed bearer with the `memd` scope (`MEMD_OIDC_SCOPE`)
  binds the caller's own store; discovery is `/.well-known/oauth-protected-resource`,
  which advertises `MEMD_OIDC_AUTHORIZATION_SERVER` (default: the issuer) and the
  scopes in `MEMD_OIDC_ADVERTISED_SCOPES` (default `memd offline_access`).
  `MEMD_OIDC_JWKS_URI` overrides the key-set URL otherwise derived from the
  authorization server. See `docs/plans/2026-09-14-oidc-resource-server.md`.
- **Kasm Workspaces sessions** (`MEMD_KASM_JWT_PUBKEY`): a Kasm-signed session token
  is accepted as an identity while its session is listed in the identity map
  (`MEMD_KASM_USER_MAP`, a JSON file kept by an external sync job); anything missing
  or unreadable refuses.
- **Per-user stores** (`MEMD_STORES_ROOT`): an authenticated identity selects
  `<root>/<store>/{clone,memd.db}` and no request field can move a caller off it.
  See `docs/plans/2026-09-14-multi-tenant-stores.md`.
- **Token registry** (`MEMD_CONTROL_DB`): scoped, expiring `memd_` automation
  tokens with rotation and audit, managed by `memd-control`. Enabling it is a
  one-way cutover: the legacy token file is never read again.
- **Admin console** (`MEMD_WEB_OIDC_ISSUER`) at `/admin`, and the **personal
  memory console** (`MEMD_USER_OIDC_ISSUER`) at `/memories` with self-service
  onboarding tokens. The admin console needs `MEMD_CONTROL_DB`, `MEMD_PUBLIC_URL` (a
  fixed HTTPS origin) and `MEMD_WEB_CLIENT_ID`/`MEMD_WEB_CLIENT_SECRET_FILE`; the
  personal console also needs `MEMD_STORES_ROOT` and
  `MEMD_USER_CLIENT_ID`/`MEMD_USER_CLIENT_SECRET_FILE`. See [ADMIN-WEB](docs/ADMIN-WEB.md),
  [USER-WEB](docs/USER-WEB.md) and [PERSONAL-ONBOARDING](docs/PERSONAL-ONBOARDING.md).
  When administered accounts (`MEMD_ADMIN_DB`) are also on, the account dashboard
  keeps `/` and the SSO entry page is served at `/sso`.

Authentication order for REST and MCP: administered account token or browser
session, then legacy token file (until the registry cutover), then registry
`memd_` tokens, then a Kasm session token, then an OIDC bearer.

A note records `saved_by`, the label of the credential that wrote it, and the
commit carries a matching `Saved-By` trailer.

## Environment variables

A server or CLI process reads these from its environment and from
`~/.config/memd/env`; an explicit `MEMD_ENV_FILE` names a different file and, unlike
the implicit default, overrides inherited process values. Unset means the default
shown; most numeric settings fall back to their default when unparseable. Variables
for the hooks and bridge on client machines are in the last table.

### Server and stores

| Variable | Default | Purpose |
| --- | --- | --- |
| `MEMD_PROFILE` | first of `MEMD_LEGACY_PROFILES` (`amber`); the container sets `personal` | The store a process or CLI works on. |
| `MEMD_ENFORCE_PROFILE` | unset; the container sets `1` | `1` locks the instance to `MEMD_PROFILE`: requests naming another store are refused. |
| `MEMD_REQUIRE_RECALL_TOKEN` | unset; the container sets `1` | `1` requires a valid token for recall, ask and timeline. |
| `MEMD_ALLOW_UNAUTHENTICATED` | unset | `1` re-opens the pre-registry mode that serves remote callers without a token. By default they are refused. |
| `MEMD_CLONE` / `MEMD_DB` | `~/.memd/<profile>/clone` and `memd.db`; the container uses `/data/default/clone` and `index.db` | Notes clone and index database of a single-store deployment. `MEMD_<PROFILE>_CLONE`/`_DB` set them per legacy profile; neither applies under `MEMD_STORES_ROOT`. |
| `MEMD_LEGACY_PROFILES` | `amber,cobalt` | Names of the env-configured profiles (`MEMD_<NAME>_CLONE`/`_DB`/`_REPO`/`_CRED`); the first is the default `MEMD_PROFILE`. |
| `MEMD_TOKENS_FILE` / `MEMD_TOKEN` | `/home/memd/.memd/tokens` / unset | Legacy bearer tokens: a file of `<label> <token>` lines, reread on every request (mount its directory, not the file; `deploy/memd-token` edits it), and one extra token labelled `legacy`. Unused once `MEMD_CONTROL_DB` is set. |
| `MEMD_ENV_FILE` | `~/.config/memd/env`; clients use `~/.config/memd/client.env` | Environment file read as a configuration layer. |
| `MEMD_CORE_LIMIT` | `8` | Default number of core-index entries per recall (a call's `core_limit` takes 0..50). |
| `MEMD_BACKGROUND_REFRESH` / `MEMD_STARTUP_REFRESH` | on | `0`, `false`, `off` or `no` turns off the background index refresh / the refresh at server start. |
| `MEMD_GIT_SHA` | unset | The application commit `/health` reports as `app_commit`. `deploy/apphost/Dockerfile` stamps it from the `GIT_SHA` build argument; otherwise memd asks the checkout, then reports `unknown`. |
| `MEMD_DATA_DIR` | `/data` | Data root of the container entrypoint (`python -m memd.bootstrap`). |
| `MEMD_ADMIN_DB` | the container uses `/data/control/admin.db` | SQLite database of administered accounts, stores, tokens and audit; setting it turns administration on. |
| `MEMD_ADMIN_PASSWORD_FILE` / `MEMD_ADMIN_USERNAME` | unset / `admin` | Creates the first administrator at container start from a mounted password file, only while none exists. |
| `MEMD_VAULT_ROOTS` | `vaults` beside the admin database; the container uses `/vaults` | Folders (separated like `PATH`) under which Obsidian vault stores may be mounted. |
| `MEMD_SCRYPT_CONCURRENCY` | `2` | Password hashes that may run at once; each holds about 128 MB while it runs. |
| `MEMD_AUDIT_READ_DAYS` | `90` | With administration on: days to keep audit entries for recall and read (at least 1). Export and administrative entries are kept. |
| `MEMD_GIT_REMOTE`, `MEMD_SSH_KEY`, `MEMD_SSH_KNOWN_HOSTS` | unset | Remote wiring and deploy key of the portable entrypoint (`deploy/entrypoint.py`); `MEMD_SSH_KEY` and the known-hosts file also serve per-user store clones. |
| `MEMD_GIT_AUTHOR_NAME` / `MEMD_GIT_AUTHOR_EMAIL` | `memd service` / `memd@example.invalid` | Git identity the entrypoint and per-user stores set on a new clone. |
| `MEMD_STORES_ROOT` | unset | Turns on per-user stores under this directory. |
| `MEMD_STORES_REPO_TEMPLATE` | unset | Git remote for per-user stores, containing `{store}`. Unset leaves them local-only. |
| `MEMD_STORES_LOCAL_DOMAIN` | unset | The one email domain whose users get a short `u-<localpart>` repository name; other domains keep theirs in the name. |
| `MEMD_CONTROL_DB` | unset | The token registry database; see above. |
| `MEMD_<STORE>_PUBLISH_REVIEW` | `true` | For an environment-configured store: `false` saves published notes directly instead of queueing them in its review inbox (administered stores use the `publish_review` setting, **Published notes need review** in Settings). |
| `MEMD_FEDERATED_DEADLINE_MS` | embed + rerank deadlines + 1500 | One deadline for a whole federated recall, 100..30000; stores still running are skipped and reported. |
| `MEMD_<PROFILE>_KEY_FILE` | unset | Key file (32 random bytes, mode 0600) of a legacy profile's [encrypted store](#encrypted-personal-stores-optional). |
| `MEMD_METRICS_PUBLIC` | unset | `1` serves `GET /metrics` without a token to loopback callers that carry no forwarding header; see [Metrics](#metrics-prometheus). |
| `MEMD_LOCAL_HOST` | unset | Host a new note is scoped to when the call names none; `any` disables host inference. Unset uses this machine's short hostname when it is a known host name (see `MEMD_HOST_NAMES`), else `any`. |
| `MEMD_HOST_NAMES` | unset | Maps the placeholder host roles (`gpuhost`, `lapbox`, `vmhost`, `lxc`, `remote`, and `apphost` for host filters) to real hostnames, e.g. `gpuhost=ws1,apphost=nas`. |
| `MEMD_HOST_SIGNALS` | unset | JSON map of host role to extra phrases that scope a note to it during `mem-sweep`, e.g. `{"vmhost": ["10.10.1.11"]}`. |
| `MEMD_ORG_NAME` | `memd` | Organisation name shown in the web consoles. |
| `MEMD_ADMIN_GROUP` / `MEMD_USER_GROUP` | `memd-admins` / `memd-users` | Identity-provider groups for the admin console and personal memory console. |

### Model services

| Variable | Default | Purpose |
| --- | --- | --- |
| `MEMD_EMBED_URL` | `http://127.0.0.1:8000` | Base URL of the embedding service; `/v1/embeddings` is appended (no trailing slash). An administered store may set its own in Settings. |
| `MEMD_EMBED_MODEL` | `DEFAULT_EMBED_MODEL` in `memd/config.py` | Embedding model id sent with each request. |
| `MEMD_RERANK_URL` / `MEMD_RERANK_MODEL` | `http://127.0.0.1:8000` / `DEFAULT_RERANK_MODEL` in `memd/config.py` | Optional reranker; recall degrades to vector or keyword order when it is down. |
| `MEMD_RERANK_PATH` | `/api/v1/reranking` | Reranker path. Cohere-shaped services use `/v1/rerank`. |
| `MEMD_EMBED_DIM` | `768` | Vector width, 8..8192. Changing it rebuilds the vector cache; notes and keyword search are untouched. |
| `MEMD_EMBED_DEADLINE_MS` / `MEMD_RERANK_DEADLINE_MS` | `800` / `900` | Hot-path model deadlines, clamped to 100..10000. |
| `MEMD_RECALL_VECTORS` | `chunks` | Recall's vector arm: `chunks` (best chunk per note) or `notes` (one whole-note vector, the earlier arm). Both are always indexed, so switching needs no re-embed. |
| `MEMD_MODEL_API_KEY` / `_FILE` | unset | Bearer sent to the embedding, rerank and chat backends (authenticated gateways); set only one of the two. An administered store whose Settings point a model URL at another host never receives it: the key only goes to the origins of `MEMD_EMBED_URL`, `MEMD_RERANK_URL` and `MEMD_LLM_URL`. |
| `MEMD_LLM_URL` | unset | OpenAI-compatible chat endpoint (`/v1/chat/completions` is appended to a bare base) for background jobs such as `mem-summarize`, `mem-facts` and `mem-inbox distill`, save's optional conflict check and `ask`'s answers. Never used by recall. Sends `MEMD_MODEL_API_KEY` when set. |
| `MEMD_LLM_MODEL` | unset | Chat model name; required when `MEMD_LLM_URL` is set. |
| `MEMD_LLM_TIMEOUT_S` | `120` | Timeout for one chat completion, clamped to 1..600 seconds. |
| `MEMD_CONFLICT_CHECK` | `facts` | Save's advisory conflict check: `facts` (deterministic fact patterns, no model), `llm` (also asks the chat model about related notes; needs `MEMD_LLM_URL`) or `off`. |
| `MEMD_CONFLICT_DEADLINE_MS` | `1500` | Hard deadline for save's model conflict check, clamped to 100..10000; on expiry the save succeeds and the receipt says the check was skipped. |
| `MEMD_ASK_DEADLINE_MS` | `6000` | Hard deadline for `ask`'s chat-model answer, clamped to 500..30000; on expiry `ask` answers extractively and says so. |

### Review, learning and maintenance

| Variable | Default | Purpose |
| --- | --- | --- |
| `MEMD_SAVE_MODE` | `direct` | `inbox` files an agent token's MCP/HTTP `save` in the review inbox instead of writing it; sessions and local callers save directly. |
| `MEMD_INBOX_TOKEN_REVIEW` | `off` | Whether agent tokens may review the inbox: `off`, `others` (only candidates proposed under another label) or `all`. |
| `MEMD_USAGE_LOG` | `on` | Recall usage log next to each index: `on` (normalised query text kept in that file only), `hash` (query hash only) or `off`. |
| `MEMD_USAGE_RETENTION_DAYS` | `90` | Usage log rows older than this are pruned, 1..3650. |
| `MEMD_USAGE_BOOST` | `off` | `on` applies the bounded per-note weight learned from the usage log in recall fusion. |
| `MEMD_NIGHTLY_STEPS` | `facts,summarize,health` | Steps `mem-nightly` runs: `facts`, `summarize`, `verify`, `forget`, `backup`, `drill`, `health`. |
| `MEMD_SUMMARY_BRANCH` | `memd/summaries` | Review branch `mem-summarize` proposes on. |
| `MEMD_VERIFY_BRANCH` | `memd/verify` | Review branch `mem-verify` proposes on. |
| `MEMD_VERIFY_ALLOW` | unset | `mem-verify` probe targets: addresses, CIDRs and domains (with subdomains). Unset allows only loopback, RFC 1918 and ULA addresses and names resolving only to them. |
| `MEMD_VERIFY_MAX_PROBES` | `50` | Probes per `mem-verify` run, 0..1000; notes beyond it are deferred to the next run. |
| `MEMD_VERIFY_TIMEOUT_S` | `3` | Timeout for each probe step (resolve, connect, HTTP), clamped to 0.5..10 seconds. |
| `MEMD_FORGET_BRANCH` | `memd/forget` | Review branch `mem-forget` proposes archives on. |
| `MEMD_FORGET_DAYS` | `180` | Days since a note's last activity before `mem-forget` may propose it, 30..36500. |
| `MEMD_FORGET_MAX_IMPORTANCE` | `2` | Highest importance `mem-forget` proposes, 1..3. |
| `MEMD_BACKUP_DIR` / `MEMD_BACKUP_KEY_FILE` | unset | Where `mem-backup` writes bundles, and its 0600 key file; both required. |
| `MEMD_BACKUP_KEEP` | `14` | Bundles kept after each `mem-backup create`. |
| `FORGEJO_API`, `FORGEJO_TOKEN`, `MEMD_REPO_PATH` | placeholders in `memd/reflect.py` | Where `python -m memd.reflect` and `mem-summarize --pr` open pull requests: the Forgejo API base, the token (else the contents of `~/.config/forgejo/token`) and the notes repository as `owner/repo`. Set the API base and repository; `python -m memd.reflect` also needs `MEMD_CLONE`. |
| `GITHUB_TOKEN`, `GITHUB_API_URL` | unset | Optional token and alternative server for `mem-import github-prs`. |

### Clients (hooks, bridge, pi)

Onboarding writes `MEMD_URL`, `MEMD_REMOTE`, `MEMD_PROFILE` and `MEMD_TOKEN` to the
private file `~/.config/memd/client.env`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MEMD_REMOTE` / `MEMD_URL` | unset | Server URL for the hooks and bridge / for the pi extension. The Python hooks recall in process when `MEMD_REMOTE` is unset. |
| `MEMD_TOKEN` | unset | The client's bearer token. A token in an explicit `MEMD_ENV_FILE` wins over an inherited one, and the bridge rereads the file after a 401. |
| `MEMD_TOP_N` / `MEMD_MAX_CHARS` / `MEMD_TIMEOUT` | `8` / `14000` / `4` | `memd-recall-hook`: matches, injected characters and recall timeout in seconds. The pi extension reads `MEMD_RECALL_K` (8) and `MEMD_MAX_CHARS`. |
| `MEMD_BRIDGE_TIMEOUT` | `30` | Seconds the stdio bridge waits for one reply. |
| `MEMD_FALLBACK_CHECKOUT` | unset | Read-only checkout `python -m memd.hooks.auto_recall` greps when memd is unreachable. |
| `MEMD_ACTIVITY_HOOK`, `MEMD_ACTIVITY_ERRORS`, `MEMD_ACTIVITY_DEADLINE_MS`, `MEMD_ACTIVITY_TOP_N`, `MEMD_ACTIVITY_MAX_CHARS`, `MEMD_ACTIVITY_MAX_RECALLS` | see [Claude Code hooks](#claude-code-hooks) | The activity hook: install switch, error recall, deadline (1500 ms), notes (3), characters (2000) and recalls per session (40). |
| `MEMD_HANDOFF`, `MEMD_HANDOFF_DEADLINE_MS`, `MEMD_HANDOFF_START_DEADLINE_MS`, `MEMD_HANDOFF_MAX_AGE_DAYS` | see [Claude Code hooks](#claude-code-hooks) | The handoff hook: install switch, deadlines (8000 / 2000 ms) and the age limit of an injected handoff (14 days). |

## License

memd is source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE),
plus an additional permission for internal business use. Personal, research,
non-profit and internal company use is free. Selling memd, offering it as a
service or deploying it for paying clients needs a commercial license. See
[LICENSING.md](LICENSING.md) for the details and how to get one.
