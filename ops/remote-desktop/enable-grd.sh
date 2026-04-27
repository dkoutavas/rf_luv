#!/usr/bin/env bash
# Enable GNOME Remote Desktop (RDP) on leap so the laptop's GNOME session
# can be reached over Tailscale from Windows mstsc.
#
# Why RDP and not Steam Remote Play: simpler bootstrap from a remote
# terminal (no need to start Steam first), works for any GUI app, fine
# latency for 2D games like Balatro. If you later want better game-stream
# quality, configure Steam Remote Play from inside the RDP session.
#
# Run from any host that can reach leap over Tailscale:
#     bash ops/remote-desktop/enable-grd.sh
# It prompts for the RDP password interactively (not stored in shell
# history). RDP user defaults to 'dio_nysis' but can be changed via env:
#     RDP_USER=foo bash ops/remote-desktop/enable-grd.sh
#
# After it finishes: connect with mstsc → "scanner" → log in with the
# RDP credentials you just set. (RDP credentials are independent of the
# Linux account password.)
#
# To disable later: ssh scanner 'systemctl --user disable --now gnome-remote-desktop'

set -euo pipefail

RDP_USER="${RDP_USER:-dio_nysis}"

read -r -s -p "RDP password for user '$RDP_USER' (will be set on leap): " RDP_PASS
echo
if [ -z "$RDP_PASS" ]; then
    echo "error: empty password" >&2
    exit 1
fi

echo "==> configuring gnome-remote-desktop on leap"

# Pipe the whole bash payload to a fresh `bash -l` on leap. Credentials
# get prefixed as variable assignments inside the payload — so they never
# touch the SSH command line (where they would be visible in `ps`).
{
    printf 'RDP_USER=%q\n' "$RDP_USER"
    printf 'RDP_PASS=%q\n' "$RDP_PASS"
    cat <<'REMOTE'
set -e
export XDG_RUNTIME_DIR=/run/user/1000
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus

CERT_DIR="$HOME/.local/share/gnome-remote-desktop"
mkdir -p "$CERT_DIR"
chmod 700 "$CERT_DIR"
CERT="$CERT_DIR/rdp-tls.crt"
KEY="$CERT_DIR/rdp-tls.key"

# Self-signed cert; mstsc shows a one-time untrusted-cert warning, fine over tailnet.
if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
    echo "-- generating self-signed TLS cert"
    openssl req -new -newkey rsa:4096 -days 3650 -nodes -x509 \
        -subj "/CN=scanner" \
        -keyout "$KEY" -out "$CERT" 2>/dev/null
    chmod 600 "$KEY"
fi

grdctl --headless rdp set-tls-cert "$CERT"
grdctl --headless rdp set-tls-key "$KEY"
grdctl --headless rdp set-credentials "$RDP_USER" "$RDP_PASS"
grdctl --headless rdp disable-view-only
grdctl --headless rdp enable
gsettings set org.gnome.desktop.remote-desktop.rdp enable true

# Drop-in override so the daemon runs with --headless and reads credentials
# from the same store grdctl --headless wrote to (bypassing the locked keyring).
DROPIN_DIR="$HOME/.config/systemd/user/gnome-remote-desktop.service.d"
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_DIR/headless.conf" <<'OVERRIDE'
[Service]
ExecStart=
ExecStart=/usr/lib/gnome-remote-desktop-daemon --headless
OVERRIDE

systemctl --user daemon-reload
systemctl --user enable gnome-remote-desktop
systemctl --user restart gnome-remote-desktop
sleep 2

echo "-- final status"
grdctl --headless status | head -10
echo "-- service"
systemctl --user is-active gnome-remote-desktop
echo "-- listening port"
ss -lnt 2>&1 | grep -E ":3389\b" || echo "(nothing listening on :3389)"
echo "-- tailnet ip"
tailscale ip -4 | head -1
REMOTE
} | ssh scanner 'bash -l'

echo
echo "==> done. From Windows, run mstsc and connect to:"
echo "    scanner:3389       (or the tailnet IP shown above)"
echo "User: $RDP_USER"
