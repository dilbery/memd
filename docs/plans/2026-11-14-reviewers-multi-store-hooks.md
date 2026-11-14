# Plan: reviewers, multi-store recall in the hooks, proposal feedback

Status: **implemented**, phases 1 to 6. Phase 7 (own pending text in recall) was
not built. What changed during the build, and what is still open, is under
"After the build" at the end. Decisions marked as calls under "Open calls" were
made while writing this and are open to change.

## Why

A team running memd asked three questions. Each answer exposed a gap.

| Question | Today | Gap |
|---|---|---|
| Do the recall hooks search every store an account can reach? | No. The hooks send single-store recalls. Federation (`stores`, `scope: "all"`) exists, but only when an agent calls `recall` or `ask` itself. | No hook setting. |
| With inbox mode on, can an agent recall its own note before it is approved? | No, by design. The save reply is the only trace, and a rejection or its reason never reaches the agent. | The proposer gets no feedback, and nothing stops a reworded duplicate. |
| Is there a reviewer who is not an admin, is limited to some stores, and cannot approve their own saves? | Review is implied by a `write` grant: it cannot be given without write or withheld from a writer. Self-approval is blocked only for agent tokens, by comparing labels, behind one instance-wide switch. Accounts have no review screen. | No review permission, no separation of duties, no place to review. |

## What changes

| Area | Before | After |
|---|---|---|
| Recall hook, pi extension | One store | Unchanged unless `MEMD_RECALL_STORES` is set; then `all` or a list |
| Federated context | No current-facts block; a skipped store shows only in the JSON | The first store's facts block; skipped stores named in the text |
| Review permission | Implied by `write` | Its own per-store capability beside read and write |
| Token review | Instance-wide `MEMD_INBOX_TOKEN_REVIEW` | A per-token flag bounded by its owner's grant; the switch keeps working |
| Who "the proposer" is | The token label | Account and credential ids recorded on the candidate |
| Inbox rules | Checked by each route, passed in as arguments | Checked by the inbox itself |
| Review rules | One instance-wide save mode | A per-store policy: `open`, `agents` or `four-eyes` |
| Review screen | Personal console only; accounts use the API or `mem-inbox` | An Inbox tab in the account web app |
| Proposer feedback | None | A `proposals` tool and endpoint; pending-duplicate lint; opt-in recall of own pending text |
| Audit | Inbox decisions are not audited | `inbox.approve` and `inbox.reject` rows |

Settings all default to today's behaviour. An instance that takes this code and
sets nothing still sees these: the `proposals` tool and endpoint; the
`pending_duplicates` lint field; an Inbox tab for accounts that hold review;
audit rows for decisions; ids on new candidates; and two tokens that share a
label stop counting as one proposer.

## Decisions

**1. The hook setting lives on the client.** Three ways to make hooks federate:

- *Client environment (chosen).* `MEMD_RECALL_STORES` beside `MEMD_PROFILE`. A bare
  recall keeps meaning "my own store" for every caller; the token still bounds
  reach; each machine chooses.
- *A default scope stored on the token.* No client change, but it silently changes
  what a bare `recall` means for every client of that token, MCP calls included,
  and a token's stores cannot be edited after issue.
- *Federate automatically when a token reaches several stores.* Changes behaviour
  on upgrade for anyone who already issued such a token for publishing.

**2. Review is a capability beside read and write.** Alternatives were a third
level above write (`read < write < review`) and a third account role. A level
cannot express a reviewer who may not write, which is the only structural way
to stop someone approving their own saves; a role is not per store. A separate
flag gives four combinations and leaves `Principal.grants` and every existing
`== "write"` check alone. To keep the two apart, changing a candidate's text
needs write: a reviewer without it may change tags and importance only.

**3. Separation of duties is a per-store policy.** A personal store wants the
opposite of a team store: its owner approving their own agent's notes *is* the
workflow. An instance-wide switch cannot serve both, so the rule is a store
setting resolved the way `publish_review` already is.

**4. Pending text stays out of recall unless the operator opts in.** The inbox
exists so an unreviewed note cannot steer an agent. Proposers get a way to ask
about their own proposals; mixing them into recall is a separate, default-off
switch.

**5. Two meanings of "own", both decided by ids.**

- *Reading your own pending text* (handoffs, proposals) means the same
  credential. That is today's handoff rule: `tests/test_handoff.py` asserts that
  a second token of the same user does not see a pending handoff.
