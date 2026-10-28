"""
psycopg3 connection helper — duplicated per service (no shared runtime lib).
Supports DATABASE_URL (wins) or discrete PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE.
"""
from __future__ import annotations
import os
import logging
import asyncio
from typing import AsyncGenerator

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)


def _build_connstr() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER", "canopy")
    password = os.environ.get("PGPASSWORD", "")
    dbname = os.environ.get("PGDATABASE", "canopy")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


async def connect_with_backoff(max_delay: float = 60.0) -> psycopg.AsyncConnection:
    """Connect to Postgres with exponential backoff, capped at max_delay."""
    delay = 1.0
    connstr = _build_connstr()
    while True:
        try:
            conn = await psycopg.AsyncConnection.connect(
                connstr, row_factory=dict_row, autocommit=False
            )
            log.info("event=db_connected")
            return conn
        except Exception as exc:
            log.warning("event=db_connect_failed error=%r retry_in=%.1fs", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)
