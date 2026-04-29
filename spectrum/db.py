"""Shared ClickHouse HTTP client for the spectrum pipeline.

Replaces six near-identical urllib wrappers (scan_ingest, migrate,
feature_extractor, classifier, classifier_health, analysis/detect_compression).
All connection params come from `spectrum.config`.

ClickHouse HTTP API conventions:
- SELECT / CREATE / ALTER / etc. — SQL goes in the POST body
- INSERT with data — SQL goes in the URL (`?query=...`), rows in the POST body

`db.query(sql)` covers the first case; `db.query(sql, data=rows_jsonl)` and
`db.insert(table, rows)` cover the second. Errors raise HTTPError after
logging the response body so ClickHouse's complaint is visible.

Usage:

    from spectrum import db

    db.query("CREATE TABLE foo (id Int32) ENGINE = Memory")
    rows = db.query_rows("SELECT freq_hz FROM scans LIMIT 10")
    n = db.query_scalar("SELECT count() FROM scans")
    db.insert("peak_features", [{"freq_hz": 100_000_000, "duty": 0.5}])
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from config import config

log = logging.getLogger("spectrum.db")


def _url() -> str:
    return f"http://{config.CH_HOST}:{config.CH_PORT}/"


def _params() -> str:
    return (
        f"user={config.CH_USER}"
        f"&password={config.CH_PASSWORD}"
        f"&database={config.CH_DATABASE}"
    )


def query(sql: str, *, data: str | None = None, timeout: int = 60) -> str:
    """Execute a query via the ClickHouse HTTP API. Return the raw response text."""
    if data is not None:
        url = f"{_url()}?{_params()}&query={quote(sql)}"
        body = data.encode("utf-8")
    else:
        url = f"{_url()}?{_params()}"
        body = sql.encode("utf-8")
    req = Request(url, data=body)
    req.add_header("Content-Type", "text/plain")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        log.error(f"ClickHouse HTTP {e.code}: {err_body[:300]}")
        raise


def query_rows(sql: str, *, timeout: int = 60) -> list[dict[str, Any]]:
    """SELECT and parse JSONEachRow lines into dicts. Sql must NOT include FORMAT."""
    text = query(sql + " FORMAT JSONEachRow", timeout=timeout)
    return [json.loads(line) for line in text.splitlines() if line]


def query_scalar(sql: str, *, timeout: int = 60) -> Any:
    """Return the first column of the first row of a SELECT, or None if empty."""
    rows = query_rows(sql, timeout=timeout)
    if not rows:
        return None
    return next(iter(rows[0].values()), None)


def insert(table: str, rows: Iterable[dict[str, Any]], *, timeout: int = 60) -> None:
    """Batch-insert rows into `table` via JSONEachRow.

    Each row is a dict; ClickHouse maps dict keys to column names. Empty
    iterables are no-ops (no request sent).
    """
    body_lines = [json.dumps(row) for row in rows]
    if not body_lines:
        return
    body = "\n".join(body_lines)
    query(f"INSERT INTO {table} FORMAT JSONEachRow", data=body, timeout=timeout)