- *Separation of duties* means the same account, which is stricter.

Labels are not unique. Today they decide both the self-approval check and who
reads a pending handoff. After this change ids decide for every remote caller.
A candidate with no ids was filed by a local caller (`mem-inbox distill`,
`mem-import`, the in-process handoff hook) or before this change, and is matched
by label, as now.

**6. The inbox enforces its own rules.** Today `inbox.approve` trusts the
`reviewer` and `reviewer_is_token` it is handed; authority is checked only in
`server._inbox_reviewer`, and the personal console calls `approve` directly.
Every rule in Parts B and C would otherwise have to be repeated in each caller.

## Part A: multi-store recall in the hooks

**Setting.** `MEMD_RECALL_STORES` in the hook's process environment, where
`MEMD_PROFILE` is read.

| Value | Request sent |
|---|---|
| unset or empty | Today's request, field for field |
| `all` | `scope: "all"`, no `profile` |
| `amber,team` | `stores: ["amber", "team"]`, no `profile` |

`profile` is dropped because `server._federation` answers 400 when it arrives
together with `stores` or `scope`. No server authorisation changes: each store
is still resolved through `share.authorized_store`.

**Clients.** `clients/memd-recall-hook` (`fetch_recall`) and `clients/pi-memd.ts`:
the two that print the server's rendered `context`. `clients/onboard.sh` gains
`--stores=all` or `--stores=a,b`, one token so it fits the existing flag parser,
and writes the variable beside `MEMD_PROFILE`.

**Server additions**, all in the federated path (`server._federated_recall_route`,
`mcp._federated_recall`, `share.federated_recall`):

1. `deadline_ms` on `/recall` and the `recall` tool, clamped to 100 ms up to
   `share.deadline_ms()` (800 + 900 + 1500 = 3200 ms by default), so it can only
   shorten. A client sends it only in multi-store mode, as its own timeout less
   a margin, so a short `MEMD_TIMEOUT` costs a skipped store, not the whole
   injection.
2. The current-facts block for the first store searched, which under `all` is
   the caller's own. The single-store route appends it
   (`facts.append_current_facts`); the federated route does not, so turning
   federation on would otherwise lose it. Its heading names the store.
3. Skipped stores in the text. `stores_skipped` is in the JSON only. The rendered
   context gains one closing line, inside `max_chars`, naming each skipped store
   and why.
4. The read hint. `read(slug)` without `store` reads the caller's own store, so
   for a federated reply the footer adds: for a note marked `[store]`, pass
   `store="<that store>"`.

**Refusals.** One refused name refuses the whole federated call (403). The hook
then retries once as today's single-store request, within what is left of its
timeout, and prefixes one line saying multi-store recall was refused and which
store is shown. It still never blocks the turn.

**What changes for the reader.** `k` is cut after the stores are fused and the
core index limit is shared, so with several stores fewer of the caller's own
matches and core entries appear. `MEMD_TOP_N` raises the first.

**What the operator does.** A token reaches other stores only through
"Additional stores", fixed when it is issued, so existing tokens are reissued.
Legacy file tokens, registry tokens and bound identities reach one store, so
`all` is their own store.

## Part B: a review permission

**Data** (`control.initialize`). `executescript` commits, and a bare `ALTER`
followed by an `UPDATE` is two transactions, so a failure between them would
leave the column present and the backfill skipped for good. The migration
therefore runs as one explicit `BEGIN IMMEDIATE` transaction with the column
check inside it, which also covers instances that share one administration
database and start together. The existing `tokens.extra` step moves inside it.

- `grants.review INTEGER NOT NULL DEFAULT 0`, then
  `UPDATE grants SET review=1 WHERE permission='write'`, once.
- `tokens.review INTEGER NOT NULL DEFAULT 0`.
- Every `INSERT` names its columns. `admin_api.update_grants` inserts
  positionally today; `control.token_records` selects an explicit column list
  that needs the new one.

| Grant | Recall, read | Save, propose, publish into | Review the inbox | Change a candidate's text |
|---|---|---|---|---|
| read | yes | no | no | no |
| read + review | yes | no | yes | no |
| write | yes | yes | no | no |
| write + review | yes | yes | yes | yes |

Every existing `write` grant becomes write + review, which is what it means today.

**Principal** (`memd/access.py`). New field `reviews: frozenset[str]`:

