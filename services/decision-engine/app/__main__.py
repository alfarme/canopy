"""
decision-engine entry point.

Runs a periodic loop (INTERVAL_SECONDS, default 300) that:
  1. Loads active sensors, assignments, thresholds from DB.
  2. Fetches recent readings for each sensor.
  3. Runs evaluate_sensor() (pure logic in engine.py).
  4. Inserts new alert_events rows for any newly-triggered conditions.
  5. Maintains in-memory active_alerts set for deduplication across cycles.
"""
from __future__ import annotations

import os
import signal
import time
from datetime import datetime, timedelta, timezone

import psycopg

from .db import connect
from .engine import RANGE_KINDS, evaluate_sensor, within_cooldown
from .log import get_logger
from .queries import (
    fetch_active_assignments,
    fetch_active_sensors,
    fetch_last_alert_times,
    fetch_recent_bt_readings,
    fetch_silenced_alerts,
    fetch_thresholds,
    insert_alert_event,
)

log = get_logger(__name__)

INTERVAL = int(os.environ.get("INTERVAL_SECONDS", "300"))
# STALE_HOURS / STUCK_HOURS / BATTERY_LOW_PCT used to be read here. Those three
# kinds are now Grafana alert rules; their thresholds live in
# compose/grafana/provisioning/alerting/rules.yaml. The env vars can stay in
# env/decision.env — they are simply no longer read.
# Restart-durable dedup: an alert of a given (sensor, kind) is emitted at most
# once per this window, so a container restart (which wipes the in-memory
# active_alerts set) does not re-fire every currently-true condition at once.
# Grafana's equivalent knob is the notification policy's repeat_interval, kept
# at the same 6h.
ALERT_COOLDOWN = timedelta(hours=float(os.environ.get("ALERT_COOLDOWN_HOURS", "6")))
PER_KIND_COOLDOWN: dict[str, timedelta] = {}
# Days of sustained out-of-range light_lux readings before alert fires.
# Prevents night-time false positives — only chronic bad placement triggers.
LIGHT_OOR_MIN_DAYS = float(os.environ.get("LIGHT_OOR_MIN_DAYS", "3"))
LIGHT_OOR_LOOKBACK_HOURS = LIGHT_OOR_MIN_DAYS * 24 * 1.5 + 1
# The range rules share one history fetch. The widest of them counts soggy
# spells over 7 days, so the window has to cover that even when the light rule
# (the previous widest, at ~4.5 days) would be happy with less.
HISTORY_LOOKBACK_HOURS = max(LIGHT_OOR_LOOKBACK_HOURS, 7 * 24 + 1)
BACKOFF_CAP = 60  # seconds

_shutdown = False


def _handle_sigterm(signum, frame):
    global _shutdown
    log.info("event=sigterm_received shutting_down=true")
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_sigterm)


def run_cycle(
    conn: psycopg.Connection,
    active_alerts: set,
    last_fired: dict[tuple[int, str], datetime],
) -> set:
    """Run one evaluation cycle; return updated active_alerts.

    last_fired is mutated in place: it holds the last-emitted time per
    (sensor_id, kind) and gates re-emission via the ALERT_COOLDOWN window.
    """
    now = datetime.now(tz=timezone.utc)

    sensors = fetch_active_sensors(conn)
    assignments_list = fetch_active_assignments(conn)
    thresholds = fetch_thresholds(conn)
    silenced = fetch_silenced_alerts(conn)

    # Index assignments by sensor_id
    assignments: dict[int, dict] = {a["sensor_id"]: a for a in assignments_list}

    log.info(
        f"event=cycle_start sensors={len(sensors)} "
        f"assignments={len(assignments)} thresholds={len(thresholds)} "
        f"silenced={len(silenced)}"
    )

    history_since = now - timedelta(hours=HISTORY_LOOKBACK_HOURS)

    for sensor in sensors:
        sid = sensor["id"]

        # Two windows per sensor: a small one for the instantaneous checks
        # (water_needed reads the latest sample), and a wide one for every
        # range rule, all of which measure duration rather than sample count.
        readings = fetch_recent_bt_readings(conn, sid, limit=5)
        history_readings = fetch_recent_bt_readings(conn, sid, since=history_since)

        assignment = assignments.get(sid)
        threshold = None
        if assignment:
            threshold = thresholds.get(assignment["plant_id"])

        new_alerts, active_alerts = evaluate_sensor(
            sensor_id=sid,
            recent_readings=readings,
            assignment=assignment,
            threshold=threshold,
            active_alerts=active_alerts,
            now=now,
            sensor_name=sensor.get("name"),
            sensor_location=sensor.get("location"),
            light_oor_readings=history_readings,
            light_oor_min_days=LIGHT_OOR_MIN_DAYS,
        )

        for alert in new_alerts:
            key = (alert["sensor_id"], alert["kind"])
            # Back-compat: every range condition used to be one `out_of_range`
            # kind. A silence stored under that old name still means "mute range
            # alerts for this sensor", so honour it — otherwise the split would
            # un-mute things the user had deliberately silenced.
            legacy = (alert["sensor_id"], "out_of_range")
            if key in silenced or (alert["kind"] in RANGE_KINDS and legacy in silenced):
                continue
            cooldown = PER_KIND_COOLDOWN.get(alert["kind"], ALERT_COOLDOWN)
            if within_cooldown(last_fired.get(key), now, cooldown):
                log.info(
                    f"event=alert_suppressed kind={alert['kind']} "
                    f"sensor_id={alert['sensor_id']} reason=cooldown"
                )
                continue
            log.info(
                f"event=alert_fired kind={alert['kind']} "
                f"sensor_id={alert['sensor_id']} plant_id={alert['plant_id']}"
            )
            insert_alert_event(
                conn,
                sensor_id=alert["sensor_id"],
                plant_id=alert["plant_id"],
                kind=alert["kind"],
                message=alert["message"],
            )
            last_fired[key] = now

    log.info(f"event=cycle_done alerts_fired={sum(1 for _ in range(0))}")
    return active_alerts


def main():
    log.info(f"event=startup interval_seconds={INTERVAL} cooldown={ALERT_COOLDOWN}")
    active_alerts: set = set()
    last_fired: dict[tuple[int, str], datetime] = {}
    seeded = False
    backoff = 5

    while not _shutdown:
        conn = None
        try:
            conn = connect()
            backoff = 5  # reset on success
            if not seeded:
                # Seed cooldown state from history so a restart doesn't re-fire
                # every currently-active alert. Done once, after the first
                # successful DB connect.
                last_fired = fetch_last_alert_times(conn)
                seeded = True
                log.info(f"event=cooldown_seeded keys={len(last_fired)}")
            active_alerts = run_cycle(conn, active_alerts, last_fired)
        except psycopg.OperationalError as exc:
            log.error(f"event=db_error error={exc} backoff={backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_CAP)
            continue
        except Exception as exc:
            log.error(f"event=unexpected_error error={exc}")
        finally:
            if conn and not conn.closed:
                conn.close()

        # Sleep in small increments to respond to SIGTERM promptly
        elapsed = 0
        while elapsed < INTERVAL and not _shutdown:
            time.sleep(1)
            elapsed += 1

    log.info("event=shutdown_complete")


if __name__ == "__main__":
    main()
