#!/usr/bin/env bash

# Re-exec under bash when we were started by a POSIX shell.
#
# The documented one-liner -- here, in `memd`'s index page, and in the apphost
# design doc -- is `sh onboard.sh <token>`. On Debian/Ubuntu /bin/sh is dash,
# and this script is bash: arrays, ${var:0:n} substrings, [[ =~ ]] with
# BASH_REMATCH, $'\n'. Under dash it died on the first array before doing
# anything at all:
#
#     onboard.sh: 54: Syntax error: "(" unexpected
#
# Found while onboarding a fresh Ubuntu VM. Re-execing is preferred
# over rewriting the script as POSIX, or over changing the docs to say `bash`:
# it makes the command everyone already copy-pastes correct on every distro.
#
# This block must come BEFORE any bash-only syntax, and must itself be POSIX.
# `[ -r "$0" ]` keeps a piped `curl ... | sh` from exec'ing an interactive
# bash -- that form cannot work here anyway, so fail it with a clear message.
if [ -z "${BASH_VERSION:-}" ]; then
    if [ -r "$0" ] && command -v bash >/dev/null 2>&1; then
        exec bash "$0" "$@"
    fi
    echo "ERROR: onboard.sh needs bash. Re-run it as:  bash onboard.sh <token>" >&2
    exit 1
fi

set -euo pipefail
umask 077

# onboard.sh — one-command memd onboarding for a machine
# Serves as the single entry point for setting up local agent integrations from a memd server.

usage() {
    echo "Usage: bash onboard.sh [--activity-hook] [--handoff] [--token-file PATH] [token] [server-url]" >&2
    echo "" >&2
    echo "  Leave the token out to be prompted for it, or use --token-file PATH or an" >&2
    echo "  exported MEMD_TOKEN: a token on the command line is kept in shell history" >&2
    echo "  and shown to other users in the process list." >&2
    echo "" >&2
    echo "  --activity-hook  also install the Claude Code PostToolUse hook that recalls" >&2
    echo "                   memory about the host, service or file a tool call targets" >&2
    echo "                   (same as MEMD_ACTIVITY_HOOK=1)" >&2
    echo "  --handoff        also install the Claude Code SessionEnd/SessionStart hook that" >&2
    echo "                   writes where a session left off in a repository and shows it" >&2
    echo "                   to the next session there (same as MEMD_HANDOFF=1)" >&2
    echo "" >&2
    echo "Get a token via: ssh apphost '~/docker/memd/memd-token issue <profile> <label>'" >&2
    exit 1
}

# Opt-in flags may appear anywhere; strip them so the positional <token> and
# [server-url] keep their places. The activity and handoff hooks are off unless
# asked for.
activity_hook=0
case "${MEMD_ACTIVITY_HOOK:-}" in
    1|true|yes|on) activity_hook=1 ;;
esac
handoff_hook=0
case "${MEMD_HANDOFF:-}" in
    1|true|yes|on) handoff_hook=1 ;;
esac
token_file=
positional=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --activity-hook) activity_hook=1 ;;
        --handoff) handoff_hook=1 ;;
        --token-file)
            [ -n "${2:-}" ] || { echo "ERROR: --token-file needs a PATH" >&2; usage; }
            token_file="$2"
            shift ;;
        --token-file=*)
            token_file="${1#--token-file=}"
            [ -n "$token_file" ] || { echo "ERROR: --token-file needs a PATH" >&2; usage; } ;;
        *) positional+=("$1") ;;
    esac
    shift
done
set -- ${positional[@]+"${positional[@]}"}

# Where the token comes from, most explicit first: --token-file, a positional
# token (anything that is not a URL), MEMD_TOKEN, then an interactive prompt.
# A token on the command line is still accepted, with a warning, because it
# lands in shell history and in the process list.
if [ -n "$token_file" ]; then
    TOKEN=$(head -n 1 -- "$token_file" 2>/dev/null | tr -d '\r')
    [ -n "$TOKEN" ] || { echo "ERROR: cannot read a token from $token_file" >&2; exit 1; }
    SERVER="${1:-${MEMD_SERVER:-https://memd.example.com}}"
