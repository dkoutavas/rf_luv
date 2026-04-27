#!/usr/bin/env bash
# Move tailscale0 into firewalld's "trusted" zone on leap. After this,
# any port a service listens on (e.g. RDP 3389, Grafana 3003) is
# reachable from other tailnet nodes without per-port firewall rules.
# LAN traffic is unaffected — physical interfaces stay in the default
# zone with their existing rules.
#
# Run from any host that can reach leap over Tailscale:
#     bash ops/remote-desktop/trust-tailscale-iface.sh
# Asks for sudo password on leap.

set -euo pipefail

echo "==> trusting tailscale0 on leap"
ssh -t scanner "sudo bash -c '
    set -e
    if ! command -v firewall-cmd >/dev/null; then
        echo \"firewalld not installed; this script is for firewalld setups only\" >&2
        exit 1
    fi
    echo \"-- before\"
    firewall-cmd --get-active-zones
    firewall-cmd --zone=trusted --change-interface=tailscale0 --permanent
    firewall-cmd --reload
    echo \"-- after\"
    firewall-cmd --get-active-zones
    echo \"-- 3389 reachable?\"
    ss -lnt | grep -E \":3389\\b\" || echo \"(nothing listening on :3389)\"
'"
echo "==> done"
