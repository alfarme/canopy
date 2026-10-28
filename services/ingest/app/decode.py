"""
Theengs Gateway MQTT payload → bt_readings field mapping.

Theengs publishes ONE metric per BLE advertisement under canopy/ble/<id>.
Each message contains 'id' (MAC) + rssi + zero-or-more of: tempc, moi, lux, fer.

Field mapping (Theengs → bt_readings schema):
  tempc  → temp_c               (°C, float)
  moi    → moisture             (%, float)
  lux    → light_lux            (lux, int)
  fer    → conductivity_us_cm   (µS/cm, int)
  rssi   → rssi                 (dBm, int)
  batt   → battery              (%, int)

These field names are verified from:
  docs/research/r2-miflora-collector.md §1 (passive BLE advertisement JSON)
  Theengs Decoder docs for HHCCJCY01HHCC.

Note on `batt`: this key never appears on the MiFlora passive path — the
HHCCJCY01HHCC decoder emits only temperature/moisture/fertility/luminance,
and battery sits behind GATT handle 0x38, which passive scanning never reads.
It is mapped anyway so an active-connect collector publishing to the same
topic lands in the column. `batt` (not `battery`) is Theengs' spelling:
    "batt": {"unit": "%", "name": "battery"}
        — theengs/decoder src/devices/LYWSD03MMC_json.h
"""
from __future__ import annotations

from typing import Any

# Mapping from Theengs field name → bt_readings column name
FIELD_MAP: dict[str, str] = {
    "tempc": "temp_c",
    "moi": "moisture",
    "lux": "light_lux",
    "fer": "conductivity_us_cm",
    "rssi": "rssi",
    "batt": "battery",
}

# Fields that should be stored as integers in bt_readings
INT_FIELDS = {"light_lux", "conductivity_us_cm", "battery", "rssi"}


def decode_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Decode a Theengs Gateway MQTT payload dict into a partial bt_readings row.

    Returns a dict with:
      - 'mac': the sensor MAC address (from 'id' field)
      - zero or more of: temp_c, moisture, light_lux, conductivity_us_cm,
        battery, rssi
    Raises ValueError if 'id' field is missing.
    Unknown/extra Theengs fields are silently ignored.
    """
    if "id" not in raw:
        raise ValueError("Theengs payload missing 'id' (MAC) field")

    result: dict[str, Any] = {"mac": raw["id"].upper()}

    # Carried through so ensure_sensor can refuse to auto-register a device
    # that is not a plant sensor. Not a bt_readings column — write_bt_reading's
    # whitelist drops it, and the flusher pops it before writing anyway.
    if raw.get("model_id"):
        result["model_id"] = str(raw["model_id"])

    for theengs_field, db_col in FIELD_MAP.items():
        if theengs_field in raw:
            val = raw[theengs_field]
            if val is None:
                continue
            try:
                if db_col in INT_FIELDS:
                    result[db_col] = int(val)
                else:
                    result[db_col] = float(val)
            except (TypeError, ValueError):
                # Skip unparseable values rather than crashing
                pass

    return result


def merge_partials(partials: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge a list of partial bt_readings rows for the same MAC + bucket.

    Later values in the list override earlier ones for the same field.
    All non-MAC fields are optional; only present fields are merged.
    'mac' is taken from the first entry.
    """
    if not partials:
        raise ValueError("merge_partials called with empty list")

    merged: dict[str, Any] = {}
    for partial in partials:
        merged.update(partial)
    return merged


def bucket_key(mac: str, ts_seconds: float, bucket_minutes: int) -> tuple[str, int]:
    """Return (mac, bucket_start_seconds) for a given timestamp and bucket size.

    Buckets are aligned to UTC midnight (epoch % bucket_seconds == 0).
    """
    bucket_seconds = bucket_minutes * 60
    bucket_start = int(ts_seconds // bucket_seconds) * bucket_seconds
    return (mac.upper(), bucket_start)
