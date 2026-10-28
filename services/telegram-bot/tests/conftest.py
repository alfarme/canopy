"""
Shared fixtures for integration tests.

Provides:
- FakeAsyncConnection: in-memory async DB stub using SQLite (dict_row style)
- fake_update / fake_context: Telegram Update/Context stubs
- httpx monkeypatching for PlantNet/OpenPlantbook
"""
from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ------------------------------------------------------------------ DB Stub

# In-memory SQLite schema matching the real Postgres schema (minimal subset
# needed by the bot handlers). Differences from Postgres:
# - No timescaledb extensions
# - SERIAL → INTEGER PRIMARY KEY AUTOINCREMENT
# - TIMESTAMPTZ → TEXT
# - ILIKE → LIKE (SQLite is case-insensitive for ASCII by default)
# - %s → ? placeholders (handled by the cursor wrapper)

_SCHEMA = """
CREATE TABLE sensors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'bt',
    mac_or_device_id TEXT UNIQUE NOT NULL,
    name TEXT,
    location TEXT,
    collector TEXT,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE plants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    species TEXT,
    opb_pid TEXT,
    gbif_id TEXT,
    threshold_source TEXT NOT NULL DEFAULT 'default',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE plant_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_id INTEGER NOT NULL REFERENCES sensors(id),
    plant_id INTEGER NOT NULL REFERENCES plants(id),
    assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
    released_at TEXT
);

CREATE TABLE thresholds (
    plant_id INTEGER PRIMARY KEY REFERENCES plants(id),
    min_soil_moist INTEGER,
    max_soil_moist INTEGER,
    min_soil_ec INTEGER,
    max_soil_ec INTEGER,
    min_light_lux INTEGER,
    max_light_lux INTEGER,
    min_temp INTEGER,
    max_temp INTEGER,
    hysteresis_moist INTEGER NOT NULL DEFAULT 5,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE bt_readings (
    time TEXT NOT NULL,
    sensor_id INTEGER NOT NULL REFERENCES sensors(id),
    moisture REAL,
    temp_c REAL,
    light_lux INTEGER,
    conductivity_us_cm INTEGER,
    battery INTEGER,
    rssi INTEGER
);

CREATE TABLE alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL DEFAULT (datetime('now')),
    sensor_id INTEGER REFERENCES sensors(id),
    plant_id INTEGER REFERENCES plants(id),
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE alert_silences (
    sensor_id INTEGER NOT NULL REFERENCES sensors(id),
    kind TEXT NOT NULL,
    silenced_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (sensor_id, kind)
);

CREATE TABLE identify_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL DEFAULT (datetime('now')),
    file_path TEXT NOT NULL,
    species TEXT,
    score REAL,
    sensor_id INTEGER REFERENCES sensors(id),
    sensor_name TEXT,
    plant_name TEXT,
    plant_id INTEGER REFERENCES plants(id)
);

CREATE VIEW sensor_labels AS
SELECT s.id AS sensor_id,
       COALESCE(p.display_name || ' (' || s.name || ')', s.name, s.mac_or_device_id) AS label
FROM sensors s
LEFT JOIN plant_assignments pa ON pa.sensor_id = s.id AND pa.released_at IS NULL
LEFT JOIN plants p ON p.id = pa.plant_id;
"""


class _DictRow(dict):
    """Row that supports both dict-style and attribute access."""
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def _coerce_timestamps(row_dict: dict) -> dict:
    """Convert ISO-format timestamp strings to datetime objects (mimics psycopg behavior)."""
    from datetime import datetime, timezone
    result = {}
    for k, v in row_dict.items():
        if isinstance(v, str) and ('T' in v or (len(v) > 10 and v[4] == '-' and v[10] == ' ')):
            try:
                # Try parsing ISO format
                dt = datetime.fromisoformat(v)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                result[k] = dt
                continue
            except (ValueError, IndexError):
                pass
        result[k] = v
    return result


