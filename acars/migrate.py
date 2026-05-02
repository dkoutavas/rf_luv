#!/usr/bin/env python3
"""
ClickHouse schema migration runner for the acars pipeline.

Scans clickhouse/migrations/ for numbered SQL files (NNN_description.sql),
tracks which have been applied in a schema_migrations table, and runs
any pending migrations in order.

Stdlib-only — uses urllib for ClickHouse HTTP. Mirrors spectrum/migrate.py
in behaviour (including its SQL splitter that handles `;` inside comments,
discovered while applying spectrum migrations 003 and 004 on 2026-04-18).

Designed to run at container startup before acarsdec/ingest start.
Safe to run repeatedly — already-applied migrations are skipped.

Usage:
    python3 migrate.py                 # run pending migrations
    python3 migrate.py --status        # show migration status
    python3 migrate.py --dry-run       # show what would run without applying
"""

import os
import re
import sys
import time
import json
import hashlib
import logging
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("acars-migrate")

CH_HOST = os.environ.get("CLICKHOUSE_HOST", "clickhouse")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CH_DB = os.environ.get("CLICKHOUSE_DB", "acars")
CH_USER = os.environ.get("CLICKHOUSE_USER", "acars")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "acars_local")

MIGRATIONS_DIR = Path(__file__).parent / "clickhouse" / "migrations"
MIGRATION_PATTERN = re.compile(r"^(\d{3})_.+\.sql$")
CH_URL = f"http://{CH_HOST}:{CH_PORT}/"


def ch_query(sql: str, data: str = "", timeout: int = 30) -> str:
    """Execute a ClickHouse query via the HTTP interface.

    Two call shapes (mirrors spectrum/db.py):
      - DDL / SELECT:        ch_query(sql)               → SQL in body (POST)
      - INSERT with payload: ch_query(sql, data=rows)    → SQL in URL, payload in body

    Both shapes POST; never GET. ClickHouse 24.3 forbids DDL via GET
    ("Cannot execute query in readonly mode") so a body is always required.
    """
    base_params = f"database={CH_DB}&user={CH_USER}&password={CH_PASSWORD}"
    if data:
        url = f"{CH_URL}?{base_params}&query={quote(sql)}"
        body = data.encode("utf-8")
    else:
        url = f"{CH_URL}?{base_params}"
        body = sql.encode("utf-8")
    req = Request(url, data=body)
    req.add_header("Content-Type", "text/plain")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        log.error(f"ClickHouse HTTP {e.code}: {err_body[:400]}")
        raise


def wait_for_clickhouse(max_retries: int = 30, delay: int = 2) -> bool:
    for i in range(max_retries):
        try:
            ch_query("SELECT 1 FORMAT TabSeparated")
            return True
        except (URLError, OSError):
            log.info(f"Waiting for ClickHouse... ({i + 1}/{max_retries})")
            time.sleep(delay)
    return False


def ensure_migrations_table() -> None:
    ch_query(
        """
        CREATE TABLE IF NOT EXISTS acars.schema_migrations (
            version     String,
            name        String,
            applied_at  DateTime64(3) DEFAULT now64(3),
            checksum    String DEFAULT ''
        ) ENGINE = MergeTree()
        ORDER BY version
        """
    )


def get_applied_versions() -> set[str]:
    try:
        result = ch_query(
            "SELECT version FROM acars.schema_migrations FORMAT TabSeparated"
        )
        if not result.strip():
            return set()
        return {line.strip() for line in result.split("\n") if line.strip()}
    except Exception as e:
        log.error(f"Failed to read schema_migrations: {e}")
        return set()


def discover_migrations() -> list[tuple[str, str, Path]]:
    if not MIGRATIONS_DIR.is_dir():
        return []

    migrations = []
    for f in sorted(MIGRATIONS_DIR.iterdir()):
        match = MIGRATION_PATTERN.match(f.name)
        if match and f.is_file():
            migrations.append((match.group(1), f.stem, f))
    return migrations


def compute_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _has_sql(stmt: str) -> bool:
    """True if stmt has any non-comment, non-whitespace content."""
    for line in stmt.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            return True
    return False


def split_sql_statements(sql: str) -> list[str]:
    """Split SQL into statements on `;` while respecting `--` comments and `'...'` strings.

    Lifted from spectrum/migrate.py — handles two real bugs:
    a `;` inside a full-line comment, and a `;` inside an inline end-of-line
    comment trailing a CREATE TABLE column. Both chopped statements before
    the splitter became state-aware.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_string = False
    in_comment = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_comment:
            buf.append(ch)
            if ch == "\n":
                in_comment = False
            i += 1
            continue

        if in_string:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_string = False
            i += 1
            continue

        if ch == "'":
            in_string = True
            buf.append(ch)
        elif ch == "-" and nxt == "-":
            in_comment = True
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        elif ch == ";":
            stmt = "".join(buf).strip()
            if _has_sql(stmt):
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1

    stmt = "".join(buf).strip()
    if _has_sql(stmt):
        statements.append(stmt)
    return statements


def apply_migration(version: str, name: str, path: Path) -> None:
    sql = path.read_text()
    log.info(f"Applying migration {version}: {name}")

    for i, stmt in enumerate(split_sql_statements(sql), start=1):
        try:
            ch_query(stmt)
        except Exception as e:
            log.error(f"Migration {version} failed on statement {i}: {e}")
            log.error(f"Statement preview: {stmt[:200]}")
            raise

    checksum = compute_checksum(path)
    payload = json.dumps({"version": version, "name": name, "checksum": checksum})
    ch_query(
        "INSERT INTO acars.schema_migrations FORMAT JSONEachRow",
        data=payload,
    )
    log.info(f"Migration {version} applied successfully")


def run_migrations(dry_run: bool = False) -> int:
    ensure_migrations_table()
    applied = get_applied_versions()
    all_migrations = discover_migrations()
    pending = [(v, n, p) for v, n, p in all_migrations if v not in applied]

    if not pending:
        log.info("No pending migrations")
        return 0

    log.info(f"{len(pending)} pending migration(s)")
    for version, name, path in pending:
        if dry_run:
            log.info(f"  [dry-run] Would apply {version}: {name}")
        else:
            apply_migration(version, name, path)
    return len(pending)


def show_status() -> None:
    ensure_migrations_table()
    applied = get_applied_versions()
    all_migrations = discover_migrations()

    print(f"{'Version':<10} {'Name':<40} {'Status'}")
    print("-" * 60)
    for version, name, _ in all_migrations:
        status = "applied" if version in applied else "PENDING"
        print(f"{version:<10} {name:<40} {status}")
    if not all_migrations:
        print("(no migration files found)")


def main() -> None:
    if not wait_for_clickhouse():
        log.error("ClickHouse not available, cannot run migrations")
        sys.exit(1)

    if "--status" in sys.argv:
        show_status()
    elif "--dry-run" in sys.argv:
        run_migrations(dry_run=True)
    else:
        count = run_migrations()
        if count > 0:
            log.info(f"Applied {count} migration(s)")


if __name__ == "__main__":
    main()