elif [ "$#" -ge 1 ] && [[ "$1" != *://* ]]; then
    echo "WARNING: a token on the command line is saved in shell history and visible to other users; leave it out to be prompted, or use --token-file or an exported MEMD_TOKEN." >&2
    TOKEN="$1"
    SERVER="${2:-https://memd.example.com}"
elif [ -n "${MEMD_TOKEN:-}" ]; then
    TOKEN="$MEMD_TOKEN"
    SERVER="${1:-${MEMD_SERVER:-https://memd.example.com}}"
else
    # Running with no token is the common case: you are on a fresh machine and
    # have not minted one yet. Rather than fail, tell the user exactly how to get
    # one and prompt for it, so the only thing they had to remember was this
    # server's URL.
    if [ -t 0 ]; then
        echo "No token given. On apphost (10.10.1.10), run:" >&2
        echo "" >&2
        # Print THIS machine's hostname, not a literal $(hostname): the command is
        # pasted on apphost, where it would otherwise expand to apphost's own name.
        _label=$(hostname 2>/dev/null || echo mymachine)
        echo "    ~/docker/memd/memd-token issue amber ${_label}" >&2
        echo "" >&2
        printf "Then paste the token here (or Ctrl-C to abort): " >&2
        read -rs TOKEN
        echo >&2
        [ -n "$TOKEN" ] || { echo "No token entered." >&2; exit 1; }
        SERVER="${1:-${MEMD_SERVER:-https://memd.example.com}}"
    else
        usage
    fi
fi

# Never send the token in clear text except to this machine.
if [[ "$SERVER" == http://* ]] && [ "${MEMD_ALLOW_INSECURE_HTTP:-}" != "1" ]; then
    _host="${SERVER#http://}"
    _host="${_host%%/*}"
    _host="${_host##*@}"
    if [[ "$_host" == \[* ]]; then
        _host="${_host%%]*}]"
    else
        _host="${_host%%:*}"
    fi
    case "$_host" in
        localhost|127.0.0.1|"[::1]") ;;
        *)
            echo "ERROR: refusing to send the token over plain HTTP to $SERVER; use https:// (or set MEMD_ALLOW_INSECURE_HTTP=1 on a trusted network)" >&2
            exit 1 ;;
    esac
fi

short_token() {
    # Never print the full token — truncate to first 8 chars + ellipsis
    printf '%s…' "${TOKEN:0:8}"
}

step_failed=0

fail_step() {
    # Non-fatal per-step failure handler; records global failure and prints a clear one-liner
    echo "ERROR: $1"
    step_failed=1
}

download_client() {
    # Do not truncate a working installed client when a download fails.
    local source_url="$1" destination="$2" temporary
    temporary=$(mktemp "${destination}.memd-download.XXXXXX")
    if curl -fsS --max-time 15 "$source_url" -o "$temporary"; then
        mv -f "$temporary" "$destination"
    else
        rm -f "$temporary"
        return 1
    fi
}

### Step 0: Preflight ###

# Check for required binaries
missing=()
command -v curl >/dev/null 2>&1 || missing+=(curl)
command -v python3 >/dev/null 2>&1 || missing+=(python3)
if [ "${#missing[@]}" -gt 0 ]; then
    echo "ERROR: Missing required tools: ${missing[*]}" >&2
    exit 1
fi

# Health check
if ! curl -fsS --max-time 5 -o /dev/null "$SERVER/health"; then
    echo "ERROR: memd not reachable at $SERVER" >&2
    exit 1
fi

# Validate token by calling tools/list — do this before writing anything
# The Authorization header goes through curl's config on stdin (-K -), so the
# token never appears in curl's argv.
_curl_token="${TOKEN//\\/\\\\}"
_curl_token="${_curl_token//\"/\\\"}"
auth_response=$(curl -fsS -w '\n%{http_code}' \
    --max-time 5 \
    -K - \
    "$SERVER" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
    <<<"header = \"Authorization: Bearer $_curl_token\"" 2>/dev/null || true)
unset _curl_token

http_code="${auth_response##*$'\n'}"
case "$http_code" in
    200) ;;
    401)
        echo "ERROR: Bad memd token (401 Unauthorized)" >&2
        exit 1
        ;;
    *)
        echo "ERROR: Unexpected response validating token (HTTP $http_code)" >&2
        exit 1
        ;;
