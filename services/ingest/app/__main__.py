"""Ingest service entrypoint.

Subscribes to canopy/ble/# via MQTT, decodes Theengs Gateway JSON
for HHCCJCY01 sensors, merges per-MAC per-bucket partials, and writes
bt_readings rows to TimescaleDB. Auto-registers unknown MACs.

Run with: python -m app
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt

from .db import connect_with_backoff
from .decode import decode_payload
from .log import get_logger
from .merger import BucketMerger
from . import store
from .store import ensure_sensor, write_bt_reading

log = get_logger("ingest")

MQTT_TOPIC = "canopy/ble/#"
# Command topic the Pi's battery listener subscribes to. Publishing a MAC here
# asks for an immediate active-GATT battery read on that sensor.
BATTERY_CMD_TOPIC = "canopy/cmd/battery"
BUCKET_MINUTES = int(os.environ.get("BUCKET_MINUTES", "30"))

# MQTT connection settings
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")

_shutdown = False


def make_flusher(conn_factory):  # type: ignore[no-untyped-def]
    """Return a flusher closure that writes merged partials to DB."""

    def flusher(mac: str, bucket_time: datetime, fields: dict[str, Any]) -> None:
        conn = conn_factory()
        # model_id rides along on the decoded partials to reach ensure_sensor;
        # it is not a reading, so it never goes to write_bt_reading.
        model_id = fields.pop("model_id", None)
        sensor_id = ensure_sensor(conn, mac, model_id=model_id)
        if sensor_id is None:
            # Deactivated (known-junk) MAC, or an unknown MAC whose model is
            # not a plant sensor — drop the reading, don't store it.
            return
        write_bt_reading(conn, sensor_id, bucket_time, fields)

    return flusher


class IngestService:
    def __init__(self) -> None:
        self._conn = None
        self._merger: BucketMerger | None = None
        self._client: mqtt.Client | None = None

    def _get_conn(self):  # type: ignore[no-untyped-def]
        if self._conn is None or self._conn.closed:
            self._conn = connect_with_backoff()
        return self._conn

    def _on_connect(self, client, userdata, flags, rc, properties=None) -> None:  # type: ignore[no-untyped-def]
        if rc == 0:
            log.info(f"mqtt connected host={MQTT_HOST} port={MQTT_PORT}")
            client.subscribe(MQTT_TOPIC, qos=1)
            log.info(f"subscribed topic={MQTT_TOPIC}")
        else:
            log.error(f"mqtt connect failed rc={rc}")

    def _on_disconnect(self, client, userdata, disconnect_flags, rc, properties=None) -> None:  # type: ignore[no-untyped-def]
        # CallbackAPIVersion.VERSION2's on_disconnect signature carries an extra
        # disconnect_flags argument between userdata and the reason code — a
        # 4-positional-arg version of this callback throws TypeError on every
        # disconnect (including the one client.disconnect() triggers during
        # graceful shutdown, right after flush_all() runs).
        log.warning(f"mqtt disconnected rc={rc} — paho will reconnect")

    def _on_message(self, client, userdata, msg) -> None:  # type: ignore[no-untyped-def]
        topic = msg.topic
        try:
            raw = json.loads(msg.payload)
        except json.JSONDecodeError as exc:
            log.warning(f"invalid JSON on topic={topic}: {exc!r}")
            return

        try:
            decoded = decode_payload(raw)
        except ValueError as exc:
            log.warning(f"decode failed topic={topic}: {exc!r}")
            return

        log.info(f"received mac={decoded.get('mac')} fields={list(decoded.keys())}")
        assert self._merger is not None
        self._merger.ingest(decoded)

    def _request_battery(self, mac: str, gap_hours: float) -> None:
        """Ask the Pi to read this sensor's battery now.

        Fire-and-forget on purpose: the reading comes back through the normal
        BLE topic like any other, and a failed publish is not worth interrupting
        ingestion for — the daily job will pick the sensor up anyway.
        """
        if self._client is None:
            return
        try:
            self._client.publish(BATTERY_CMD_TOPIC, mac, qos=1)
            log.info(f"event=battery_requested mac={mac} gap_hours={gap_hours:.1f}")
        except Exception as exc:
            log.warning(f"battery request publish failed mac={mac}: {exc!r}")

    def run(self) -> None:
        store.on_sensor_return = self._request_battery
        flusher = make_flusher(self._get_conn)
        self._merger = BucketMerger(
            bucket_minutes=BUCKET_MINUTES,
            flusher=flusher,
        )

        # Setup graceful shutdown
        def _handle_sigterm(signum, frame):  # type: ignore[no-untyped-def]
            global _shutdown
            log.info("SIGTERM received — flushing and exiting")
            _shutdown = True

        signal.signal(signal.SIGTERM, _handle_sigterm)
        signal.signal(signal.SIGINT, _handle_sigterm)

        # MQTT client
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client = client

        if MQTT_USER:
            client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        # Connect with backoff
        delay = 1
        while not _shutdown:
            try:
                client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                break
            except OSError as exc:
                log.warning(f"mqtt connect error: {exc!r} — retrying in {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 60)

        client.loop_start()
        log.info(
            f"ingest running bucket_minutes={BUCKET_MINUTES} "
            f"mqtt={MQTT_HOST}:{MQTT_PORT}"
        )

        try:
            while not _shutdown:
                time.sleep(1)
        finally:
            log.info("shutting down — flushing open buckets")
            self._merger.flush_all()
            client.loop_stop()
            client.disconnect()
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
            log.info("ingest stopped")


def main() -> None:
    log.info("ingest service starting")
    svc = IngestService()
    svc.run()


if __name__ == "__main__":
    main()
