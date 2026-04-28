#!/usr/bin/env bash
# Enable xrdp on leap so the laptop's GNOME desktop can be reached over
# Tailscale from Windows mstsc.
#
# Why xrdp instead of gnome-remote-desktop:
# GRD 45 (the version pinned on Leap 15.6) only supports per-user
# `--headless` mode, which asks logind to spawn a GNOME session for the
# RDP client. logind refuses if the user already has a local session on
# tty2 ("Session creation inhibited"), so RDP connects then drops with
# no client-side error. GRD's proper "remote login" / system mode that
# fixes this only landed in GRD 46+, which Leap doesn't have.
#
# xrdp sidesteps the problem entirely: it spawns its own Xorg session
# per RDP login via PAM, independent of any local GNOME session. Local
# tty2 login and remote RDP login can coexist.
#
# Auth: your Linux account password (no separate RDP password).
# Session type: Xorg (not Wayland) — fine for general desktop / 2D apps.
#
# Run from any host that can reach leap over Tailscale:
#     bash ops/remote-desktop/enable-xrdp.sh
# Asks for sudo on leap.
#
# After it finishes: connect with Windows mstsc to "scanner:3389", log
# in with your Linux username and password.
#
# To disable later: ssh scanner 'sudo systemctl disable --now xrdp.service'

set -euo pipefail

echo "==> reconfiguring leap for xrdp"

# Stage the payload first (no TTY needed), then run it under sudo with a
# real TTY in a second ssh so the sudo password prompt works normally.
# Trying to combine `ssh -t … 'sudo bash -s' <<EOF` deadlocks: the
# heredoc on stdin and the sudo password prompt fight over the same fd.
REMOTE_SCRIPT=/tmp/enable-xrdp-remote.$$.sh
ssh scanner "cat > $REMOTE_SCRIPT && chmod 700 $REMOTE_SCRIPT" <<'REMOTE'
set -e

echo "-- stopping old gnome-remote-desktop user service (frees :3389)"
sudo -u dio_nysis \
    XDG_RUNTIME_DIR=/run/user/1000 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
    systemctl --user disable --now gnome-remote-desktop.service 2>/dev/null || true

# Headless override no longer applies; remove so future GRD use (if any)
# starts from a clean slate.
rm -f /home/dio_nysis/.config/systemd/user/gnome-remote-desktop.service.d/headless.conf
rmdir /home/dio_nysis/.config/systemd/user/gnome-remote-desktop.service.d 2>/dev/null || true

echo "-- installing xrdp + xorgxrdp"
zypper --non-interactive install xrdp xorgxrdp

# Make sure no stale process is still bound to :3389 before we start xrdp.
if ss -lntp 2>/dev/null | grep -q ':3389\b'; then
    echo "!! something still bound to :3389, listing it:"
    ss -lntp | grep ':3389\b' || true
    echo "!! aborting so we don't fight over the port — investigate and rerun"
    exit 1
fi

echo "-- enabling xrdp services"
systemctl enable --now xrdp.service xrdp-sesman.service

sleep 2

echo
echo "-- service status"
systemctl is-active xrdp.service xrdp-sesman.service || true
echo "-- listening port"
ss -lnt | grep -E ':3389\b' || echo "(nothing listening on :3389)"
echo "-- tailnet ip"
sudo -u dio_nysis tailscale ip -4 | head -1
REMOTE

ssh -t scanner "sudo bash $REMOTE_SCRIPT; rm -f $REMOTE_SCRIPT"

echo
echo "==> done. From Windows, run mstsc and connect to:"
echo "    scanner:3389       (or the tailnet IP shown above)"
echo "User:     dio_nysis    (your Linux username)"
echo "Password: your Linux account password"
echo
echo "Note: the first connection shows an untrusted-cert warning"
echo "      (xrdp generates a self-signed cert at install time). Accept"
echo "      it once — fine over the tailnet."
echo
echo "If GNOME doesn't come up cleanly inside the RDP session, create"
echo "    ~/.xsession  with:  exec gnome-session"
echo "and reconnect."
