# memd — operator registration

Host names, addresses, user names and paths in this guide are examples; adjust
them for your machines. All snippets assume: the 3.13 venv lives at `/home/svcuser/projects/memd/.venv`,
`memd.service` listens on `127.0.0.1:8077`, and `MEMD_TOKEN` is set (see below).

## 1. Set the write token (used by /save, the agent providers, the opencode sidecar)

```bash
mkdir -p /home/svcuser/.config/memd
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /home/svcuser/.config/memd/token
chmod 600 /home/svcuser/.config/memd/token
```
Export `MEMD_TOKEN` for `memd.service` (via its unit `Environment=` / drop-in) and
for any agent that calls `/save`.

## 2. Claude Code MCP server (recall + save as tools)

Add to `~/.claude.json` under the project key for this box. The MCP server runs
in-process against the local index (no HTTP hop):

```json
{
  "projects": {
    "/home/svcuser": {
      "mcpServers": {
        "memd": {
          "command": "/home/svcuser/projects/memd/.venv/bin/mem-mcp",
          "args": [],
          "env": {
            "MEMD_PROFILE": "amber"
          }
        }
      }
    }
  }
}
```

## 3. First-turn auto-recall hook (UserPromptSubmit)

Add to `~/.claude/settings.json`. The hook injects a small, additive recall set
at turn start, obeying the recall deadlines and degrading to grep:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/home/svcuser/projects/memd/.venv/bin/python -m memd.hooks.auto_recall",
            "timeout": 3
          }
        ]
      }
    ]
  }
}
```
The hook reads `MEMD_FALLBACK_CHECKOUT` (a SEPARATE read-only checkout, e.g.
`~/.local/share/memd/notes`) for its degraded grep path — never `MEMD_CLONE`.

## 4. Hermes backend swap

Keep `memory.provider: forgejo-memory` in Hermes config, and add
`~/.hermes/forgejo_memory.json`:

```json
{
  "backend": "memd",
  "url": "http://127.0.0.1:8077",
  "token": "PASTE_MEMD_TOKEN",
  "fallback_checkout": "/home/svcuser/.local/share/memd/notes"
}
```
Wire `memd.integrations.hermes_memd_provider.MemdHttpProvider` as the provider's
backend when `backend == "memd"`; otherwise the stdlib forgejo provider stays.

## 5. opencode sync-sidecar

Drop the sidecar alongside the existing sync plugin (it EXTENDS, does not fork):

```bash
cp /home/svcuser/projects/memd/memd/integrations/opencode_memd_sidecar.js \
   /home/svcuser/.config/opencode/plugins/memd-sync-sidecar.js
```
Set `MEMD_URL=http://127.0.0.1:8077` and `MEMD_TOKEN` in opencode's environment.
On `session.idle` it POSTs newly-written notes to `/save`; the existing
`memory-forgejo-sync.js` still handles git pull/push.

## 6. Cobalt hard isolation

Cobalt gets a SEPARATE Forgejo repo, clone, DB, and credential. Set, per
profile, the env the registry reads (`memd/profiles.py`):

```json
{
  "MEMD_AMBER_REPO": "git@10.10.1.10:svcuser/amber-memory.git",
  "MEMD_AMBER_CLONE": "/home/svcuser/.cache/memd/amber/clone",
  "MEMD_AMBER_DB": "/home/svcuser/.cache/memd/amber/memd.db",
  "MEMD_AMBER_CRED": "/home/svcuser/.config/memd/amber_token",
  "MEMD_COBALT_REPO": "git@10.10.1.10:svcuser/cobalt-memory.git",
  "MEMD_COBALT_CLONE": "/home/svcuser/.cache/memd/cobalt/clone",
  "MEMD_COBALT_DB": "/home/svcuser/.cache/memd/cobalt/memd.db",
  "MEMD_COBALT_CRED": "/home/svcuser/.config/memd/cobalt_token"
}
```
The Amber memd instance is launched with ONLY Amber's paths; it is physically
unable to read Cobalt's clone/DB (enforced by `assert_no_cross_profile`).

## 7. Firewalld scope

```bash
/home/svcuser/projects/memd/systemd/firewalld-memd.sh
```
Scopes tcp/8077 to `10.10.1.0/24`. `/recall` + `/health` are open on the LAN;
`/save`, `/reflect`, `/admin` additionally require the bearer `MEMD_TOKEN`.

## 8. Deploy & verify (operator runbook — paste later, NOT applied by the build)

The build produces repo files + hermetic tests only. The steps below install
the SYSTEM guard + USER reflect timer and push the branch; run them by hand on
`gpuhost` (confirm `hostname` first).

Install the guard loop + SYSTEM unit:
```bash
sudo install -m 0755 /home/svcuser/projects/memd/systemd/memd-guard-loop /usr/local/bin/memd-guard-loop
sudo install -m 0644 /home/svcuser/projects/memd/systemd/memd-guard.service /etc/systemd/system/memd-guard.service
sudo systemctl daemon-reload
sudo systemctl enable --now memd-guard.service
```

Install the propose-only nightly reflect (USER timer). `FORGEJO_TOKEN` is a
secret, so place it in a 0600 drop-in, NOT in the repo-committed unit:
```bash
mkdir -p ~/.config/systemd/user
install -m 0644 /home/svcuser/projects/memd/systemd/memd-reflect.service ~/.config/systemd/user/memd-reflect.service
install -m 0644 /home/svcuser/projects/memd/systemd/memd-reflect.timer   ~/.config/systemd/user/memd-reflect.timer
mkdir -p ~/.config/systemd/user/memd-reflect.service.d
umask 077; printf '[Service]\nEnvironment=FORGEJO_TOKEN=...\n' > ~/.config/systemd/user/memd-reflect.service.d/secret.conf
systemctl --user daemon-reload
systemctl --user enable --now memd-reflect.timer
```

Health checks (expect `active` then `active`):
```bash
systemctl is-active memd-guard.service
systemctl --user is-active memd-reflect.timer
```
If `memd.service` (Plan 1) is not yet deployed, `memd-guard.service` is still
`active` — it polls `/health` and logs DEGRADE; that is correct.

Push the branch to Forgejo (repos go to Forgejo, never GitHub):
```bash
cd /home/svcuser/projects/memd
git remote get-url origin || git remote add origin ssh://git@10.10.1.10/svcuser/memd.git
git push -u origin feat/foundations
```

Final one-line health record:
```bash
echo "memd integration+ops: $(cd /home/svcuser/projects/memd && .venv/bin/python -m pytest -q 2>&1 | tail -1); guard=$(systemctl is-active memd-guard.service); reflect-timer=$(systemctl --user is-active memd-reflect.timer)"
```
Expect a single line ending `... passed; guard=active; reflect-timer=active`.
