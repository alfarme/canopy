"""
DB query helpers — thin wrappers over psycopg3 that return plain dicts/lists.
All SQL uses parameterized queries; never string-format user/DB values.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row


def fetch_active_assignments(conn: psycopg.Connection) -> list[dict]:
    """
    Return all currently-active plant assignments with plant display_name.
    """
    sql = """
        SELECT pa.sensor_id, pa.plant_id, p.display_name AS plant_name
        FROM plant_assignments pa
        JOIN plants p ON p.id = pa.plant_id
        WHERE pa.released_at IS NULL
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql)
        return cur.fetchall()


def fetch_thresholds(conn: psycopg.Connection) -> dict[int, dict]:
    """Return {plant_id: threshold_dict}."""
    sql = """
        SELECT plant_id, min_soil_moist, max_soil_moist,
               min_soil_ec, max_soil_ec,
               min_light_lux, max_light_lux,
               min_temp, max_temp, hysteresis_moist
        FROM thresholds
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql)
        return {row["plant_id"]: row for row in cur.fetchall()}


def fetch_recent_bt_readings(
    conn: psycopg.Connection,
    sensor_id: int,
    limit: int = 10,
    since: datetime | None = None,
) -> list[dict]:
    """Return recent bt_readings rows for a sensor, newest first.

    If `since` is given, returns every row from that time forward (used by
    the stuck-value check, which needs a wide enough time window to measure
    duration, not just a fixed sample count) instead of applying `limit`.
    """
    if since is not None:
        sql = """
            SELECT time, sensor_id, moisture, temp_c, light_lux,
                   conductivity_us_cm, battery, rssi
            FROM bt_readings
            WHERE sensor_id = %s AND time >= %s
            ORDER BY time DESC
        """
        params: tuple = (sensor_id, since)
    else:
        sql = """
            SELECT time, sensor_id, moisture, temp_c, light_lux,
                   conductivity_us_cm, battery, rssi
            FROM bt_readings
            WHERE sensor_id = %s
            ORDER BY time DESC
            LIMIT %s
        """
        params = (sensor_id, limit)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_active_sensors(conn: psycopg.Connection) -> list[dict]:
    """Return all active sensors."""
    sql = """
        SELECT id, kind, mac_or_device_id, name, location
        FROM sensors
        WHERE active = true
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql)
        return cur.fetchall()


def fetch_last_alert_times(
    conn: psycopg.Connection,
) -> dict[tuple[int, str], datetime]:
    """
    Latest alert_events.time per (sensor_id, kind).

    Used to seed the cooldown gate at startup so a process restart does not
    re-fire every currently-active condition (see engine.within_cooldown).
    """
    sql = """
        SELECT sensor_id, kind, max(time) AS last_time
        FROM alert_events
        WHERE sensor_id IS NOT NULL
        GROUP BY sensor_id, kind
    """
    out: dict[tuple[int, str], datetime] = {}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql)
        for row in cur.fetchall():
            out[(row["sensor_id"], row["kind"])] = row["last_time"]
    return out


def insert_alert_event(
    conn: psycopg.Connection,
    sensor_id: int | None,
    plant_id: int | None,
    kind: str,
    message: str,
) -> None:
    """Insert one alert_events row. Idempotency via dedup in engine logic."""
    sql = """
        INSERT INTO alert_events (time, sensor_id, plant_id, kind, message)
        VALUES (now(), %s, %s, %s, %s)
    """
    with conn.cursor() as cur:
        cur.execute(sql, (sensor_id, plant_id, kind, message))
    conn.commit()


def fetch_silenced_alerts(conn: psycopg.Connection) -> set[tuple[int, str]]:
    """Return the set of (sensor_id, kind) pairs permanently silenced via
    /silence in the telegram bot. Silenced alerts are still evaluated (so
    Grafana history stays intact) but never inserted as new alert_events."""
    sql = "SELECT sensor_id, kind FROM alert_silences"
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql)
        return {(row["sensor_id"], row["kind"]) for row in cur.fetchall()}
