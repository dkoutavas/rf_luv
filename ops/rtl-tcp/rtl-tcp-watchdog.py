#!/usr/bin/env python3
"""rtl_tcp health probe — detects TCP-accepts-but-no-samples, recovers.

Failure mode this targets: rtl_tcp listens on :1234, clients connect fine, and
the RTL0 greeting is sent, but no IQ samples follow (USB firmware stuck).
Seen live on 2026-04-18 after ~13 hours of runtime — only physical replug
recovered. Unbind/rebind via /sys/bus/usb/drivers/usb is the software
equivalent of replug, so the watchdog escalates to that on repeated failure.
"""

import json
import os
import socket
import subprocess
import sys
import time

HOST = os.environ.get("RTL_TCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("RTL_TCP_PORT", "1234"))
STATE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "rtl-tcp-watchdog.state")

CONNECT_TIMEOUT = 3.0
GREETING_TIMEOUT = 3.0
SAMPLE_WINDOW_S = 2.0
SAMPLE_MIN_BYTES = 512 * 1024  # at 2 MS/s real rate is ~4 MB/s; 512KB/2s = floor

SOFT_RESTART = ["systemctl", "--user", "restart", "rtl_tcp.service"]
HARD_RESET = ["sudo", "-n", "/usr/local/sbin/rtl-usb-reset"]


def log(msg):
    print(f"[watchdog] {msg}", file=sys.stderr, flush=True)


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"consecutive_failures": 0}


def save_state(state):
    with open(STATE, "w") as f:
        json.dump(state, f)


def probe():
    try:
        s = socket.create_connection((HOST, PORT), timeout=CONNECT_TIMEOUT)
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


def recover(fails):
    # Escalate: 1-2 soft restarts of rtl_tcp; from 3rd onward, unbind/rebind USB
    if fails <= 2:
        log(f"soft restart (fail #{fails})")
        subprocess.run(SOFT_RESTART, check=False)
        return
    log(f"hard USB reset (fail #{fails})")
    r = subprocess.run(HARD_RESET, check=False)
    if r.returncode != 0:
        log(f"rtl-usb-reset returned {r.returncode}")
    subprocess.run(SOFT_RESTART, check=False)


def main():
    state = load_state()
    ok, reason = probe()
    if ok:
        if state["consecutive_failures"] > 0:
            log(f"recovered: {reason}")
        state["consecutive_failures"] = 0
    else:
        state["consecutive_failures"] += 1
        log(f"unhealthy: {reason}")
        recover(state["consecutive_failures"])
    save_state(state)


if __name__ == "__main__":
    main()
