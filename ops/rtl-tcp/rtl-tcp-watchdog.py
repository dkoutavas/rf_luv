#!/usr/bin/env python3
"""rtl_tcp health probe — detects TCP-accepts-but-no-samples, recovers.

Failure mode this targets: rtl_tcp listens on its port, clients connect fine,
and the RTL0 greeting is sent, but no IQ samples follow (USB firmware stuck).
Seen live on 2026-04-18 after ~13 hours of runtime — only physical replug
recovered. Unbind/rebind via /sys/bus/usb/drivers/usb is the software
equivalent of replug, so the watchdog escalates to that on repeated failure.

Supports per-instance invocation via --serial / --unit so two dongles can
have independent watchdogs without cross-bouncing each other. The USB-reset
helper takes the same serial and unbinds only the matching device path.

Environment / CLI:
    --serial SERIAL    Dongle serial (e.g. v3-01). Required for template usage.
                       Controls the state file path AND which USB device gets
                       reset. Default: empty (single-instance legacy mode).
    --unit NAME        systemd unit to restart on soft-recovery (e.g.
                       rtl-tcp@v3-01.service). Default: rtl_tcp.service.
    RTL_TCP_HOST       Probe target host. Default 127.0.0.1.
    RTL_TCP_PORT       Probe target port. Default 1234.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time

CONNECT_TIMEOUT = 3.0
GREETING_TIMEOUT = 3.0
SAMPLE_WINDOW_S = 2.0
SAMPLE_MIN_BYTES = 512 * 1024  # at 2 MS/s real rate ~4 MB/s; 512KB/2s = floor

# Circuit breaker — after this many consecutive failures, stop trying recovery
# actions and just log CIRCUIT_BREAKER_OPEN every tick until a probe succeeds.
# Observed live on 2026-04-23: watchdog entered a 1000+ iteration reset loop
# for V4, which kept hammering USB + causing cascading instability for V3.
# 10 × 30s probe cadence = ~5 min tolerance before we give up and alert.
MAX_CONSECUTIVE_FAILURES = 10

# Rate-limit hard USB resets to at most one every this many seconds. The
# USB subsystem needs time to settle after an unbind/bind cycle; bouncing
# it every 30s is counter-productive. 5 min lets transient glitches
# resolve and gives any pending enumerate-on-connect races time to finish.
HARD_RESET_COOLDOWN_S = 300


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="",
                    help="Dongle serial (v3-01, v4-01); used for state file + rtl-usb-reset target")
    ap.add_argument("--unit", default="rtl_tcp.service",
                    help="systemd user unit to restart on soft-recovery")
    return ap.parse_args()


def log(msg, serial=""):
    tag = f"watchdog{':' + serial if serial else ''}"
    print(f"[{tag}] {msg}", file=sys.stderr, flush=True)


def state_path(serial: str) -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    suffix = f"-{serial}" if serial else ""
    return os.path.join(base, f"rtl-tcp-watchdog{suffix}.state")


def load_state(path: str):
    try:
        with open(path) as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    # Back-compat defaults — older state files won't have last_hard_reset_ts.
    s.setdefault("consecutive_failures", 0)
    s.setdefault("last_hard_reset_ts", 0.0)
    return s


def save_state(path: str, state):
    with open(path, "w") as f:
        json.dump(state, f)


def _hex_ip_is_loopback(hex_ip: str) -> bool:
    """True if a hex-encoded IP from /proc/net/tcp{,6} is a loopback address.

    /proc/net/tcp stores IPv4 in little-endian hex; tcp6 stores IPv6 (and
    IPv4-mapped IPv6) as 32 hex chars. Loopback covers 127.0.0.0/8, ::1, and
    ::ffff:127.0.0.0/8.
    """
    if len(hex_ip) == 8:  # IPv4 (little-endian)
        return int(hex_ip[6:8], 16) == 127
    if len(hex_ip) == 32:  # IPv6
        if hex_ip.upper() == "00000000000000000000000001000000":
            return True  # ::1
        if hex_ip[:24].upper() == "00000000000000000000FFFF":
            return _hex_ip_is_loopback(hex_ip[24:])  # IPv4-mapped
    return False


def has_external_client(port: int) -> bool:
    """True if rtl_tcp's port has an ESTABLISHED connection from a non-loopback peer.

    rtl_tcp accepts a single client at a time; an active probe would kick whoever
    is currently streaming. The scanner and watchdog connect via 127.0.0.1, so
    a non-loopback peer means a real human listener (SDR++, SDR Console, etc.)
    is on the line. If they're getting samples, rtl_tcp is healthy by proxy and
    we skip the active probe to avoid disconnecting them every 30s.
    """
    target_local = f"{port:04X}"
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                next(f)  # header
                for line in f:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    local_addr, rem_addr, state = parts[1], parts[2], parts[3]
                    if state != "01":  # TCP_ESTABLISHED
                        continue
                    local_port_hex = local_addr.rsplit(":", 1)[-1]
                    if local_port_hex.upper() != target_local:
                        continue
                    rem_ip_hex = rem_addr.rsplit(":", 1)[0]
                    if not _hex_ip_is_loopback(rem_ip_hex):
                        return True
        except FileNotFoundError:
            continue
    return False


def probe(host: str, port: int):
    try:
        s = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except OSError as e:
        return False, f"connect failed: {e}"
    try:
        s.settimeout(GREETING_TIMEOUT)
        header = b""
        while len(header) < 12:
            chunk = s.recv(12 - len(header))
            if not chunk:
                return False, f"greeting truncated at {len(header)}B"
            header += chunk
        if header[:4] != b"RTL0":
            return False, f"bad greeting: {header[:4]!r}"

        s.settimeout(SAMPLE_WINDOW_S + 1.0)
        t0 = time.time()
        total = 0
        while time.time() - t0 < SAMPLE_WINDOW_S:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                return False, f"EOF after {total}B"
            total += len(chunk)
            if total >= SAMPLE_MIN_BYTES:
                return True, f"{total}B in {time.time()-t0:.2f}s"
        return False, f"starvation: {total}B in {SAMPLE_WINDOW_S:.1f}s"
    finally:
        try:
            s.close()
        except OSError:
            pass


def recover(fails: int, serial: str, unit: str, state: dict):
    """Escalate recovery actions and record state mutations on `state` in-place.

    fails = current consecutive_failures counter (pre-increment on this call).
    Side effect: may update state["last_hard_reset_ts"].
    """
    soft_restart = ["systemctl", "--user", "restart", unit]
    hard_reset_cmd = ["sudo", "-n", "/usr/local/sbin/rtl-usb-reset"]
    if serial:
        hard_reset_cmd.append(serial)  # per-serial USB reset — unbinds only this dongle

    if fails <= 2:
        log(f"soft restart (fail #{fails}) → {unit}", serial)
        subprocess.run(soft_restart, check=False)
        return

    # Hard reset path — gate on cooldown. If we've done a hard reset too recently,
    # fall back to soft restart only. This prevents the 2026-04-23 pathology
    # where a stuck V4 got hard-reset every 30s for hours while the subsystem
    # never had time to settle.
    now = time.time()
    since_last = now - state.get("last_hard_reset_ts", 0.0)
    if since_last < HARD_RESET_COOLDOWN_S:
        remaining = HARD_RESET_COOLDOWN_S - since_last
        log(f"soft restart only (fail #{fails}); hard-reset cooldown: {remaining:.0f}s left", serial)
        subprocess.run(soft_restart, check=False)
        return

    log(f"hard USB reset (fail #{fails}) → rtl-usb-reset {serial or '(all)'}", serial)
    r = subprocess.run(hard_reset_cmd, check=False)
    if r.returncode != 0:
        log(f"rtl-usb-reset returned {r.returncode}", serial)
    state["last_hard_reset_ts"] = now
    subprocess.run(soft_restart, check=False)


def main():
    args = parse_args()
    serial = args.serial
    unit = args.unit

    host = os.environ.get("RTL_TCP_HOST", "127.0.0.1")
    port = int(os.environ.get("RTL_TCP_PORT", "1234"))

    path = state_path(serial)
    state = load_state(path)

    # Don't fight an active human listener. rtl_tcp is single-client, so an
    # active probe would kick SDR++ / SDR Console off every 30s. If a
    # non-loopback peer is established on the rtl_tcp port, it's a real
    # client streaming samples — treat that as healthy by proxy and skip.
    if has_external_client(port):
        if state["consecutive_failures"] > 0:
            log("external client connected; clearing failure counter", serial)
        state["consecutive_failures"] = 0
        save_state(path, state)
        return

    ok, reason = probe(host, port)
    if ok:
        if state["consecutive_failures"] > 0:
            log(f"recovered: {reason}", serial)
        state["consecutive_failures"] = 0
    else:
        state["consecutive_failures"] += 1
        fails = state["consecutive_failures"]
        log(f"unhealthy: {reason}", serial)
        if fails > MAX_CONSECUTIVE_FAILURES:
            # Circuit breaker open — stop hammering the USB subsystem. Keep
            # probing (counter stays incremented, keeps being reported) so
            # a lucky recovery is still noticed and clears the counter above.
            # Emit on every tick so the journal stays actionable — this is
            # the alert we watch for.
            log(
                f"CIRCUIT_BREAKER_OPEN fail #{fails} > {MAX_CONSECUTIVE_FAILURES}; "
                f"skipping recovery (human intervention needed)",
                serial,
            )
        else:
            recover(fails, serial, unit, state)
    save_state(path, state)


if __name__ == "__main__":
    main()
