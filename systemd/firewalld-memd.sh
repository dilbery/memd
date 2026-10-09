#!/usr/bin/env bash
# Scope memd's port (8077) to the trusted LAN only. Uses firewalld (not ufw).
# /recall and /health are open on the LAN; /save|/reflect|/admin also
# require MEMD_TOKEN (app-layer, see memd.server.require_token). This restricts
# even the open recall surface to the 10.10.1.0/24 LAN so nothing is WAN-exposed.
set -euo pipefail

PORT="${MEMD_PORT:-8077}"
LAN="${MEMD_LAN_CIDR:-10.10.1.0/24}"

# A dedicated rich rule: accept tcp/PORT only from the LAN, on the default zone.
sudo -n firewall-cmd --permanent \
  --add-rich-rule="rule family=ipv4 source address=${LAN} port port=${PORT} protocol=tcp accept"

# Belt-and-braces: ensure the bare port is NOT opened to all zones.
sudo -n firewall-cmd --permanent --remove-port="${PORT}/tcp" 2>/dev/null || true

sudo -n firewall-cmd --reload
echo "memd: tcp/${PORT} accepted from ${LAN} only"
