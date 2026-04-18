#!/usr/bin/env python3
"""
ClickHouse schema migration runner for the spectrum pipeline.

Scans clickhouse/migrations/ for numbered SQL files (NNN_description.sql),
tracks which have been applied in a schema_migrations table, and runs
any pending migrations in order.

Designed to run at container startup before the scanner pipeline starts.
Safe to run repeatedly — already-applied migrations are skipped.

Usage:
    python3 migrate.py                 # run pending migrations
    python3 migrate.py --status        # show migration status
    python3 migrate.py --dry-run       # show what would run without applying
"""

import os
import sys
import re
import time
import logging
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen, Request
from urllib.error import URLError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("migrate")

CH_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CH_DB = os.environ.get("CLICKHOUSE_DB", "spectrum")
CH_USER = os.environ.get("CLICKHOUSE_USER", "spectrum")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "spectrum_local")
CH_URL = f"http://{CH_HOST}:{CH_PORT}"

MIGRATIONS_DIR = Path(__file__).parent / "clickhouse" / "migrations"
MIGRATION_PATTERN = re.compile(r"^(\d{3})_.+\.sql$")


def ch_query(query: str, data: str = "") -> str:
    """Execute a ClickHouse query via HTTP API."""
    params = f"user={CH_USER}&password={CH_PASSWORD}&database={CH_DB}"
    if data:
        url = f"{CH_URL}/?{params}&query={quote(query)}"
        body = data.encode()
    else:
        url = f"{CH_URL}/?{params}"
        body = query.encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    req = Request(url, data=body, headers=headers)
    resp = urlopen(req, timeout=30)
    return resp.read().decode().strip()


def wait_for_clickhouse(max_retries: int = 30, delay: int = 2) -> bool:
    """Wait until ClickHouse is accepting queries."""
    for i in range(max_retries):
        try:
            ch_query("SELECT 1 FORMAT TabSeparated")
            return True
        except Exception:
            log.info(f"Waiting for ClickHouse... ({i + 1}/{max_retries})")
            time.sleep(delay)
    return False


def ensure_migrations_table():
    """Create the schema_migrations tracking table if it doesn't exist."""
    ch_query("""
        CREATE TABLE IF NOT EXISTS spectrum.schema_migrations (
            version     String,
            name        String,
            applied_at  DateTime64(3) DEFAULT now64(3),
            checksum    String DEFAULT ''
        ) ENGINE = MergeTree()
        ORDER BY version
    """)


def get_applied_versions() -> set[str]:
    """Return the set of migration versions already applied."""
    try:
        result = ch_query(
            "SELECT version FROM spectrum.schema_migrations FORMAT TabSeparated"
        )
        if not result:
            return set()
        return {line.strip() for line in result.split("\n") if line.strip()}
    except Exception as e:
        log.error(f"Failed to read schema_migrations: {e}")
        return set()


def discover_migrations() -> list[tuple[str, str, Path]]:
    """Find all migration files, return sorted list of (version, name, path)."""
    if not MIGRATIONS_DIR.is_dir():
        return []

    migrations = []
    for f in sorted(MIGRATIONS_DIR.iterdir()):
        match = MIGRATION_PATTERN.match(f.name)
        if match and f.is_file():
            version = match.group(1)
            name = f.stem  # e.g. "001_add_clipping_columns"
            migrations.append((version, name, f))

    return migrations


def compute_checksum(path: Path) -> str:
    """Simple checksum: hex of hash of file contents."""
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def split_sql_statements(sql: str) -> list[str]:
    """Split a SQL file into statements on ; while respecting comments and strings.

    - Full-line `-- comments` are stripped first so any ; inside them is invisible.
    - Semicolons inside single-quoted strings are not treated as terminators;
      '' (doubled single quote) is the SQL-standard embedded-quote escape.

    Both bugs were discovered applying migration 003 (2026-04-18): a ; inside
    a comment chopped the statement list, and a ; inside an allocations `notes`
    string chopped an INSERT in half.
    """
    sql = "\n".join(l for l in sql.split("\n") if l.strip() and not l.strip().startswith("--"))
    statements = []
    buf = []
    in_string = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            if in_string and i + 1 < len(sql) and sql[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_string = not in_string
            buf.append(ch)
        elif ch == ";" and not in_string:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    stmt = "".join(buf).strip()
    if stmt:
        statements.append(stmt)
    return statements


def apply_migration(version: str, name: str, path: Path):
    """Run a single migration file and record it."""
    sql = path.read_text()
    log.info(f"Applying migration {version}: {name}")

    statements = split_sql_statements(sql)
    for i, stmt in enumerate(statements):
        try:
            ch_query(stmt)
        except Exception as e:
            log.error(f"Migration {version} failed on statement {i + 1}: {e}")
            log.error(f"Statement: {stmt[:200]}")
            raise

    # Record successful migration
    checksum = compute_checksum(path)
    ch_query(
        "INSERT INTO spectrum.schema_migrations FORMAT JSONEachRow",
        f'{{"version": "{version}", "name": "{name}", "checksum": "{checksum}"}}'
    )
    log.info(f"Migration {version} applied successfully")


def run_migrations(dry_run: bool = False) -> int:
    """Run all pending migrations. Returns count of migrations applied."""
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


def show_status():
    """Print migration status table."""
    ensure_migrations_table()
    applied = get_applied_versions()
    all_migrations = discover_migrations()

    print(f"{'Version':<10} {'Name':<40} {'Status'}")
    print("-" * 60)
    for version, name, path in all_migrations:
        status = "applied" if version in applied else "PENDING"
        print(f"{version:<10} {name:<40} {status}")

    if not all_migrations:
        print("(no migration files found)")


def main():
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