esac

# HTTP 200 alone is not a successful MCP response (errors also use 200).
if ! printf '%s' "${auth_response%$'\n'*}" | python3 -c '
import json, sys
text = sys.stdin.read()
try:
    try:
        reply = json.loads(text)
    except ValueError:
        reply = next(json.loads(line[5:].strip()) for line in text.splitlines()
                     if line.startswith("data:"))
    tools = {t["name"] for t in reply["result"]["tools"]}
    assert reply.get("jsonrpc") == "2.0" and reply.get("id") == 1
    assert "error" not in reply and {"recall", "save"}.issubset(tools)
except (ValueError, KeyError, TypeError, AssertionError, StopIteration):
    sys.exit(1)
'; then
    echo "ERROR: memd token probe did not return valid recall/save tool discovery." >&2
    exit 1
fi

# Derive profile from token shape: mem_<profile>_<random>
profile="amber"
if [[ "$TOKEN" =~ ^mem_([^_]+)_.+$ ]]; then
    profile="${BASH_REMATCH[1]}"
else
    echo "WARNING: Token did not match expected format (mem_<profile>_<random>); defaulting profile to 'amber'." >&2
fi

echo "Onboarding with profile '$profile' token $(short_token)"

export SERVER PROFILE="$profile" TOKEN
env_file="$HOME/.config/memd/client.env"
mkdir -p "$(dirname "$env_file")"
python3 - "$env_file" <<'PYEOF'
import os, shlex, sys, tempfile
path = sys.argv[1]
fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w") as output:
    for key, value in {"MEMD_URL": os.environ["SERVER"],
                       "MEMD_REMOTE": os.environ["SERVER"],
                       "MEMD_PROFILE": os.environ["PROFILE"],
                       "MEMD_TOKEN": os.environ["TOKEN"]}.items():
        output.write("export " + key + "=" + shlex.quote(value) + "\n")
os.replace(temporary, path)
PYEOF
export MEMD_CLIENT_ENV="$env_file"
codex_config="${CODEX_HOME:-$HOME/.codex}/config.toml"

