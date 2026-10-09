#!/bin/sh
# Canonical apphost deployment. Installed at ~/docker/memd/deploy.sh.
set -eu
cd "$(dirname "$0")"

# The second store's Compose service (its /health is on :8078).
SECOND=${MEMD_SECOND_SERVICE:-memd-cobalt}

# A second deploy must not replace the rollback tags while the first is still
# building or verifying. Keep the lock for the complete script lifetime.
command -v flock >/dev/null 2>&1 || { echo "ERROR: flock is required for deployment." >&2; exit 1; }
exec 9>.deploy.lock
flock -n 9 || { echo "ERROR: another memd deployment is running." >&2; exit 1; }

# Capture the actual running images BEFORE build can replace memd:local.
# Keep separate tags because the two instances might currently differ.
rollback_primary=
rollback_second=
for service in memd "$SECOND"; do
    container=$(docker compose ps -q "$service")
    if [ -n "$container" ]; then
        previous=$(docker inspect --format '{{.Image}}' "$container")
        docker tag "$previous" "${service}:rollback"
        case "$service" in
            memd) rollback_primary=$previous ;;
            "$SECOND") rollback_second=$previous ;;
        esac
        echo "Rollback saved for ${service}: ${previous}"
    fi
done

rollout_started=0
rollback_file=
finish() {
    rc=$?
    trap - EXIT
    if [ "$rc" -ne 0 ] && [ "$rollout_started" -eq 1 ]; then
        echo "Deployment failed; restoring previous running images." >&2
        if [ -n "$rollback_primary" ] && [ -n "$rollback_second" ]; then
            rollback_file=$(mktemp "./rollback.XXXXXX.yml")
            cat > "$rollback_file" <<EOF
services:
  memd:
    image: memd:rollback
  ${SECOND}:
    image: ${SECOND}:rollback
EOF
            # Compose auto-discovers the normal file; COMPOSE_FILE may override it.
            compose_base=${COMPOSE_FILE:-}
            if [ -z "$compose_base" ]; then
                for candidate in compose.yaml compose.yml docker-compose.yml docker-compose.yaml; do
                    if [ -f "$candidate" ]; then compose_base=$candidate; break; fi
                done
            fi
            if [ -n "$compose_base" ]; then
                COMPOSE_FILE="${compose_base}:${rollback_file}" \
                    docker compose up -d --no-build memd "$SECOND" || \
                    echo "ERROR: automatic rollback failed; rollback image tags are preserved." >&2
            else
                echo "ERROR: no Compose file found for rollback; image tags are preserved." >&2
            fi
        else
            echo "No complete previous deployment; rollback tags retained where available." >&2
        fi
    fi
    [ -z "$rollback_file" ] || rm -f "$rollback_file"
    exit "$rc"
}
trap finish EXIT

echo "Pulling source"
# Git owns SSH resolution: honor repo core.sshCommand or the caller's explicit
# GIT_SSH_COMMAND, including configured host aliases and deploy identities.
GIT_TERMINAL_PROMPT=0 git -C src pull --ff-only
SHA=$(git -C src rev-parse HEAD)
echo "Building ${SHA}"
docker compose build --build-arg GIT_SHA="$SHA" memd
rollout_started=1
docker compose up -d memd "$SECOND"

# Check JSON values, not text fragments or HTTP status alone. Model backends
# may degrade, but both stores must serve the exact built commit and current
# lexical index. Poll startup for up to 12 bounded attempts per instance.
for port in 8077 8078; do
    verified=0
    attempt=0
    while [ "$attempt" -lt 12 ]; do
        attempt=$((attempt + 1))
        if body=$(curl -fsS -m 10 "http://127.0.0.1:${port}/health"); then
            if printf '%s' "$body" | python3 -c '
import json, sys
try:
    h = json.load(sys.stdin)
    c = h.get("checks", {})
    valid = (h.get("ok") is True and h.get("app_commit") == sys.argv[1]
             and c.get("index", {}).get("ok") is True
             and c.get("git", {}).get("ok") is True
             and c.get("git", {}).get("in_sync") is True)
    degraded = [k for k, v in c.items() if isinstance(v, dict) and v.get("ok") is not True]
    print("port={} commit={} status={} degraded={}".format(
        sys.argv[2], h.get("app_commit"), h.get("status"), ",".join(degraded) or "none"))
except (ValueError, AttributeError, TypeError) as exc:
    print("Invalid health response: {}".format(exc), file=sys.stderr)
    valid = False
sys.exit(0 if valid else 1)
' "$SHA" "$port"; then
                verified=1
                break
            fi
        fi
        [ "$attempt" -ge 12 ] || sleep 2
    done
    if [ "$verified" -ne 1 ]; then
        echo "ERROR: :${port} did not pass health/commit validation." >&2
        exit 1
    fi
done
echo "Deployed and verified ${SHA} on both instances"