- administrator: every store;
- member session: the stores whose grant carries review;
- account token: only when the token has `review`, the stores it reaches
  intersected with its owner's current review stores, so removing the owner's
  review removes the token's at the next request;
- legacy file token: none.

**The reviewer** (`memd/inbox.py`). `reviewer_for(store)` returns a `Reviewer`
(label, account, credential, token or not, holds write or not, operator or
not), and `approve` and `reject` take that value in place of the two arguments
they trust today. Self-approval, edit limits and the store's policy are checked
inside them. The personal console, whose session has no principal, builds its
reviewer through `inbox.console_reviewer(store)` after its own guard.

`reviewer_for` allows:

- a session when the store is in `principal.reviews` (today:
  `grants[store] == "write"`);
- an account token when the store is in `principal.reviews`; or, as today, any
  write token when `MEMD_INBOX_TOKEN_REVIEW` is `others` or `all`. Today the
  token branch refuses when the switch is off before it looks at grants; that
  order changes;
- the local CLI: the operator, unchanged.

**The approval write.** `save._save` begins with `authorize(profile, write=True)`,
the only write check on the approval path, so an approval by a review-only
reviewer would be refused inside the core. Add `access.approved_write`, a
context variable holding one store name, which `authorize(..., write=True)`
accepts for that store. Only `inbox.approve` sets it, after the checks above,
inside the copied context its `_write` closure already runs in. The request's
own context never holds it. It has no effect for callers without a principal
(registry tokens, OIDC, the personal console), where `authorize` already
returns early.

**Tokens.** `control.issue_token(..., review=False)` is refused unless the owner
holds review on at least one of the token's stores. A reviewing token cannot
decide a candidate its own credential proposed, and may change only tags and
importance. `MEMD_INBOX_TOKEN_REVIEW` keeps its meaning under the `open` and
`agents` policies, including `all` lifting both limits, and is documented as
the older way.

**Identity** (shared with Part D). `memd/actor.py` gains `caller()`:

| Caller | `account` | `credential` |
|---|---|---|
| account session | user id | `session` |
| account token | user id | token id |
| legacy file token | none | `Principal.id` (`legacy:<digest>`) |
| registry token | its bound identity when it is a personal token, else none | the registry token id |
| bound identity (OIDC, Kasm) | `identity:<store>` | the same |
| local CLI, stdio, in-process hooks | none | none: the label is the identity |

Rotating a registry token mints a new id, so the successor is a different
credential and does not read its predecessor's pending text.

Candidates gain `proposer_account`, `proposer_credential` and `reviewer_account`,
added in `inbox._connect` the way `provenance` was. The three remote `propose`
call sites (`server._core_propose`, `mcp._propose`, `share.publish`) pass
`caller()`. `handoff.latest` and `handoff.replace_pending` match on the
credential, and on the label for a candidate without one.

**API.** `PUT /admin/users/{id}/grants` accepts `"read"`, `"write"` (unchanged
meaning: write + review) or `{"permission": ..., "review": bool}`.
`POST /admin/tokens` accepts `review`. `/ui/me`, `/ui/stores` and
`/admin/overview` report review per store and per token.

**Audit.** `inbox.approve` and `inbox.reject`, target `<store>:<candidate id>`,
written best-effort through a helper that does nothing without `MEMD_ADMIN_DB`,
as `control.audit_read` does. A console that runs on the token registry alone
keeps only the inbox row's own reviewer and time.

**Web.** `memd/web/admin.js` and `index.html`: a "Can review" box per store in
the grants dialog and the users table; a "May review" box when issuing a token,
shown in the token table. `entities.js` and the README say "review access"
where they say "write access". A new **Inbox** tab for anyone holding review on
a store: a store picker, the pending list with lint flags, the candidate with
its preview, approve and reject. `memd/web_assets/inbox.js` (the personal
console) is the model, and the new script joins the asset allowlist in
`server.dashboard_asset`.

## Part C: a review policy per store

`review_policy` in the store's configuration (Settings, **Configure**;
`PUT /admin/stores/{id}`), else `MEMD_<STORE>_REVIEW_POLICY` for a store
configured by environment: the resolution `share.publish_review` uses. Helper
`inbox.review_policy(store)`. `sources.validate_config` copies only the keys it
knows, so the key is added there, and to the payload `admin.js` builds. A change
is audited as `store.review_policy`, target `<store>:<old>-><new>`.

