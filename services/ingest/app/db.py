"""psycopg3 connection helper.

Supports DATABASE_URL (takes priority) or discrete PGHOST/PGPORT/PGUSER/
PGPASSWORD/PGDATABASE env vars. All timestamps must be UTC timezone-aware.
"""
from __future__ import annotations

import os
import time
from collections.abc import Generator
from contextlib import contextmanager

import psycopg

from .log import get_logger

log = get_logger(__name__)

_DEFAULT_PORT = 5432
_BACKOFF_CAP = 60


def _conninfo() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", str(_DEFAULT_PORT))
    user = os.environ.get("PGUSER", "postgres")
    password = os.environ.get("PGPASSWORD", "")
    dbname = os.environ.get("PGDATABASE", "canopy")
    return f"host={host} port={port} user={user} password={password} dbname={dbname}"


def connect_with_backoff() -> psycopg.Connection:
    """Return a psycopg3 connection, retrying with capped exponential backoff."""
    delay = 1
    while True:
        try:
            conn = psycopg.connect(_conninfo(), autocommit=False)
            log.info("db connected")
            return conn
        except psycopg.OperationalError as exc:
            log.warning(f"db connect failed: {exc!r} — retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, _BACKOFF_CAP)


@contextmanager
def get_conn() -> Generator[psycopg.Connection, None, None]:
    """Context manager yielding a connection; reconnects on failure."""
    conn = connect_with_backoff()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass
