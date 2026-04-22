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
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"consecutive_failures": 0}


def save_state(path: str, state):
    with open(path, "w") as f:
        json.dump(state, f)


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


def recover(fails: int, serial: str, unit: str):
    # Escalate: 1-2 soft restarts of the matching rtl_tcp unit; from 3rd onward
    # unbind/rebind the specific USB device identified by serial.
    soft_restart = ["systemctl", "--user", "restart", unit]
    hard_reset = ["sudo", "-n", "/usr/local/sbin/rtl-usb-reset"]
    if serial:
        hard_reset.append(serial)  # per-serial USB reset — unbinds only this dongle

    if fails <= 2:
        log(f"soft restart (fail #{fails}) → {unit}", serial)
        subprocess.run(soft_restart, check=False)
        return

    log(f"hard USB reset (fail #{fails}) → rtl-usb-reset {serial or '(all)'}", serial)
    r = subprocess.run(hard_reset, check=False)
    if r.returncode != 0:
        log(f"rtl-usb-reset returned {r.returncode}", serial)
    subprocess.run(soft_restart, check=False)


def main():
    args = parse_args()
    serial = args.serial
    unit = args.unit

    host = os.environ.get("RTL_TCP_HOST", "127.0.0.1")
    port = int(os.environ.get("RTL_TCP_PORT", "1234"))

    path = state_path(serial)
    state = load_state(path)
    ok, reason = probe(host, port)
    if ok:
        if state["consecutive_failures"] > 0:
            log(f"recovered: {reason}", serial)
        state["consecutive_failures"] = 0
    else:
        state["consecutive_failures"] += 1
        log(f"unhealthy: {reason}", serial)
        recover(state["consecutive_failures"], serial, unit)
    save_state(path, state)


if __name__ == "__main__":
    main()