| Policy | Saves that wait in the inbox | Who may decide a candidate | Reviewer edits |
|---|---|---|---|
| `open` (default) | Agent-token saves, when `MEMD_SAVE_MODE=inbox` | Anyone holding review; a token, not its own credential's | Tags and importance; any field for a reviewer who also holds write |
| `agents` | Agent-token saves into this store, whatever `MEMD_SAVE_MODE` says | As `open` | As `open` |
| `four-eyes` | Every save and publish into this store through memd, sessions included | A reviewer with an account that is not the proposer's | Tags and importance, for everyone |

`inbox.save_routes_to_inbox()` takes the store. Its call sites are
`server.save_route`, the MCP `save` and `share.publish`.

Under `four-eyes`:

- only a reviewer with an account decides: a session, or an account token with
  `review`. The `MEMD_INBOX_TOKEN_REVIEW` switch grants nothing on such a store,
  and `all` lifts nothing;
- approve and reject are both refused to the proposer's account;
- a candidate with no ids (filed locally, or before this change) may be decided
  by any such reviewer, but one filed by a
  remote credential that belongs to no account is the operator's to decide;
- a publish waits even when `publish_review` is off;
- the correction path is reject with a reason, which the proposer reads through
  Part D;
- the personal console's edit and retract (`user_memory.change`) are refused;
- administrators are not exempt. They can change the policy, which is audited;
- the local `mem-inbox` CLI is the operator and is exempt;
- Settings warns when fewer than two accounts hold review on the store, since
  nothing could then be approved.

**What `four-eyes` covers.** memd's own write paths: save, publish, approval and
the console's edits. A store fed by a Git remote or an Obsidian vault also takes
in whatever arrives there (`/reindex` with pull, store sync and its scheduler,
vault conflict resolution, repointing the remote). Those are the operator's and
administrator's channels and are not reviewed. Settings says so when the policy
is chosen for such a store.

**memd-doctor.** Its save check requires `saved` to be true, so it fails against
a store whose policy queues its token. It accepts a `queued` receipt as a pass
and says which it saw.

## Part D: feedback for the proposer

1. **Own proposals.** `GET /proposals`, `GET /proposals/{id}` and a `proposals`
   tool, advertised wherever `propose` is: the candidates the calling credential
   filed in one store, with status, title, dates, reviewer label, rejection
   reason, and the slug and revision once approved; full text by id. Needs read
   access to the store. `tests/test_mcp_sdk_client.py` pins the tool list and
   changes with it.
2. **Pending-duplicate lint.** `inbox.propose` compares the fact with the
   pending candidates using `_similar`, the check `mem-inbox distill` already
   runs, through a new helper that also returns proposer ids (`pending_texts`
   keeps its three-value shape for its other callers). It records
   `lint.pending_duplicates`. Reviewers see ids and titles. The proposer's
   receipt names its own matches and says nothing about anyone else's, not even
   a count, which would let a proposer test guesses about what others have
   pending. The same filter applies to the existing exact-duplicate reply, which today hands
   the first proposer's stored lint to whoever proposes the same text.
3. **Own pending text in recall, opt-in.** `MEMD_INBOX_RECALL_OWN=1`, default off.
   Recall of a single store, or the first store of a federated one, appends a
   block headed "Proposed by you, awaiting review": at most three of the calling
   credential's pending candidates that share keywords with the query, 600
   characters each, added the way the current-facts block is (only when it fits
   `max_chars`). They are never in `notes`, never usage-logged, never another
   credential's, never rejected ones.

## Properties to test

Driven through the REST surface and the MCP tool surface, as
`tests/test_multitenant_cross_tenant.py` does.

1. No hook setting widens reach: a store outside the token's grants is refused
   whatever `MEMD_RECALL_STORES` says.
2. Write does not imply review, and review does not imply write: a review-only
   session gets 403 from `/save`, `/propose` and a publish into the store, and
   cannot change a candidate's title, body, slug or `supersedes`.
3. The approval write is authorised for one store, only inside `inbox.approve`,
   after the inbox's own check, and is gone when it returns or raises. Calling
   `approve` without a reviewer the inbox built is refused.
4. A token's review never exceeds its owner's current review grant.
5. Under `four-eyes`, nothing reaches the store through save, publish or a
   console edit without a decision by a different account, and no reviewer
   changes its text.
6. Two tokens with one label are different proposers. Two tokens of one account
   are one proposer under `four-eyes`, and two readers of pending text.
7. Pending text is readable only by the credential that proposed it and the
   store's reviewers.
