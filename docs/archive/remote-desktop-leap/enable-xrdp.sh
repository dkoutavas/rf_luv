#!/usr/bin/env bash
# Enable xrdp on leap so the laptop's desktop can be reached from
# Windows mstsc, either over Tailscale or directly on the home LAN.
#
# Why xrdp instead of gnome-remote-desktop:
# GRD 45 (the version pinned on Leap 15.6) only supports per-user
# `--headless` mode, which asks logind to spawn a GNOME session for the
# RDP client. logind refuses if the user already has a local session on
# tty2 ("Session creation inhibited"), so RDP connects then drops with
# no client-side error. GRD's proper "remote login" / system mode that
# fixes this only landed in GRD 46+, which Leap doesn't have.
#
# xrdp sidesteps the problem entirely: it spawns its own X server (Xvnc
# backend on Leap by default) per RDP login via PAM, independent of any
# local GNOME session. Local tty2 login and remote RDP login coexist.
#
# Why icewm and not GNOME for the RDP session:
# When a GNOME session is already running locally on tty2, a second
# gnome-session inside Xvnc fails to launch (D-Bus / keyring / shell
# services conflict with the active session) and the client sees a
# black screen that disconnects after a few seconds. icewm is a small
# X11 window manager that runs cleanly inside Xvnc alongside an active
# GNOME session. RDP gets a usable desktop with a taskbar and right-
# click menu; the local GNOME on the physical machine is untouched.
# This script flips /etc/xrdp/startwm.sh's SESSION variable to icewm.
#
# Auth: your Linux account password (no separate RDP password).
#
# Prerequisites on the machine you run this from:
#   - "scanner" must resolve to leap. Either:
#     a) Tailscale + MagicDNS (works from anywhere), or
#     b) ~/.ssh/config alias pointing at leap's LAN IP, e.g.:
#          Host scanner
#              HostName 192.168.2.10
#              User dio_nysis
#   - You must be able to ssh scanner without a password (key-based).
#
# Run:
#     bash ops/remote-desktop/enable-xrdp.sh
# Asks for sudo on leap. NOTE: openSUSE's sudo is configured with
# `Defaults targetpw`, so it prompts for the *root* password, not your
# user password.
#
# After it finishes: connect with Windows mstsc.
#   - Over Tailscale (works from anywhere): mstsc → "scanner" or the
#     tailnet IP printed at the end. Requires Tailscale on Windows.
#   - On the home LAN: mstsc → leap's LAN IP (e.g. 192.168.2.10). The
#     companion script trust-tailscale-iface.sh only opens 3389 on the
#     tailscale0 interface; if you want LAN-direct RDP you'll also need
#     a firewalld rule on the public/home zone.
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

echo "-- installing xrdp + xorgxrdp + icewm"
# icewm is the RDP-side window manager (see header for why not GNOME).
zypper --non-interactive install xrdp xorgxrdp icewm

# Make sure no stale process is still bound to :3389 before we start xrdp.
if ss -lntp 2>/dev/null | grep -q ':3389\b'; then
    echo "!! something still bound to :3389, listing it:"
    ss -lntp | grep ':3389\b' || true
    echo "!! aborting so we don't fight over the port — investigate and rerun"
    exit 1
fi

echo "-- pointing /etc/xrdp/startwm.sh at icewm"
# Idempotent: only rewrites if it's still on the gnome default. The
# whitespace-tolerant regex matches the indented `SESSION="gnome"` line
# inside wm_start().
if grep -qE '^[[:space:]]*SESSION="gnome"' /etc/xrdp/startwm.sh; then
    sed -i.bak -E 's/^([[:space:]]*)SESSION="gnome"/\1SESSION="icewm"/' /etc/xrdp/startwm.sh
    echo "   switched gnome -> icewm (backup: /etc/xrdp/startwm.sh.bak)"
else
    echo "   already not on gnome; current setting:"
    grep -E '^[[:space:]]*SESSION=' /etc/xrdp/startwm.sh || true
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
echo "Session: icewm (lightweight X11 WM). Right-click empty desktop"
echo "         for the menu; taskbar at bottom. Run any X11 app from"
echo "         a terminal — Firefox, file managers, SDR++ etc. all work."
echo "         GNOME on tty2 keeps running independently."