class FakeCursor:
    """Async cursor stub backed by a SQLite connection.

    Translates psycopg-style %s placeholders to SQLite ? placeholders,
    and wraps results as dicts. Handles RETURNING via a separate SELECT.
    """

    def __init__(self, sqlite_conn: sqlite3.Connection):
        self._conn = sqlite_conn
        self._rows: list[_DictRow] = []

    async def execute(self, query: str, params: tuple = ()) -> None:
        # Translate %s to ?
        q = query.replace("%s", "?")
        # Translate ILIKE to LIKE (SQLite LIKE is case-insensitive for ASCII)
        q = q.replace(" ILIKE ", " LIKE ")
        # Translate PostgreSQL DISTINCT ON (...) to SQLite GROUP BY
        import re
        distinct_on_match = re.search(r'SELECT\s+DISTINCT\s+ON\s*\(([^)]+)\)', q, re.IGNORECASE)
        if distinct_on_match:
            col_expr = distinct_on_match.group(1).strip()
            # Remove the DISTINCT ON clause
            q = re.sub(r'DISTINCT\s+ON\s*\([^)]+\)\s*', '', q, flags=re.IGNORECASE)
            # Add GROUP BY at the end (before ORDER BY if present)
            order_match = re.search(r'ORDER BY', q, re.IGNORECASE)
            if order_match:
                q = q[:order_match.start()] + f"GROUP BY {col_expr} " + q[order_match.start():]
            else:
                q += f" GROUP BY {col_expr}"
        # Translate NULLS LAST (SQLite doesn't support it, but default sort puts NULLs first)
        q = re.sub(r'\bNULLS\s+LAST\b', '', q, flags=re.IGNORECASE)
        # Translate boolean true/false to 1/0
        q = re.sub(r'\btrue\b', '1', q, flags=re.IGNORECASE)
        q = re.sub(r'\bfalse\b', '0', q, flags=re.IGNORECASE)

        # Handle RETURNING clause (SQLite doesn't support it natively in older versions)
        returning_cols = None
        if "RETURNING" in q.upper():
            parts = q.rsplit("RETURNING", 1)
            q = parts[0].strip()
            returning_cols = [c.strip() for c in parts[1].split(",")]

        cursor = self._conn.execute(q, params)
        self._conn.commit()

        if returning_cols:
            if q.strip().upper().startswith("INSERT"):
                # For INSERT RETURNING, get the last inserted row
                rowid = cursor.lastrowid
                table = self._extract_table(q, "INSERT INTO")
                sel = f"SELECT {', '.join(returning_cols)} FROM {table} WHERE rowid = ?"
                cur2 = self._conn.execute(sel, (rowid,))
                row = cur2.fetchone()
                if row:
                    self._rows = [_DictRow(_coerce_timestamps(dict(zip(returning_cols, row))))]
                else:
                    self._rows = []
            elif q.strip().upper().startswith(("UPDATE", "DELETE")):
                # For UPDATE/DELETE RETURNING: if rows were modified, return a
                # synthetic row. Callers (unassign, unsilence) only check
                # fetchone() truthiness, never the row's actual contents.
                if cursor.rowcount > 0:
                    self._rows = [_DictRow({col: 1 for col in returning_cols})]
                else:
                    self._rows = []
            else:
                self._rows = []
        else:
            if cursor.description:
                cols = [d[0] for d in cursor.description]
                self._rows = [_DictRow(_coerce_timestamps(dict(zip(cols, r)))) for r in cursor.fetchall()]
            else:
                self._rows = []

    def _extract_table(self, query: str, keyword: str) -> str:
        """Extract table name from INSERT INTO table or UPDATE table."""
        upper = query.upper()
        idx = upper.find(keyword.upper())
        if idx < 0:
            return ""
        rest = query[idx + len(keyword):].strip()
        # table name is the first word
        return rest.split()[0].strip("(")

    async def fetchone(self) -> _DictRow | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[_DictRow]:
        return self._rows