8. With `MEMD_RECALL_STORES` unset the hook's request is today's, field for field.
9. The migration is atomic: a failure part-way leaves no column without its
   backfill, and a second run changes nothing.

## Phases

Each phase is one change set that passes the full suite and can be deployed
alone.

| # | Phase | Main files | Tests | Done when |
|---|---|---|---|---|
| 1 | Hooks (Part A) | `clients/memd-recall-hook`, `clients/pi-memd.ts`, `clients/onboard.sh`, `memd/share.py`, `memd/server.py`, `memd/mcp.py` | new `tests/test_recall_hook_stores.py`; the `test_render.py` drift guard; `test_share.py`; `test_onboard_sh_runs_under_sh.py`; `test_clients_ops_improvements.py` | Properties 1 and 8 hold; a hook set to `all` shows `[store]` headings, the facts block and any skipped store |
| 2 | Identity, audit, inbox-owned rules | `memd/actor.py`, `memd/inbox.py`, `memd/handoff.py`, `memd/server.py`, `memd/mcp.py`, `memd/share.py`, `memd/user_web.py`, `memd/control.py` | `test_inbox.py`, `test_user_web_inbox.py`, `test_handoff.py` (unchanged and passing), `test_backup.py` (new columns survive a restore) | Candidates carry ids; decisions are audited; properties 3 (second half) and 6 (first half) hold |
| 3 | Review capability (Part B) | `memd/control.py`, `memd/access.py`, `memd/inbox.py`, `memd/admin_api.py`, `memd/web/admin.js`, `memd/web/index.html`, `memd/web/entities.js` | `test_administration.py` (migration, idempotence, shared database), `test_inbox.py`, `test_dashboard.py`; browser check | Properties 2, 3, 4, 9 hold; an upgraded instance behaves as before |
| 4 | Inbox tab | `memd/web/index.html`, new `memd/web/inbox.js`, `memd/web/app.js`, `memd/server.py` (asset allowlist) | `test_dashboard.py`; browser check | A review-only member can approve and reject from the browser |
| 5 | Proposals and duplicate lint (Part D 1, 2) | `memd/inbox.py`, `memd/server.py`, `memd/mcp.py` | `test_inbox.py`, `test_mcp_sdk_client.py` | Property 7 holds; a rejected proposal's reason reaches its proposer |
| 6 | Review policy (Part C) | `memd/inbox.py`, `memd/share.py`, `memd/server.py`, `memd/mcp.py`, `memd/user_web.py`, `memd/sources.py`, `memd/admin_api.py`, `memd/control.py` (`public_store`), `memd/web/admin.js`, `memd/web/index.html`, `maint/memd-doctor` | `test_inbox.py`, `test_share.py`, `test_user_web_inbox.py`, `test_administration.py` | Properties 5, 6 hold; `agents` queues in one store and not its neighbour |
| 7 | Own pending in recall (Part D 3), optional | `memd/inbox.py`, `memd/server.py`, `memd/mcp.py` | `test_inbox.py`, `test_render.py` | Off by default; on, only the proposing credential sees the block |

Phase 1 depends on nothing and closes the first question by itself. Phase 5 comes
before phase 6 because `four-eyes` relies on rejection reasons reaching the
proposer. Documentation moves with each phase: the README's Claude Code hooks,
Review inbox and Team sharing sections and its settings table;
`docs/ADMINISTRATION.md` (a new "Reviewers and review policy" section);
`docs/ADMIN-WEB.md`; `docs/USER-WEB.md`.

## Compatibility

- Grants: the migration preserves behaviour. The legacy string form of the
  grants API keeps its meaning.
- `MEMD_SAVE_MODE`, `MEMD_INBOX_TOKEN_REVIEW` and `publish_review` keep their
  meaning under the default policy.
- A new hook against an older server: unknown fields are ignored, so `deadline_ms`
  is harmless; a server from before team sharing answers with the token's own
  store.
- An older build cannot edit grants against a migrated database, because its
  positional `INSERT` expects three columns. Take the usual backup before the
  phase 3 deploy; the path is forward.
- Candidates filed before phase 2 have no ids and are matched by label.

## What this does not do

- **The activity hook and the in-process `auto_recall` stay single-store.** Both
  render notes themselves, without `[store]`. The activity hook also works to a
  1.5 s budget with a 60 s backoff, and keys its dedupe on the slug alone.