### Step 1: Claude Code ###
if [ -d "$HOME/.claude" ]; then
    echo "Configuring Claude Code..."

    hooks_dir="$HOME/.claude/hooks"
    mkdir -p "$hooks_dir"

    # Download hook script
    if ! download_client "$SERVER/clients/memd-recall-hook" "$hooks_dir/memd-recall-hook"; then
        fail_step "Failed to download memd-recall-hook"
    else
        chmod 755 "$hooks_dir/memd-recall-hook"
        echo "Installed Claude hook: $hooks_dir/memd-recall-hook"
    fi

    # Opt-in PostToolUse hook: recall about the host/service/file a tool targets.
    activity_install=0
    if [ "$activity_hook" -eq 1 ]; then
        if ! download_client "$SERVER/clients/memd-activity-hook" "$hooks_dir/memd-activity-hook"; then
            fail_step "Failed to download memd-activity-hook"
        else
            chmod 755 "$hooks_dir/memd-activity-hook"
            activity_install=1
            echo "Installed Claude activity hook: $hooks_dir/memd-activity-hook"
        fi
    fi

    # Opt-in SessionEnd/SessionStart hook: session handoff notes per repository.
    handoff_install=0
    if [ "$handoff_hook" -eq 1 ]; then
        if ! download_client "$SERVER/clients/memd-handoff-hook" "$hooks_dir/memd-handoff-hook"; then
            fail_step "Failed to download memd-handoff-hook"
        else
            chmod 755 "$hooks_dir/memd-handoff-hook"
            handoff_install=1
            echo "Installed Claude handoff hook: $hooks_dir/memd-handoff-hook"
        fi
    fi

    # Merge settings.json using Python to preserve existing content and avoid fragile sed edits
    settings_file="$HOME/.claude/settings.json"
    if [ ! -f "$settings_file" ]; then
        echo '{}' > "$settings_file"
    fi
    # Keep the previous settings: restored if the merge fails, reported otherwise.
    settings_backup=$(mktemp "${settings_file}.memd-backup.XXXXXX")
    cp -p "$settings_file" "$settings_backup"

    # Exported BEFORE the heredoc runs — the python block reads these from the
    # environment, and exporting afterwards wrote an empty hook command.
    export SERVER PROFILE="$profile" TOKEN MEMD_ACTIVITY_INSTALL="$activity_install" \
        MEMD_HANDOFF_INSTALL="$handoff_install"

    # The failure handler must sit on the SAME line as the heredoc redirect.
    python3 - <<'PYEOF' || { fail_step "Failed to merge settings.json"; cp -p "$settings_backup" "$settings_file"; echo "Restored Claude settings from $settings_backup"; }
import json
import os
import shlex
import sys

f = os.path.expanduser("~/.claude/settings.json")
try:
    with open(f, "r") as fh:
        data = json.load(fh)
except Exception as e:
    print(f"ERROR: settings.json exists but is invalid JSON ({e}); skipping Claude hook merge.")
    sys.exit(1)

# Ensure nested structure exists
hooks = data.setdefault("hooks", {})
user_submit = hooks.setdefault("UserPromptSubmit", [])

entry_cmd = (
    "MEMD_REMOTE={server} "
    "MEMD_PROFILE={profile} "
    "MEMD_ENV_FILE={env_file} "
    "{home}/.claude/hooks/memd-recall-hook"
).format(
    server=shlex.quote(os.environ.get("SERVER", "")),
    profile=shlex.quote(os.environ.get("PROFILE", "")),
    env_file=shlex.quote(os.environ["MEMD_CLIENT_ENV"]),
    home=shlex.quote(os.environ["HOME"]),
)

# Claude Code expects UserPromptSubmit to be a list of GROUPS:
#   [{"matcher": "*", "hooks": [{"type": "command", "command": ...}]}]
# Appending a bare {"command": ...} at the group level produces an entry the
# harness silently ignores, so build the nested shape.
new_hook = {
    "type": "command",
    "command": entry_cmd,
    "timeout": 15,
    "statusMessage": "memd recall...",
}

# Idempotent: replace an existing memd hook in place rather than adding a second.
found = False
for group in user_submit:
    if not isinstance(group, dict):
        continue
    for i, h in enumerate(group.get("hooks", []) or []):
        if isinstance(h, dict) and "memd-recall-hook" in (h.get("command") or ""):
            group["hooks"][i] = new_hook
            found = True
            break
    if found:
        break

# Drop any malformed bare entries a previous version of this script appended.
user_submit[:] = [
    g for g in user_submit
    if not (isinstance(g, dict) and "command" in g and "hooks" not in g)
]

if not found:
    user_submit.append({"matcher": "*", "hooks": [new_hook]})

hooks["UserPromptSubmit"] = user_submit

# Opt-in activity hook (--activity-hook): PostToolUse on the tools that name a
# target. Replaced in place when already present; never removed here.
if os.environ.get("MEMD_ACTIVITY_INSTALL") == "1":
    post_tool = hooks.setdefault("PostToolUse", [])
    if not isinstance(post_tool, list):
        print("ERROR: settings.json hooks.PostToolUse is not a list; not modifying.")
        sys.exit(1)
    activity_matcher = "Bash|Edit|Write|MultiEdit|Read|NotebookEdit"
    activity_hook = {
        "type": "command",
        "command": entry_cmd.replace("/memd-recall-hook", "/memd-activity-hook"),
        # The hook enforces its own recall deadline (1.5 s); this is a backstop.
        "timeout": 5,
    }
    placed = False
    for group in post_tool:
        if not isinstance(group, dict):
            continue
        for i, h in enumerate(group.get("hooks", []) or []):
            if isinstance(h, dict) and "memd-activity-hook" in (h.get("command") or ""):
                group["hooks"][i] = activity_hook
                group["matcher"] = activity_matcher
                placed = True
                break
        if placed:
            break
    if not placed:
        post_tool.append({"matcher": activity_matcher, "hooks": [activity_hook]})
    # A failed Bash call reaches PostToolUseFailure, not PostToolUse: the same hook
    # recalls "seen this error before?" there (MEMD_ACTIVITY_ERRORS=0 turns it off).
    post_fail = hooks.setdefault("PostToolUseFailure", [])
    if not isinstance(post_fail, list):
        print("ERROR: settings.json hooks.PostToolUseFailure is not a list; not modifying.")
        sys.exit(1)
    placed = False
    for group in post_fail:
        if not isinstance(group, dict):
            continue
        for i, h in enumerate(group.get("hooks", []) or []):
            if isinstance(h, dict) and "memd-activity-hook" in (h.get("command") or ""):
                group["hooks"][i] = dict(activity_hook)
                group["matcher"] = "Bash"
                placed = True
                break
        if placed:
            break
    if not placed:
        post_fail.append({"matcher": "Bash", "hooks": [dict(activity_hook)]})

# Opt-in handoff hook (--handoff): SessionEnd writes where the session left off in
# its repository; SessionStart shows the newest one to the next session there.
# Replaced in place when already present; never removed here.
if os.environ.get("MEMD_HANDOFF_INSTALL") == "1":
    handoff_cmd = entry_cmd.replace("/memd-recall-hook", "/memd-handoff-hook")
    # The hook enforces its own deadlines (8 s at the end, 2 s at the start);
    # these are backstops.
    for event, matcher, timeout in (("SessionEnd", None, 15), ("SessionStart", "startup|resume", 10)):
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            print(f"ERROR: settings.json hooks.{event} is not a list; not modifying.")
            sys.exit(1)
        handoff_hook = {"type": "command", "command": handoff_cmd, "timeout": timeout}
        placed = False
        for group in groups:
            if not isinstance(group, dict):
                continue
            for i, h in enumerate(group.get("hooks", []) or []):
                if isinstance(h, dict) and "memd-handoff-hook" in (h.get("command") or ""):
                    group["hooks"][i] = handoff_hook
                    if matcher is not None:
                        group["matcher"] = matcher
                    placed = True
                    break
            if placed:
                break
        if not placed:
            group = {"hooks": [handoff_hook]}
            if matcher is not None:
                group = {"matcher": matcher, "hooks": [handoff_hook]}
            groups.append(group)

