"""
Core decision logic — pure functions with no DB/network dependencies.
All functions accept plain dicts/lists so they can be unit-tested offline.

Data shapes expected
--------------------
reading (bt_readings row):
  {
    "sensor_id": int,
    "time": datetime (UTC, aware),
    "moisture": float | None,
    "temp_c": float | None,
    "light_lux": int | None,
    "conductivity_us_cm": int | None,
    "battery": int | None,
  }

threshold:
  {
    "plant_id": int,
    "min_soil_moist": int | None,
    "max_soil_moist": int | None,
    "min_soil_ec": int | None,
    "max_soil_ec": int | None,
    "min_light_lux": int | None,
    "max_light_lux": int | None,
    "min_temp": int | None,
    "max_temp": int | None,
    "hysteresis_moist": int,        # default 5
  }

assignment:
  {
    "sensor_id": int,
    "plant_id": int,
    "plant_name": str,
  }

alert_state (mutable dict, keyed by (sensor_id, kind)):
  Set of (sensor_id, kind) pairs that are currently "active" (alert already sent).

All evaluate_* functions return a list of:
  {"sensor_id": int, "plant_id": int | None, "kind": str, "message": str}
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Hysteresis state machine
# ---------------------------------------------------------------------------

def water_needed_transitions(
    sensor_id: int,
    moisture: float | None,
    threshold: dict,
    active_water_alert: bool,
) -> bool:
    """
    Return the *new* active state for water_needed.

    Rules:
      - If moisture is None → no change (can't determine).
      - Alert fires when moisture < min_soil_moist.
      - Alert clears only when moisture > min_soil_moist + hysteresis_moist
        (prevents flapping).
    """
    if moisture is None:
        return active_water_alert

    min_moist = threshold.get("min_soil_moist")
    if min_moist is None:
        return False  # no threshold set

    hysteresis = threshold.get("hysteresis_moist", 5)
    clear_threshold = min_moist + hysteresis

    if not active_water_alert and moisture < min_moist:
        return True   # new alert
    if active_water_alert and moisture > clear_threshold:
        return False  # clear
    return active_water_alert  # no change


# ---------------------------------------------------------------------------
# Staleness check
# ---------------------------------------------------------------------------

# Staleness and stuck-value detection used to live here. Both are now Grafana
# alert rules (compose/grafana/provisioning/alerting/rules.yaml) — the SQL
# there is a direct port, including is_stuck's duration-based semantics (a
# sample-count window false-flagged sensors constantly: soil moisture in a dry
# spell repeats across polls without the sensor being stuck).


# ---------------------------------------------------------------------------
# Alert deduplication
# ---------------------------------------------------------------------------

def within_cooldown(
    last_fired: datetime | None,
    now: datetime,
    cooldown: timedelta,
) -> bool:
    """
    Return True if an alert of this (sensor, kind) fired too recently to re-emit.

    Restart-durable dedup: the in-memory active_alerts set is lost when the
    process restarts, so on the next cycle every currently-true condition would
    re-fire at once. Seeding last-fired times from alert_events and gating emits
    on this cooldown bounds each alert to at most one row per cooldown window,
    regardless of restarts or brief condition flapping.

    Boundary: exactly `cooldown` elapsed is NOT suppressed (allows re-fire).
    """
    if last_fired is None:
        return False
    return (now - last_fired) < cooldown


def should_fire(
    active_alerts: set[tuple[int, str]],
    sensor_id: int,
    kind: str,
    condition_true: bool,
) -> tuple[bool, set[tuple[int, str]]]:
    """
    Decide whether to fire a new alert_event row.

    Returns (fire: bool, updated_active_alerts: set).
    - fire=True  → insert alert_events row (condition just became true)
    - fire=False → condition unchanged or cleared; no new row needed.
    Active alerts are cleared when condition_true is False.
    """
    key = (sensor_id, kind)
    new_active = set(active_alerts)

    if condition_true:
        if key not in new_active:
            new_active.add(key)
            return True, new_active
        return False, new_active  # already active, no new row
    else:
        new_active.discard(key)
        return False, new_active


# ---------------------------------------------------------------------------
# Out-of-range checks
# ---------------------------------------------------------------------------

def out_of_range_fields(reading: dict, threshold: dict) -> list[str]:
    """
    Return list of field names that are out of range.
    Only checks non-None reading values against non-None threshold limits.

    NOTE: light_lux is excluded here — it uses sustained-OOR logic separately
    (see light_lux_sustained_oor) to avoid false alarms at night.
    """
    checks = [
        ("temp_c",             "min_temp",        "max_temp"),
        ("conductivity_us_cm", "min_soil_ec",     "max_soil_ec"),
    ]
    violated = []
    for field, min_key, max_key in checks:
        val = reading.get(field)
        if val is None:
            continue
        lo = threshold.get(min_key)
        hi = threshold.get(max_key)
        if (lo is not None and val < lo) or (hi is not None and val > hi):
            violated.append(field)
    return violated


def light_lux_sustained_oor(
    readings: list[dict],
    threshold: dict,
    now: datetime,
    min_days: float = 3.0,
) -> bool:
    """
    Return True only when light_lux has been continuously out-of-range for at
    least `min_days` days worth of readings.

    This prevents false positives at night: a single reading (or even a whole
    night) of low light_lux will NOT fire. Only chronic insufficient light
    (bad sensor placement) over multiple days triggers the alert.

    Logic: walk readings newest-first. If ANY reading within the min_days
    window has light_lux within range (or is None — can't determine), the
    condition is NOT met. The condition is met only when ALL readings with
    non-None light_lux over the full min_days span are out-of-range, AND
    readings span at least min_days.
    """
    min_hours = min_days * 24.0
    lo = threshold.get("min_light_lux")
    hi = threshold.get("max_light_lux")

    if lo is None and hi is None:
        return False  # no light thresholds set

    # Filter to readings with non-None light_lux
    lux_readings = [r for r in readings if r.get("light_lux") is not None]
    if not lux_readings:
        return False

    # Check that readings span at least min_days
    newest = lux_readings[0]["time"]
    oldest = lux_readings[-1]["time"]
    span_hours = (newest - oldest).total_seconds() / 3600.0
    if span_hours < min_hours:
        return False  # not enough history to judge

    # Check that ALL lux readings within the window are out-of-range
    cutoff = now - timedelta(hours=min_hours)
    for r in lux_readings:
        if r["time"] < cutoff:
            break  # older than our window, stop
        val = r["light_lux"]
        in_range = True
        if lo is not None and val < lo:
            in_range = False
        elif hi is not None and val > hi:
            in_range = False
        else:
            in_range = True
        if in_range:
            return False  # found an in-range reading, not sustained OOR

    return True


# ---------------------------------------------------------------------------
# Range conditions — one named alert kind per condition
# ---------------------------------------------------------------------------
#
# These replace the single `out_of_range` kind. The point is not nicer wording:
# an instantaneous threshold test is simply wrong for most of these signals, and
# firing one produces alerts nobody can act on.
#
#   - Watering makes soil "too moist" every single time, for hours. Only soil
#     that is STILL saturated a day later is a fault (bad drainage).
#   - Sun on the probe spikes temperature on any outdoor sensor. Heat is also
#     not actionable outdoors — you cannot turn the summer off — so it is
#     suppressed there entirely. Frost is the opposite: actionable, and urgent,
#     so it gets the shortest window of any rule here.
#   - EC drifts over days and is briefly diluted by watering, so it is judged on
#     a daily mean rather than a sample.
#   - Light is zero every night, which is why it already had sustained-OOR logic
#     (see light_lux_sustained_oor); it is only split by direction here.
#
# Every window below is chosen so that when the alert fires, there is something
# physical to do: water, feed, flush, cover, or bring the plant indoors.

# Locations that see weather. Matched as substrings against sensors.location,
# which is free text typed by a human ("Terrace", "balcony left", ...).
OUTDOOR_HINTS = ("terrace", "balcony", "garden", "outside", "outdoor", "patio", "yard")

# Kinds produced by range_conditions(), in the order they are evaluated.
# Below this soil moisture a MiFlora's conductivity reading says more about how
# dry the soil is than about its nutrients, so the feed_needed rule ignores it.
EC_MIN_MOISTURE = 20.0

RANGE_KINDS = (
    "frost_risk",
    "heat_stress",
    "feed_needed",
    "salt_buildup",
    "soil_not_draining",
    "light_low",
    "light_high",
)


def is_outdoor(location: str | None) -> bool:
    """True when a sensor's location suggests it is exposed to weather."""
    if not location:
        return False
    loc = location.lower()
    return any(hint in loc for hint in OUTDOOR_HINTS)


def _within(readings: list[dict], now: datetime, hours: float, field: str) -> list[dict]:
    """Readings inside the window that actually carry `field`, newest first."""
    cutoff = now - timedelta(hours=hours)
    return [
        r for r in readings
        if r.get(field) is not None and r.get("time") is not None and r["time"] >= cutoff
    ]


def _spans(readings: list[dict], hours: float) -> bool:
    """True when readings cover at least `hours` of wall clock.

    Guards every sustained rule: without it, two readings a minute apart would
    satisfy "continuously out of range for 24h".
    """
    if len(readings) < 2:
        return False
    span = (readings[0]["time"] - readings[-1]["time"]).total_seconds() / 3600.0
    return span >= hours


def mean_over(readings: list[dict], now: datetime, field: str, hours: float) -> float | None:
    """Mean of `field` over the window, or None when the window is empty."""
    vals = [float(r[field]) for r in _within(readings, now, hours, field)]
    return sum(vals) / len(vals) if vals else None


def sustained(
    readings: list[dict],
    now: datetime,
    field: str,
    hours: float,
    lo: float | None = None,
    hi: float | None = None,
    min_span: float | None = None,
) -> bool:
    """True when EVERY reading of `field` in the window breaches lo/hi, and the
    readings span at least `min_span` hours. One good sample resets it.

    `min_span` defaults to the full window, which is right for the long rules.
    Short windows need it lower: readings arrive one per BUCKET_MINUTES (30), so
    a 30-minute window holds at most two of them, and demanding they span the
    whole 30 minutes makes the rule unsatisfiable in practice.
    """
    window = _within(readings, now, hours, field)
    if not window or not _spans(window, hours if min_span is None else min_span):
        return False
    for r in window:
        val = float(r[field])
        if lo is not None and val < lo:
            continue
        if hi is not None and val > hi:
            continue
        return False  # in range at least once — not sustained
    return True


def episodes_above(
    readings: list[dict], now: datetime, field: str, hi: float, days: float
) -> int:
    """Count distinct excursions above `hi` within the window.

    An episode is a run of consecutive above-threshold readings; returning to
    normal and rising again counts as a second one. Three episodes of soggy soil
    in a week is a drainage problem even when none of them lasts a full day.
    """
    window = list(reversed(_within(readings, now, days * 24.0, field)))  # oldest first
    episodes, above = 0, False
    for r in window:
        now_above = float(r[field]) > hi
        if now_above and not above:
            episodes += 1
        above = now_above
    return episodes


def range_conditions(
    readings: list[dict],
    threshold: dict,
    now: datetime,
    location: str | None = None,
    light_min_days: float = 3.0,
) -> list[tuple[str, str]]:
    """Evaluate every range rule. Returns [(kind, detail), ...] for those met."""
    out: list[tuple[str, str]] = []
    if not threshold:
        return out

    min_temp = threshold.get("min_temp")
    max_temp = threshold.get("max_temp")
    min_ec = threshold.get("min_soil_ec")
    max_ec = threshold.get("max_soil_ec")
    max_moist = threshold.get("max_soil_moist")
    outdoor = is_outdoor(location)

    # Frost — 30 minutes, the shortest window here. Acting late is the failure
    # mode that matters; a false alarm just means checking on a plant.
    if min_temp is not None and sustained(
        readings, now, "temp_c", 1.0, lo=min_temp, min_span=0.5
    ):
        cur = _within(readings, now, 1.0, "temp_c")
        out.append(("frost_risk", f"{float(cur[0]['temp_c']):.1f}°C (min {min_temp}°C)"))

    # Heat — 6h mean, and never outdoors: sun on the probe spikes any single
    # reading, and summer heat outside is not something an alert can fix.
    if max_temp is not None and not outdoor:
        avg = mean_over(readings, now, "temp_c", 6.0)
        if avg is not None and avg > max_temp and _spans(_within(readings, now, 6.0, "temp_c"), 5.0):
            out.append(("heat_stress", f"{avg:.1f}°C average over 6h (max {max_temp}°C)"))

    # Fertiliser — daily mean. EC moves over days and dips right after watering.
    #
    # The low side is gated on moisture. A MiFlora measures conductivity between
    # two electrodes, so EC collapses toward zero in dry soil no matter how much
    # fertiliser is present — judging "needs feeding" on a dry pot re-reports
    # thirst as hunger. Confirmed the hard way on 2026-07-28: 12 of 18 plants
    # fired feed_needed on first deploy and every one of them was simply dry
    # (Cannabis #14 at 0.0% moisture read 28 µS/cm; Spider plant at 11.4% read 0).
    # water_needed is the correct alert for those, and it already fires.
    #
    # The floor is the plant's own minimum, but never below EC_MIN_MOISTURE:
    # succulents are allowed down to 7% moisture, and no EC reading is
    # trustworthy that dry.
    avg_moist = mean_over(readings, now, "moisture", 24.0)
    moist_floor = max(float(threshold.get("min_soil_moist") or 0.0), EC_MIN_MOISTURE)
    ec_readable = avg_moist is not None and avg_moist >= moist_floor

    ec_window = _within(readings, now, 24.0, "conductivity_us_cm")
    if ec_window and _spans(ec_window, 12.0):
        avg_ec = mean_over(readings, now, "conductivity_us_cm", 24.0)
        if min_ec is not None and avg_ec is not None and avg_ec < min_ec and ec_readable:
            out.append(("feed_needed", f"{avg_ec:.0f} µS/cm average over 24h (min {min_ec})"))
        elif max_ec is not None and avg_ec is not None and avg_ec > max_ec:
            out.append(("salt_buildup", f"{avg_ec:.0f} µS/cm average over 24h (max {max_ec})"))

    # Drainage — soaked for a full day, or soggy three separate times in a week.
    # Watering guarantees the first few hours, so neither branch can be tripped
    # by normal care.
    if max_moist is not None:
        if sustained(readings, now, "moisture", 24.0, hi=max_moist):
            out.append(("soil_not_draining", f"above {max_moist}% for 24h"))
        else:
            eps = episodes_above(readings, now, "moisture", float(max_moist), 7.0)
            if eps >= 3:
                out.append(("soil_not_draining", f"{eps} soggy spells in 7 days (max {max_moist}%)"))

    # Light — reuse the existing sustained rule, split by direction so the
    # message can say which way it is wrong.
    if light_lux_sustained_oor(readings, threshold, now, min_days=light_min_days):
        lo, hi = threshold.get("min_light_lux"), threshold.get("max_light_lux")
        lux = _within(readings, now, light_min_days * 24.0, "light_lux")
        latest_lux = float(lux[0]["light_lux"]) if lux else None
        if hi is not None and latest_lux is not None and latest_lux > hi:
            out.append(("light_high", f"{latest_lux:.0f} lux (max {hi}) for {light_min_days:g} days"))
        elif lo is not None:
            detail = f"below {lo} lux for {light_min_days:g} days"
            out.append(("light_low", detail))

    return out


# ---------------------------------------------------------------------------
# Alert message builders
# ---------------------------------------------------------------------------

def _plant_label(assignment: dict | None) -> str:
    if assignment and assignment.get("plant_name"):
        return f"plant={assignment['plant_name']}"
    return "plant=unknown"


def _sensor_label(sensor_id: int, sensor_name: str | None) -> str:
    """Prefer the human-assigned sensor name (shown in Grafana/Telegram) over
    the raw numeric id, which meant nothing without a lookup."""
    return f"sensor={sensor_name}" if sensor_name else f"sensor_id={sensor_id}"


def build_water_needed_message(
    sensor_id: int, moisture: float, assignment: dict | None, sensor_name: str | None = None
) -> str:
    return (
        f"Water needed: {_sensor_label(sensor_id, sensor_name)} {_plant_label(assignment)} "
        f"moisture={moisture:.1f}%"
    )


def build_out_of_range_message(
    sensor_id: int, fields: list[str], assignment: dict | None, sensor_name: str | None = None
) -> str:
    return (
        f"Out of range ({', '.join(fields)}): {_sensor_label(sensor_id, sensor_name)} "
        f"{_plant_label(assignment)}"
    )


# Headline per range kind. These are what a person reads on their phone, so
# each names the action, not the measurement: "Feed needed", not "EC low".
RANGE_HEADLINES = {
    "frost_risk":        "Frost risk",
    "heat_stress":       "Too hot",
    "feed_needed":       "Fertiliser needed",
    "salt_buildup":      "Salt build-up — flush the soil",
    "soil_not_draining": "Soil not draining",
    "light_low":         "Not enough light",
    "light_high":        "Too much light",
}


def build_range_message(
    kind: str,
    sensor_id: int,
    detail: str,
    assignment: dict | None,
    sensor_name: str | None = None,
) -> str:
    headline = RANGE_HEADLINES.get(kind, kind.replace("_", " ").capitalize())
    return (
        f"{headline}: {_sensor_label(sensor_id, sensor_name)} "
        f"{_plant_label(assignment)} {detail}"
    )




# ---------------------------------------------------------------------------
# Main evaluation function (pure, injectable)
# ---------------------------------------------------------------------------

def evaluate_sensor(
    sensor_id: int,
    recent_readings: list[dict],          # ordered newest-first, bt_readings rows
    assignment: dict | None,              # current active assignment or None
    threshold: dict | None,               # threshold row or None
    active_alerts: set[tuple[int, str]],  # mutable state passed in
    now: datetime,
    sensor_name: str | None = None,
    sensor_location: str | None = None,
    light_oor_readings: list[dict] | None = None,
    light_oor_min_days: float = 3.0,
) -> tuple[list[dict], set[tuple[int, str]]]:
    """
    Evaluate one sensor and return (new_alert_rows, updated_active_alerts).

    new_alert_rows: list of dicts ready to INSERT into alert_events.

    Scope note: this evaluates *plant* state only — water_needed and
    out_of_range. The sensor-health kinds (stale, low_battery, stuck) used to
    live here too; they are scheduled threshold queries over the same tables,
    so they now run as Grafana alert rules
    (compose/grafana/provisioning/alerting/rules.yaml) and notify Telegram
    directly. Keeping them here as well would double every message.

    light_oor_readings: readings spanning at least light_oor_min_days, used
    to evaluate sustained light_lux out-of-range. Falls back to
    recent_readings if not given.

    light_oor_min_days: minimum days of continuous OOR light_lux readings
    before firing an alert (default 3). Prevents false positives at night.
    """
    alerts: list[dict] = []
    plant_id = assignment["plant_id"] if assignment else None

    if not recent_readings:
        return alerts, active_alerts  # nothing to evaluate

    latest = recent_readings[0]

    # ---- Threshold-based alerts (only if assigned + threshold exists) -------
    if assignment and threshold:

        # water_needed (hysteresis)
        moisture = latest.get("moisture")
        currently_watering = (sensor_id, "water_needed") in active_alerts
        new_water_state = water_needed_transitions(
            sensor_id, moisture, threshold, currently_watering
        )
        fire_water, active_alerts = should_fire(
            active_alerts, sensor_id, "water_needed", new_water_state
        )
        if fire_water:
            alerts.append({
                "sensor_id": sensor_id,
                "plant_id": plant_id,
                "kind": "water_needed",
                "message": build_water_needed_message(sensor_id, moisture, assignment, sensor_name),
            })

        # Range conditions, one named kind each. Evaluated over history rather
        # than the latest sample: see the RANGE_KINDS section for why an
        # instantaneous test is wrong for every one of these signals.
        history = light_oor_readings if light_oor_readings is not None else recent_readings
        met = dict(range_conditions(
            history, threshold, now,
            location=sensor_location,
            light_min_days=light_oor_min_days,
        ))

        # Every kind is evaluated every cycle, including the ones that are NOT
        # met — should_fire needs the falling edge to clear its state, otherwise
        # a resolved condition could never fire again.
        for range_kind in RANGE_KINDS:
            fire, active_alerts = should_fire(
                active_alerts, sensor_id, range_kind, range_kind in met
            )
            if fire:
                alerts.append({
                    "sensor_id": sensor_id,
                    "plant_id": plant_id,
                    "kind": range_kind,
                    "message": build_range_message(
                        range_kind, sensor_id, met[range_kind], assignment, sensor_name
                    ),
                })

    return alerts, active_alerts