- **No server-side default recall scope**, per token or per account (decision 1).
- **A token's additional stores stay fixed at issue.** Using multi-store hooks
  means issuing new tokens.
- **No `review` operation for registry tokens** (`MEMD_CONTROL_DB`). They review
  only through `MEMD_INBOX_TOKEN_REVIEW`, as today.
- **No review of what arrives through a store's Git remote or vault** (Part C).
- **No reviewer notifications.** The pending count stays on Health and in
  `memd_inbox_pending`.
- **No change to the branch-and-merge reviews** of `mem-forget`, `mem-verify`
  and `mem-summarize`.
- **No withdrawing of a proposal by its proposer.**
- **`four-eyes` does not defend against the operator**, who has the disk and the
  Git remote, or an administrator who changes the policy. It makes the second
  visible in the audit log.

## Open calls

1. Part D item 3 is optional. Leave phase 7 out to keep the inbox a hard gate.
2. A reviewer without write cannot change a candidate's text under any policy,
   and under `four-eyes` nobody can. The alternative keeps free edits and
   records the original, as today.
3. Reading your own pending text is per credential, not per account. Per account
   would let a user's second machine see the first one's proposals, and would
   change today's handoff rule.
4. Under `four-eyes` only reviewers with an account decide, and administrators
   are not exempt.
5. The grants API's `"write"` string keeps meaning write + review, so existing
   scripts do not silently create writers who cannot review.
6. Multi-store recall covers the recall hook and the pi extension only, and the
   hooks ship first and alone.

## After the build

Phases 1 to 6 are built. Each work item was implemented and then reviewed by
someone who had not written it; the reviews changed the result, and the changes
that depart from the plan above are listed here.

**Changed from the plan**

- *A proposer learns nothing about other proposers' pending candidates.* The
  plan gave a count of them. A count is an oracle: anyone who can propose into
  a shared store could test guesses about what others have waiting. The reply
  names the proposer's own matches only. Reviewers still see every match.
- *An identical proposal reveals nothing of the first.* The reply used to carry
  the first proposer's stored lint (the slug they chose, the note they meant to
  supersede). It now carries the lint of the caller's own fact. What remains is
  what was already true: the caller learns that an identical candidate is
  waiting, and its id.
- *The token-review switch needs a token that may save.* With
  `MEMD_INBOX_TOKEN_REVIEW` on, a registry token without the save operation was
  treated as a reviewer on the entity page. That predates this work and is
  closed.
- *Under `four-eyes`, a remote credential with no account is the operator's.* A
  legacy file token, or a registry token that is no one's personal token, has
  no account to compare, and whoever holds it may hold a reviewing account too.
  Its saves still wait in the inbox, but only the operator decides them. A
  candidate with no ids at all, filed on the server, is still any reviewer's.
- *An unknown policy value means `four-eyes`.* A misspelt value in the
  environment must not quietly switch review off.
- *The operator is exempt from all of `four-eyes`*, the edit limit included. It
  can save directly in any case.
- *The facts block appears only when the first store was searched.* If that
  store was skipped, no other store's facts stand in for it.
- *The hook's refusal notice appears only with context.* When the single-store
  retry returns nothing, nothing is injected, as for any empty recall.
- *`onboard.sh --stores` refuses `all` inside a list.* The server would refuse
  it on every prompt.
- *`memd-doctor` skips, not fails, where its probe was queued*: the read-back
  and the agent-client checks have nothing to read.

**Known limits, not fixed**

1. *The switch outranks a withheld review.* With `MEMD_INBOX_TOKEN_REVIEW` set
   to `others` or `all`, a write token of a member whose grant is write without
   review can still review, while that member's session cannot.
2. *Tag edits can move a handoff.* A reviewer without write may change tags, and
   a handoff's repository is carried in a tag.
3. *The entity page counts pending candidates for any reader.* It shows how many
   pending candidates mention an entity to anyone who can read the store. That
   predates this work and is the same kind of count removed from the proposer's
   reply.
4. *A second proposer of identical text cannot follow it.* One proposer is
   recorded per candidate, so the second never sees the outcome or the reason.
5. *The personal console lags.* On a `four-eyes` store it still shows Edit and
   Retract (the request is refused with the reason), and its inbox does not
   show pending duplicates.
6. *Every proposal reads all pending text.* It streams it now, so memory is
   bounded, but the time grows with the size of the inbox.
7. *`docs/ADMIN-WEB.md` and `docs/USER-WEB.md` do not describe the policy.*