class FakeAsyncConnection:
    """Async DB connection stub using an in-memory SQLite database.

    Supports:
    - async with conn.cursor() as cur: ...
    - await conn.commit()
    - await conn.close()
    """

    def __init__(self):
        self._sqlite = sqlite3.connect(":memory:")
        self._sqlite.executescript(_SCHEMA)

    @asynccontextmanager
    async def cursor(self):
        cur = FakeCursor(self._sqlite)
        yield cur

    async def commit(self):
        self._sqlite.commit()

    async def close(self):
        self._sqlite.close()

    # Test helpers for seeding data
    def seed_sensor(self, id: int, name: str, mac: str = None, location: str = "Balcony", active: bool = True, kind: str = "bt"):
        mac = mac or f"AA:BB:CC:DD:EE:{id:02X}"
        self._sqlite.execute(
            "INSERT INTO sensors (id, kind, mac_or_device_id, name, location, active) VALUES (?,?,?,?,?,?)",
            (id, kind, mac, name, location, int(active)),
        )
        self._sqlite.commit()

    def seed_plant(self, id: int, display_name: str, species: str = None, threshold_source: str = "default"):
        self._sqlite.execute(
            "INSERT INTO plants (id, display_name, species, opb_pid, threshold_source) VALUES (?,?,?,?,?)",
            (id, display_name, species, (species or "").lower(), threshold_source),
        )
        self._sqlite.commit()

    def seed_assignment(self, sensor_id: int, plant_id: int):
        self._sqlite.execute(
            "INSERT INTO plant_assignments (sensor_id, plant_id, assigned_at) VALUES (?,?,?)",
            (sensor_id, plant_id, datetime.now(tz=timezone.utc).isoformat()),
        )
        self._sqlite.commit()

    def count(self, table: str, sensor_id: int | None = None) -> int:
        sql = f"SELECT count(*) FROM {table}"
        args: tuple = ()
        if sensor_id is not None:
            sql += " WHERE sensor_id = ?"
            args = (sensor_id,)
        return self._sqlite.execute(sql, args).fetchone()[0]

    def sensor_name(self, sensor_id: int) -> str | None:
        row = self._sqlite.execute("SELECT name FROM sensors WHERE id = ?", (sensor_id,)).fetchone()
        return None if row is None else row[0]

    def sensor_active(self, sensor_id: int) -> bool:
        row = self._sqlite.execute("SELECT active FROM sensors WHERE id = ?", (sensor_id,)).fetchone()
        return None if row is None else bool(row[0])

    def seed_alert(self, sensor_id: int, kind: str, message: str = "test alert", delivered: bool = False):
        self._sqlite.execute(
            "INSERT INTO alert_events (sensor_id, plant_id, kind, message, delivered) VALUES (?,NULL,?,?,?)",
            (sensor_id, kind, message, int(delivered)),
        )
        self._sqlite.commit()

    def seed_silence(self, sensor_id: int, kind: str = "stuck"):
        self._sqlite.execute(
            "INSERT INTO alert_silences (sensor_id, kind) VALUES (?,?)",
            (sensor_id, kind),
        )
        self._sqlite.commit()

    def count_silences(self) -> int:
        cur = self._sqlite.execute("SELECT COUNT(*) FROM alert_silences")
        return cur.fetchone()[0]

    def seed_thresholds(self, plant_id: int, **kwargs):
        from app.api_clients import DEFAULT_THRESHOLDS
        t = DEFAULT_THRESHOLDS.copy()
        t.update(kwargs)
        self._sqlite.execute(
            """INSERT INTO thresholds (plant_id, min_soil_moist, max_soil_moist,
                min_soil_ec, max_soil_ec, min_light_lux, max_light_lux,
                min_temp, max_temp) VALUES (?,?,?,?,?,?,?,?,?)""",
            (plant_id, t["min_soil_moist"], t["max_soil_moist"],
             t["min_soil_ec"], t["max_soil_ec"],
             t["min_light_lux"], t["max_light_lux"],
             t["min_temp"], t["max_temp"]),
        )
        self._sqlite.commit()

    def seed_reading(self, sensor_id: int, moisture=50.0, temp_c=22.0,
                     light_lux=1000, conductivity=500, battery=85):
        self._sqlite.execute(
            """INSERT INTO bt_readings (time, sensor_id, moisture, temp_c, light_lux,
                conductivity_us_cm, battery) VALUES (?,?,?,?,?,?,?)""",
            ("2026-07-19T10:00:00+00:00", sensor_id,
             moisture, temp_c, light_lux, conductivity, battery),
        )
        self._sqlite.commit()

    def get_plant(self, plant_id: int) -> dict | None:
        cur = self._sqlite.execute("SELECT * FROM plants WHERE id = ?", (plant_id,))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        return dict(zip(cols, row)) if row else None

    def get_assignments(self, sensor_id: int) -> list[dict]:
        cur = self._sqlite.execute(
            "SELECT * FROM plant_assignments WHERE sensor_id = ? AND released_at IS NULL",
            (sensor_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_thresholds(self, plant_id: int) -> dict | None:
        cur = self._sqlite.execute("SELECT * FROM thresholds WHERE plant_id = ?", (plant_id,))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        return dict(zip(cols, row)) if row else None

    def count_plants(self) -> int:
        cur = self._sqlite.execute("SELECT COUNT(*) FROM plants")
        return cur.fetchone()[0]

    def seed_identify_photo(self, id: int, file_path: str = "/data/identify-photos/x.jpg",
                            species: str = None, score: float = None, plant_id: int = None,
                            sensor_id: int = None) -> int:
        self._sqlite.execute(
            "INSERT INTO identify_photos (id, file_path, species, score, plant_id, sensor_id) "
            "VALUES (?,?,?,?,?,?)",
            (id, file_path, species, score, plant_id, sensor_id),
        )
        self._sqlite.commit()
        return id

    def get_identify_photos(self) -> list[dict]:
        cur = self._sqlite.execute("SELECT * FROM identify_photos")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def seed_alert_with_plant(self, sensor_id: int, plant_id: int, kind: str, message: str = "test alert"):
        self._sqlite.execute(
            "INSERT INTO alert_events (sensor_id, plant_id, kind, message, delivered) VALUES (?,?,?,?,0)",
            (sensor_id, plant_id, kind, message),
        )
        self._sqlite.commit()

    def get_alert_events(self) -> list[dict]:
        cur = self._sqlite.execute("SELECT * FROM alert_events")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_sensor(self, sensor_id: int) -> dict | None:
        cur = self._sqlite.execute("SELECT * FROM sensors WHERE id = ?", (sensor_id,))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        return dict(zip(cols, row)) if row else None


# ------------------------------------------------------------------ Telegram stubs

def make_fake_update(text: str = "", user_id: int = 12345, chat_id: int = 99) -> MagicMock:
    """Create a fake telegram.Update with a message containing the given text."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id

    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.message.reply_media_group = AsyncMock()
    update.message.chat_id = chat_id

    # For /identify: no photo by default
    update.message.photo = []
    update.message.reply_to_message = None
    update.message.caption = None

    return update


def make_fake_callback_query(data: str, user_id: int = 12345) -> MagicMock:
    """Create a fake callback query (inline keyboard press)."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id

    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()

    # No regular message on callback updates
    update.message = None

    return update


def make_fake_context(user_data: dict = None) -> MagicMock:
    """Create a fake telegram.ext.ContextTypes.DEFAULT_TYPE context."""
    context = MagicMock()
    context.user_data = user_data if user_data is not None else {}
    return context


@pytest.fixture(autouse=True)
def _reset_token_cache():
    """Reset the OpenPlantbook token cache between tests.

    Prevents order-dependent flakiness: a test that populates the cache with
    a valid token must not leak that state to a later test that expects
    'missing credentials → None'.
    """
    import app.api_clients as api_mod
    yield
    api_mod._token_cache.update({"access_token": None, "expires_at": 0.0})


@pytest.fixture
def db():
    """Provide a fresh in-memory DB for each test."""
    return FakeAsyncConnection()


@pytest.fixture
def admin_env(monkeypatch):
    """Set up admin user ID."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_ID", "12345")
