"""Database operations for the ingest service.

All SQL uses parameterized queries (psycopg3 %s style). Timestamps are UTC.
"""
from __future__ import annotations

import os

from datetime import datetime, timezone
from typing import Any, Callable

import psycopg

from .log import get_logger

log = get_logger(__name__)


# Hours of silence after which a reappearing sensor is considered "returned".
# A sensor that drops out and comes back is the classic dying-battery pattern,
# and the battery reading we have for it is by then up to a day stale — so the
# return is exactly the moment worth re-reading it.
RETURN_AFTER_HOURS = float(os.environ.get("RETURN_AFTER_HOURS", "6"))

# Set by the ingest service at startup: called with (mac, gap_hours) when a
# known sensor reappears after RETURN_AFTER_HOURS of silence. It lives here as
# a hook rather than an import because store.py must stay free of MQTT.
on_sensor_return: Callable[[str, float], None] | None = None


# Theengs `model_id` values allowed to CREATE a sensor row. The Pi's gateway
# decodes every BLE advertiser in radio range, not just ours, and ingest used
# to register any MAC that produced a payload -- so a neighbour's thermometer
# at -99 dBm became a "plant sensor", fired "New sensor discovered", and then
# sat in the fleet as a nameless MAC. Theengs publishes the decoded model on every message
# (`"model_id": "HHCCJCY01HHCC"`), which is the reliable discriminator -- RSSI
# is not, since a weak signal is also what a distant plant of ours looks like.
#
# This gates REGISTRATION ONLY. An already-known sensor keeps ingesting
# whatever it sends, so nothing here can break an existing device: notably the
# Pi's active-GATT battery collector, which publishes to the same topic with a
# minimal payload of its own construction.
AUTO_REGISTER_MODELS = frozenset(
    m.strip()
    for m in os.environ.get("AUTO_REGISTER_MODELS", "HHCCJCY01HHCC").split(",")
    if m.strip()
)


def ensure_sensor(
    conn: psycopg.Connection, mac: str, model_id: str | None = None
) -> int | None:
    """Return sensor.id for the given MAC, auto-registering if new — or
    None if the MAC belongs to a deactivated sensor, which the caller
    should treat as "drop this reading, nothing to write".

    On first insert: sets kind='bt', active=True, no name/location/collector
    (unassigned) and inserts an alert_events row with kind='new_sensor'.
    Existing *active* sensors get last_seen bumped. Existing *inactive*
    sensors are the durable way to silence a known-junk MAC (stray BLE
    device, never going to be a real plant sensor): deleting the row instead
    just makes ensure_sensor treat the next advertisement as "unknown" again
    and re-register + re-alert — deactivating is permanent because this
    branch is only reached once the row already exists.
    """
    now = datetime.now(tz=timezone.utc)
    mac = mac.upper()

    with conn.cursor() as cur:
        # Try to find existing sensor
        cur.execute(
            "SELECT id, active, last_seen FROM sensors WHERE mac_or_device_id = %s",
            (mac,),
        )
        row = cur.fetchone()

        if row is None:
            # Unknown MAC. Only a recognised plant-sensor model earns a row;
            # anything else is somebody else's hardware and is dropped without
            # trace. Deliberately not tombstoned as inactive either -- writing
            # a row per passing BLE device would just move the clutter.
            if model_id not in AUTO_REGISTER_MODELS:
                log.info(
                    f"event=auto_register_refused mac={mac} model_id={model_id!r} "
                    f"reason=not_a_plant_sensor"
                )
                conn.commit()
                return None
            # Auto-register unknown sensor
            cur.execute(
                """
                INSERT INTO sensors (kind, mac_or_device_id, active, first_seen, last_seen)
                VALUES ('bt', %s, true, %s, %s)
                RETURNING id
                """,
                (mac, now, now),
            )
            sensor_id: int = cur.fetchone()[0]
            log.info(f"auto-registered new sensor mac={mac} sensor_id={sensor_id}")

            # Insert alert event so telegram-bot can notify
            cur.execute(
                """
                INSERT INTO alert_events (time, sensor_id, kind, message, delivered)
                VALUES (%s, %s, 'new_sensor', %s, false)
                """,
                (now, sensor_id, f"New BT sensor discovered: {mac}"),
            )
        else:
            sensor_id, active, last_seen = row
            if not active:
                conn.commit()
                return None
            # A sensor coming back after a long silence gets an immediate
            # battery check. Detected here because this is the one place that
            # sees the old last_seen before it is overwritten.
            if last_seen is not None and on_sensor_return is not None:
                gap_hours = (now - last_seen).total_seconds() / 3600.0
                if gap_hours >= RETURN_AFTER_HOURS:
                    log.info(
                        f"event=sensor_returned mac={mac} sensor_id={sensor_id} "
                        f"gap_hours={gap_hours:.1f}"
                    )
                    try:
                        on_sensor_return(mac, gap_hours)
                    except Exception as exc:  # never let this break ingestion
                        log.warning(f"sensor-return hook failed mac={mac}: {exc!r}")
            # Update last_seen
            cur.execute(
                "UPDATE sensors SET last_seen = %s WHERE id = %s",
                (now, sensor_id),
            )

    conn.commit()
    return sensor_id


def write_bt_reading(
    conn: psycopg.Connection,
    sensor_id: int,
    bucket_time: datetime,
    fields: dict[str, Any],
) -> None:
    """Upsert a bt_readings row for (sensor_id, bucket_time).

    Only writes columns present in `fields`; skips columns not in the
    schema whitelist. Uses INSERT ... ON CONFLICT DO UPDATE for idempotency.
    bucket_time must be UTC timezone-aware.
    """
    # Whitelist of updatable columns (excludes time + sensor_id)
    ALLOWED = {"moisture", "temp_c", "light_lux", "conductivity_us_cm", "battery", "rssi"}
    present = {k: v for k, v in fields.items() if k in ALLOWED}

    if not present:
        log.warning(f"write_bt_reading: no recognized fields for sensor_id={sensor_id}")
        return

    cols = ["time", "sensor_id"] + list(present.keys())
    vals = [bucket_time, sensor_id] + list(present.values())
    placeholders = ", ".join(["%s"] * len(vals))
    col_list = ", ".join(cols)

    # Build the ON CONFLICT update clause: only update fields that are non-NULL
    update_parts = [
        f"{col} = COALESCE(EXCLUDED.{col}, bt_readings.{col})"
        for col in present.keys()
    ]
    update_clause = ", ".join(update_parts)

    sql = f"""
        INSERT INTO bt_readings ({col_list})
        VALUES ({placeholders})
        ON CONFLICT (time, sensor_id) DO UPDATE SET {update_clause}
    """
    with conn.cursor() as cur:
        cur.execute(sql, vals)
    conn.commit()
    log.info(
        f"wrote bt_reading sensor_id={sensor_id} "
        f"bucket={bucket_time.isoformat()} fields={list(present.keys())}"
    )
