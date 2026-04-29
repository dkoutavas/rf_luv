#!/usr/bin/env python3.11
# Pinned to 3.11 because leap's default `python3` is 3.6 (Leap 15.6).
"""ntfy.sh notifier for the RF reliability stack.

Stdlib-only POST to https://ntfy.sh/<topic>. Reads NTFY_TOPIC + NTFY_URL from
/etc/rtl-scanner/notify.env (KEY=VALUE lines). Idempotent within a 5-min
window per (level,title) pair, so repeated CB-open ticks don't spam the phone.

Importable: from notify import send
CLI: notify.py LEVEL TITLE [-m MESSAGE] [-t TAG ...]

Levels map to ntfy priorities:
    INFO     → priority 2 (low)   tag: white_check_mark
    WARN     → priority 3 (default) tag: warning
    CRITICAL → priority 5 (max)   tag: rotating_light
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ENV_PATH = "/etc/rtl-scanner/notify.env"
DEDUP_DIR = Path("/var/lib/rtl-tcp-escalator")
DEDUP_WINDOW_S = 300

LEVEL_PRIORITY = {"INFO": "2", "WARN": "3", "CRITICAL": "5"}
LEVEL_DEFAULT_TAG = {
    "INFO": "white_check_mark",
    "WARN": "warning",
    "CRITICAL": "rotating_light",
}


def load_env(path: str = ENV_PATH) -> dict:
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def _dedup_key(level: str, title: str) -> Path:
    safe = "".join(c if c.isalnum() else "_" for c in f"{level}_{title}")[:120]
    return DEDUP_DIR / f"last-notify-{safe}.ts"


def _is_duplicate(level: str, title: str) -> bool:
    f = _dedup_key(level, title)
    try:
        last = float(f.read_text().strip())
    except (FileNotFoundError, ValueError, PermissionError, OSError):
        return False
    return (time.time() - last) < DEDUP_WINDOW_S


def _record_sent(level: str, title: str) -> None:
    # Best-effort: dedup is a nice-to-have, not load-bearing. If the running
    # user can't write the dedup dir (e.g., dio_nysis CLI invocation against a
    # root-owned dir), we still want the notification to count as sent.
    try:
        DEDUP_DIR.mkdir(parents=True, exist_ok=True)
        _dedup_key(level, title).write_text(f"{time.time():.3f}\n")
    except OSError:
        pass


def send(level: str, title: str, message: str = "", tags: list | None = None,
         force: bool = False) -> bool:
    """POST a notification. Returns True if sent, False if deduped or no topic."""
    if level not in LEVEL_PRIORITY:
        level = "WARN"
    if not force and _is_duplicate(level, title):
        return False
    env = load_env()
    topic = env.get("NTFY_TOPIC", "").strip()
    base = env.get("NTFY_URL", "https://ntfy.sh").strip()
    if not topic:
        # No topic configured — operate in dry-run mode (don't crash; let
        # the action log carry the signal until owner sets up the topic).
        print(f"[notify] no NTFY_TOPIC; would send: {level} {title}: {message}",
              file=sys.stderr, flush=True)
        return False
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": LEVEL_PRIORITY[level],
        "Tags": ",".join(tags or [LEVEL_DEFAULT_TAG[level]]),
    }
    url = f"{base.rstrip('/')}/{topic}"
    body = (message or title).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in headers.items():
        req.add_header(k, v if isinstance(v, str) else v.decode("utf-8"))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except OSError as e:
        print(f"[notify] send failed: {e}", file=sys.stderr, flush=True)
        return False
    _record_sent(level, title)
    return True


def _cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("level", choices=list(LEVEL_PRIORITY.keys()))
    ap.add_argument("title")
    ap.add_argument("-m", "--message", default="")
    ap.add_argument("-t", "--tag", action="append", default=None)
    ap.add_argument("--force", action="store_true",
                    help="bypass 5-min dedup")
    args = ap.parse_args()
    ok = send(args.level, args.title, args.message, args.tag, force=args.force)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _cli()