with open(f, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
PYEOF
    echo "Previous Claude settings: $settings_backup"
    if [ "$activity_hook" -eq 0 ]; then
        echo "Activity recall hook not installed (re-run with --activity-hook to enable)."
    fi
    if [ "$handoff_hook" -eq 0 ]; then
        echo "Session handoff hook not installed (re-run with --handoff to enable)."
    fi

    # Register MCP server via claude CLI if available
    if command -v claude >/dev/null 2>&1; then
        claude_user_config="$HOME/.claude.json"
        claude_backup=
        if [ -f "$claude_user_config" ]; then
            claude_backup=$(mktemp "${claude_user_config}.memd-backup.XXXXXX")
            cp -p "$claude_user_config" "$claude_backup"
        fi
        claude mcp remove memd -s user 2>/dev/null || true
        if claude mcp add --transport http memd "$SERVER" \
             --header "Authorization: Bearer $TOKEN" -s user; then
            echo "Registered memd MCP server with claude"
            [ -z "$claude_backup" ] || echo "Previous Claude configuration: $claude_backup"
        else
            if [ -n "$claude_backup" ]; then
                cp -p "$claude_backup" "$claude_user_config"
            else
                # Restore the originally absent user configuration too.
                rm -f "$claude_user_config"
            fi
            fail_step "Failed to register memd MCP server with claude"
            echo "Restored previous Claude MCP configuration."
        fi
    else
        fail_step "Claude directory detected but claude CLI is unavailable; MCP registration incomplete"
    fi

else
    echo "Skipped Claude Code (no ~/.claude directory)"
fi

### Step 2: pi ###
if [ -d "$HOME/.pi/agent" ]; then
    echo "Configuring pi agent..."

    ext_dir="$HOME/.pi/agent/extensions"
    mkdir -p "$ext_dir"

    if ! download_client "$SERVER/clients/pi-memd.ts" "$ext_dir/memd.ts"; then
        fail_step "Failed to download pi-memd.ts"
    else
        echo "Installed pi extension: $ext_dir/memd.ts"
    fi

    echo "Wrote memd env to $env_file"
    echo "For Bash/Zsh, source this before starting pi:"
    echo "  [ -f ~/.config/memd/client.env ] && source ~/.config/memd/client.env"

else
    echo "Skipped pi agent (no ~/.pi/agent directory)"
fi

### Step 3: Install memd-mcp-bridge (for Claude Desktop and Codex) ###
bridge_bin="$HOME/.local/bin/memd-mcp-bridge"
bridge_needed=0
bridge_installed=0

# Detect whether Claude Desktop config or Codex config exists
if [ -f "$HOME/.config/Claude/claude_desktop_config.json" ]; then
    bridge_needed=1
fi
if [ -f "$codex_config" ] || command -v codex >/dev/null 2>&1; then
    bridge_needed=1
fi

if [ "$bridge_needed" -eq 1 ]; then
    echo "Installing memd-mcp-bridge..."
    mkdir -p "$(dirname "$bridge_bin")"
    if ! download_client "$SERVER/clients/memd-mcp-bridge" "$bridge_bin"; then
        fail_step "Failed to download memd-mcp-bridge"
    else
        chmod 755 "$bridge_bin"
        bridge_installed=1
        echo "Installed memd-mcp-bridge: $bridge_bin"
    fi
else
    echo "Skipped memd-mcp-bridge (no Claude Desktop or Codex detected)"
fi

### Step 4: Claude Desktop ###
if [ -f "$HOME/.config/Claude/claude_desktop_config.json" ]; then
    echo "Configuring Claude Desktop..."

    config_file="$HOME/.config/Claude/claude_desktop_config.json"
    backup_file="${config_file}.bak"

    # If bridge failed to install, skip this step
    if [ "$bridge_installed" -ne 1 ]; then
        fail_step "Skipping Claude Desktop config (memd-mcp-bridge not installed)"
    else
        # Back up the existing file
        cp "$config_file" "$backup_file"

        export SERVER PROFILE="$profile" TOKEN HOME

        # The failure handler must sit on the SAME line as the heredoc redirect;
        # a multi-line brace group here would be consumed as heredoc body.
        python3 - "$config_file" "$backup_file" "$bridge_bin" <<'PYEOF' || { fail_step "Failed to merge Claude Desktop config"; mv -f "$backup_file" "$config_file"; echo "Restored Claude Desktop config from backup."; }
import json
import os
import sys

config_path = sys.argv[1]
backup_path = sys.argv[2]
bridge_path = sys.argv[3]

server = os.environ["SERVER"]
profile = os.environ["PROFILE"]
token = os.environ["TOKEN"]
home = os.environ["HOME"]

# Read the file content
with open(config_path, "r") as fh:
    raw = fh.read()

# Parse JSON — if invalid, abort without overwriting
try:
    data = json.loads(raw)
except json.JSONDecodeError as e:
    print(f"ERROR: Claude Desktop config is invalid JSON ({e}); not modifying.")
    sys.exit(1)

# Build the memd server entry.
#
# The endpoint goes in MEMD_REMOTE, NOT in args. A harness that sees a bare https
# URL in a stdio server's args may decide the entry IS a remote HTTP MCP server,
# connect to it directly and never spawn this bridge at all -- so it never reads
# the token and every call fails 401. omp v17.3.4 does exactly that. The bridge
# already falls back to $MEMD_REMOTE when no positional arg is given.
memd_entry = {
    "command": f"{home}/.local/bin/memd-mcp-bridge",
    "args": [],
    "env": {
        "MEMD_REMOTE": server,
        "MEMD_ENV_FILE": os.environ["MEMD_CLIENT_ENV"],
        "MEMD_PROFILE": profile,
    },
}

# Merge into mcpServers (create if absent, overwrite memd key, preserve others)
mcp_servers = data.setdefault("mcpServers", {})
mcp_servers["memd"] = memd_entry

# Write the updated file
with open(config_path, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")

# Re-parse to confirm validity
with open(config_path, "r") as fh:
    try:
        json.load(fh)
    except json.JSONDecodeError as e:
        print(f"ERROR: Wrote Claude Desktop config is invalid JSON ({e}); restoring backup.")
        sys.exit(1)

print("Configured Claude Desktop MCP server: memd")
PYEOF
    fi

    if [ "$step_failed" -eq 1 ]; then
        : # error already reported
    else
        # chmod 600 — the config now contains the token
        chmod 600 "$config_file"
        echo "Set permissions on $config_file (chmod 600) — contains token"
        echo "NOTE: Claude Desktop must be restarted to pick up the new MCP configuration."
        echo "Unlike Claude Code, Claude Desktop has no hook mechanism, so memd recall is on-demand"
        echo "(the model calls the 'recall' tool) rather than automatic each turn."
    fi

else
    echo "Skipped Claude Desktop (no claude_desktop_config.json)"
fi

### Step 5: Codex ###
if [ -f "$codex_config" ] || command -v codex >/dev/null 2>&1; then
    if ! command -v codex >/dev/null 2>&1; then
        fail_step "Codex config exists but codex CLI is unavailable; config was preserved"
    elif [ "$bridge_installed" -ne 1 ]; then
        fail_step "Cannot configure Codex without the MCP bridge"
    else
        codex_backup=
        if [ -f "$codex_config" ]; then
            codex_backup=$(mktemp "${codex_config}.memd-backup.XXXXXX")
            cp -p "$codex_config" "$codex_backup"
        fi
        # Let Codex own TOML editing; replace only this named MCP entry. The
        # credential remains in a 0600 env file, outside config.toml.
        if codex mcp add memd --env "MEMD_REMOTE=$SERVER" \
             --env "MEMD_PROFILE=$profile" --env "MEMD_ENV_FILE=$env_file" \
             -- "$bridge_bin"; then
            echo "Configured Codex MCP server: memd"
            [ -z "$codex_backup" ] || echo "Previous Codex config: $codex_backup"
        else
            if [ -n "$codex_backup" ]; then cp -p "$codex_backup" "$codex_config"; fi
            fail_step "Failed to configure Codex; previous config restored where present"
        fi
    fi
else
    echo "Skipped Codex (no ~/.codex/config.toml)"
fi

### Step 6: Summary ###
echo ""
if [ "$step_failed" -eq 1 ]; then
    echo "ERROR: Onboarding incomplete; one or more steps failed." >&2
    exit 1
fi
echo "Onboarding complete."
echo "To revoke this token later, run on the server:"
echo "  memd-token revoke $profile <label>"
