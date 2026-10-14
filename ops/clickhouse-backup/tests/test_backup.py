#!/usr/bin/env python3
"""Self-test for backup.sh's failure paths. stdlib only.

Run bare:   python3 ops/clickhouse-backup/tests/test_backup.py
Or:         pytest ops/clickhouse-backup/tests/test_backup.py

The regression this guards: with the ClickHouse container stopped, backup.sh
skipped every database as "no MergeTree tables found" and still logged
"[ok] clickhouse-backup done (0 tables ...)". No alert, green timer, no
backup. It must exit non-zero and send a CRITICAL instead.

A fake `docker` and `rf-notify` on PATH stand in for the real ones, so the
test never touches the live container or sends a real popup.
"""

import os
import subprocess
import tempfile
from pathlib import Path

BACKUP_SH = Path(__file__).resolve().parent.parent / "backup.sh"


def _run(running):
    """Run backup.sh against a fake docker. `running` is what `docker inspect`
    prints for State.Running. Every `docker exec` fails, as when the server
    inside the container is not answering. Returns (exit code, notify calls)."""
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp, "bin")
        bin_dir.mkdir()
        notify_log = Path(tmp, "notify.log")
        (bin_dir / "docker").write_text(
            "#!/bin/bash\n"
            f'[ "$1" = inspect ] && {{ echo {running}; exit 0; }}\n'
            'echo "Error response from daemon: container is not running" >&2\n'
            "exit 1\n")
        (bin_dir / "rf-notify").write_text(
            f'#!/bin/bash\necho "$*" >> {notify_log}\n')
        for f in bin_dir.iterdir():
            f.chmod(0o755)
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "CLICKHOUSE_BACKUP_ENV": "/nonexistent",   # never read the host's env file
            "BACKUP_DIR": str(Path(tmp, "backups")),
            "DATABASES": "spectrum acars",
        }
        result = subprocess.run(["bash", str(BACKUP_SH)], env=env,
                                capture_output=True, text=True, timeout=30)
        calls = notify_log.read_text().splitlines() if notify_log.exists() else []
    return result.returncode, calls


def test_stopped_container_fails_and_alerts():
    # docker inspect exits 0 for a stopped container and prints "false"
    code, calls = _run(running="false")
    assert code == 1
    assert len(calls) == 1 and calls[0].startswith("CRITICAL")


def test_unanswering_server_fails_and_alerts():
    # Container up, but every clickhouse-client call fails: 0 tables dumped
    code, calls = _run(running="true")
    assert code == 1
    assert len(calls) == 1 and "0 tables dumped" in calls[0]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
