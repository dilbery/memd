# memd-maint — daily self-checking, self-healing maintenance (2026-08-21)

## Why

The failures that actually hurt were all invisible to the existing
`memd-canary` (which only polls `/health`):

- saves lost to client-side schema rejection (canary green throughout),
- apphost running a stale image for days because the deploy pipeline silently
  failed (canary green throughout),
- an agent harness unable to find memd's tools and asking the user instead
  (canary green throughout).

The ask: a daily maintenance run over the whole chain; on failure the LOCAL
model evaluates the evidence and either fixes it from a bounded menu or
escalates via Pushover so the operator and an assistant can fix it together.

## Decisions

- **Fix authority: menu.** The model chooses ONE pre-written remediation (or
  ESCALATE) from evidence. It never composes commands. This keeps the pipeline
  static (a standing rule) while giving the model the judgment role. It
  also bounds prompt-injection from evidence text: the worst a poisoned
  evidence bundle can do is pick a wrong-but-safe remediation or escalate.
- **Scope: full e2e including omp**, because omp's wiring is what actually
  broke and omp self-updates. The omp check no-ops on machines without omp.
- **Probe hygiene:** the daily save probe uses one fixed slug
  (`memd-daily-probe`, importance 1, tag `probe`) so dedup/upsert keeps it to
  a single note forever.
- Runs on **gpuhost** (has the local model, the timers, pushover, and it IS
  the client whose path we care about), daily **05:00** — after the 03:30 /
  03:45 reflect timers so reflect breakage is caught the same morning.

## Components (all under `maint/` in this repo)

### 1. `maint/memd-doctor` (Python, stdlib-only like the bridge)

Deterministic checks, no model involved. Emits human lines to stderr and one
evidence JSON to stdout: `{"ts", "host", "checks": [{"id", "name", "status":
"PASS|FAIL|SKIP", "detail"}], "failed": N}`. Exit 0 iff no FAIL.

| id  | check |
|-----|-------|
| C1  | amber `/health` on :8077 — `ok` true and every sub-check ok |
| C2  | cobalt `/health` on :8078 — same |
| C3  | MCP `tools/list` via https://memd.example.com — `required` is `[]` on both tools and the alias properties are advertised (regression guard on the schema fix) |
| C4  | MCP recall using the alias shape `{"q": ...}` returns a rendered block |
| C5  | MCP save using the alias shape `{slug, content, tags-as-string}` with a nonce in the body — asserts accepted AND `indexed` |
| C6  | MCP recall for the probe slug returns the nonce from C5 (proves the write round-tripped through the index) |
| C7  | `memd-mcp-bridge --selftest` exits 0 (token + real HTTP round trip) |
| C8  | installed bridge is byte-identical to `clients/memd-mcp-bridge` in the local repo clone (drift detector) |
| C9  | deploy drift: `/health.app_commit` == Forgejo `main` HEAD (via the Forgejo HTTP API — no SSH). SKIP with reason if either side unavailable |
| C10 | token env files exist, are readable, and carry a non-empty `MEMD_TOKEN` (memd.env, memd-claude.env, memd-codex.env) |
| C11 | omp e2e: `omp -p` is told to save the probe note with a fresh nonce via its `xd://mcp__memd_recall`/`_save` mounts; SKIP if omp absent |
| C12 | objective verification of C11: re-read the probe via MCP and assert the C11 nonce is present (never trusts the model's self-report) |

C9 needs the server to know its own commit → see §4.

### 2. `maint/remediations/` (shell, hand-written — never model-drafted)

Each idempotent, each prints what it did, each exits non-zero on failure.
SSH goes through an SSH agent socket (fixed path, linger is on).

| id | script | fixes |
|----|--------|-------|
| R1 | `restart-memd` | wedged container(s): `docker compose up -d memd memd-cobalt` on apphost |
| R2 | `redeploy-from-git` | deploy drift / stale image: runs `~/docker/memd/deploy.sh` on apphost (pull → build with commit stamp → up) |
| R3 | `resync-bridge` | bridge drift: copy repo bridge → `~/.local/bin`, re-run selftest |
| R4 | `restart-embed` | embed sub-check failing: restart the embed container |
| R5 | `retry` | transient network: do nothing, let the re-check run |

### 3. `maint/memd-triage` (Python, stdlib-only) — the runner

- All checks pass → one journald line; if the previous run had escalated,
  send a recovery Pushover (state file in `~/.local/state/memd-maint/`).
- Any FAIL → build the triage prompt (evidence JSON + the remediation menu +
  strict output contract), run it through the configured local-model CLI
  (`MEMD_MAINT_MODEL_CMD`; it reuses the model roster and capacity guard, and
  `enable_thinking` is already handled). Parse strictly:
  the reply must be JSON `{"choice": "R1".."R5"|"ESCALATE", "reason": ...}`;
  anything malformed → ESCALATE (fail-safe).
- Apply the chosen remediation, re-run the doctor. Still failing → ONE more
  triage round, never repeating an already-tried remediation. Still failing →
  escalate.
- **Escalate** = write `~/.local/state/memd-maint/report-YYYYMMDD-HHMM.md`
  (evidence + what was tried + the model's diagnosis) and
  `latest-report.md` symlink, then `pushover -t "memd-maint: BORKED" -p 1`
  with the diagnosis and the report path. If the model CLI itself fails, escalate
  with "triage unavailable" — the maintenance must never die silently because
  the triage model is down.
- `--dry-run` prints instead of remediating/pushing (for testing).

### 4. `/health.app_commit` (small server change)

`/health` gains `app_commit`: `$MEMD_GIT_SHA` if set, else resolved from
`/app/.git` if the image carries it, else `"unknown"`. The Dockerfile gains
`ARG GIT_SHA` → `ENV MEMD_GIT_SHA`. A new `~/docker/memd/deploy.sh` on
apphost formalises the deploy (pull → `docker compose build
--build-arg GIT_SHA=$(git -C src rev-parse HEAD)` → up), which also fixes the
"deploy is a remembered incantation" problem permanently.

### 5. systemd user units + installer

`memd-maint.service` (oneshot → `memd-triage`) + `memd-maint.timer`
(`OnCalendar=*-*-* 05:00`, `Persistent=true`). The unit sets
`SSH_AUTH_SOCK=%h/.ssh/agent.sock`. `maint/install-gpuhost.sh`
copies units into `~/.config/systemd/user/` and enables the timer.

## Failure-mode boundaries

- SSH-dependent remediations degrade: if the agent socket is dead the
  remediation fails and the run escalates with that fact in the report.
- The doctor itself never mutates anything; only remediations mutate, only
  one at a time, at most two per run.
- Pushover fires on escalation and on recovery-after-escalation only — no
  daily green noise (same state-transition discipline as memd-canary).
- The apphost `memd-canary` stays: it covers the 15-minute-granularity
  outage case; memd-maint covers the daily deep-path case. They overlap on
  /health only.

## Testing

- Doctor: live run must go all-green on the healthy stack.
- Triage: forced-failure rehearsal with `--dry-run` (bogus URL override), then
  ONE controlled live drill: stop `memd-cobalt`, run the service, verify the
  model picks R1, the container heals, and the re-check goes green.
- The C3 assertions double as the permanent regression guard on the schema fix.
