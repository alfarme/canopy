"""
TDD unit tests for decision-engine pure logic.
No network, DB, or broker — all inputs are injected dicts/lists.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.engine import (
    RANGE_KINDS,
    range_conditions,
    is_outdoor,
    evaluate_sensor,
    out_of_range_fields,
    should_fire,
    within_cooldown,
    water_needed_transitions,
    build_water_needed_message,
    build_out_of_range_message,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_reading(
    sensor_id=1,
    time=None,
    moisture=50.0,
    temp_c=22.0,
    light_lux=1000,
    conductivity_us_cm=200,
    battery=80,
):
    return {
        "time": time or NOW,
        "sensor_id": sensor_id,
        "moisture": moisture,
        "temp_c": temp_c,
        "light_lux": light_lux,
        "conductivity_us_cm": conductivity_us_cm,
        "battery": battery,
    }


def make_threshold(
    min_soil_moist=30,
    max_soil_moist=80,
    hysteresis_moist=5,
    min_temp=10,
    max_temp=35,
    min_light_lux=500,
    max_light_lux=5000,
    min_soil_ec=100,
    max_soil_ec=500,
):
    return {
        "plant_id": 1,
        "min_soil_moist": min_soil_moist,
        "max_soil_moist": max_soil_moist,
        "hysteresis_moist": hysteresis_moist,
        "min_temp": min_temp,
        "max_temp": max_temp,
        "min_light_lux": min_light_lux,
        "max_light_lux": max_light_lux,
        "min_soil_ec": min_soil_ec,
        "max_soil_ec": max_soil_ec,
    }


def make_assignment(sensor_id=1, plant_id=1, plant_name="Monstera"):
    return {"sensor_id": sensor_id, "plant_id": plant_id, "plant_name": plant_name}


# ---------------------------------------------------------------------------
# water_needed_transitions (hysteresis state machine)
# ---------------------------------------------------------------------------

class TestWaterNeededTransitions:
    """TDD: hysteresis state machine for water_needed alert."""

    def test_no_alert_when_moisture_above_min(self):
        t = make_threshold(min_soil_moist=30, hysteresis_moist=5)
        assert water_needed_transitions(1, 50.0, t, False) is False

    def test_alert_fires_when_moisture_drops_below_min(self):
        t = make_threshold(min_soil_moist=30, hysteresis_moist=5)
        assert water_needed_transitions(1, 25.0, t, False) is True

    def test_alert_stays_active_when_moisture_below_min(self):
        t = make_threshold(min_soil_moist=30, hysteresis_moist=5)
        # Already active, still below min
        assert water_needed_transitions(1, 25.0, t, True) is True

    def test_alert_stays_active_within_hysteresis_band(self):
        """Moisture rose above min but below min+hysteresis — stays active."""
        t = make_threshold(min_soil_moist=30, hysteresis_moist=5)
        # 33 > 30 (min) but < 35 (min+hysteresis): should NOT clear
        assert water_needed_transitions(1, 33.0, t, True) is True

    def test_alert_clears_when_moisture_above_hysteresis_band(self):
        t = make_threshold(min_soil_moist=30, hysteresis_moist=5)
        # 36 > 35 (30+5): should clear
        assert water_needed_transitions(1, 36.0, t, True) is False

    def test_no_change_when_moisture_is_none(self):
        t = make_threshold(min_soil_moist=30, hysteresis_moist=5)
        assert water_needed_transitions(1, None, t, False) is False
        assert water_needed_transitions(1, None, t, True) is True

    def test_no_alert_when_no_min_threshold(self):
        t = make_threshold(min_soil_moist=None)
        assert water_needed_transitions(1, 10.0, t, True) is False

    def test_alert_fires_at_boundary_below_min(self):
        """moisture == min_soil_moist - 0.001 should fire."""
        t = make_threshold(min_soil_moist=30, hysteresis_moist=5)
        assert water_needed_transitions(1, 29.9, t, False) is True

    def test_no_fire_at_exact_min_boundary(self):
        """moisture == min_soil_moist exactly — not strictly less, no fire."""
        t = make_threshold(min_soil_moist=30, hysteresis_moist=5)
        assert water_needed_transitions(1, 30.0, t, False) is False

    def test_does_not_fire_twice_without_clearing(self):
        """Hysteresis: once fired, stays fired until clear threshold crossed."""
        t = make_threshold(min_soil_moist=30, hysteresis_moist=5)
        # Start alert
        state = water_needed_transitions(1, 20.0, t, False)
        assert state is True
        # Moisture rises to 32 — still in band, should NOT clear
        state = water_needed_transitions(1, 32.0, t, state)
        assert state is True
        # Moisture rises to 36 — above band, clear
        state = water_needed_transitions(1, 36.0, t, state)
        assert state is False
# ---------------------------------------------------------------------------
# should_fire (deduplication)
# ---------------------------------------------------------------------------

class TestShouldFire:
    def test_fires_on_first_occurrence(self):
        fire, new_set = should_fire(set(), 1, "stale", True)
        assert fire is True
        assert (1, "stale") in new_set

    def test_does_not_refire_while_active(self):
        active = {(1, "stale")}
        fire, new_set = should_fire(active, 1, "stale", True)
        assert fire is False
        assert (1, "stale") in new_set

    def test_clears_when_condition_false(self):
        active = {(1, "stale")}
        fire, new_set = should_fire(active, 1, "stale", False)
        assert fire is False
        assert (1, "stale") not in new_set

    def test_no_fire_when_already_clear(self):
        fire, new_set = should_fire(set(), 1, "stale", False)
        assert fire is False
        assert (1, "stale") not in new_set

    def test_independent_sensors(self):
        active = {(1, "stale")}
        fire, new_set = should_fire(active, 2, "stale", True)
        assert fire is True
        assert (2, "stale") in new_set
        assert (1, "stale") in new_set  # unaffected

    def test_independent_kinds(self):
        active = {(1, "stale")}
        fire, new_set = should_fire(active, 1, "low_battery", True)
        assert fire is True
        assert (1, "low_battery") in new_set
        assert (1, "stale") in new_set  # unaffected

    def test_refires_after_clear(self):
        """After condition clears and returns, a new alert should fire."""
        active = {(1, "stale")}
        # Clear
        _, active = should_fire(active, 1, "stale", False)
        # Condition comes back
        fire, active = should_fire(active, 1, "stale", True)
        assert fire is True

    def test_does_not_mutate_input_set(self):
        """Input set must not be mutated."""
        original = {(1, "stale")}
        original_copy = set(original)
        should_fire(original, 2, "stale", True)
        assert original == original_copy


# ---------------------------------------------------------------------------
# out_of_range_fields
# ---------------------------------------------------------------------------

class TestOutOfRangeFields:
    def test_all_in_range(self):
        r = make_reading(temp_c=22, light_lux=1000, conductivity_us_cm=200)
        t = make_threshold(min_temp=10, max_temp=35, min_light_lux=500, max_light_lux=5000,
                           min_soil_ec=100, max_soil_ec=500)
        assert out_of_range_fields(r, t) == []

    def test_temp_too_high(self):
        r = make_reading(temp_c=40)
        t = make_threshold(min_temp=10, max_temp=35)
        assert "temp_c" in out_of_range_fields(r, t)

    def test_temp_too_low(self):
        r = make_reading(temp_c=5)
        t = make_threshold(min_temp=10, max_temp=35)
        assert "temp_c" in out_of_range_fields(r, t)

    def test_light_too_low_not_instant(self):
        """light_lux is excluded from instant out_of_range — uses sustained OOR logic."""
        r = make_reading(light_lux=100)
        t = make_threshold(min_light_lux=500)
        assert "light_lux" not in out_of_range_fields(r, t)

    def test_ec_too_high(self):
        r = make_reading(conductivity_us_cm=600)
        t = make_threshold(max_soil_ec=500)
        assert "conductivity_us_cm" in out_of_range_fields(r, t)

    def test_skips_none_reading(self):
        r = make_reading(temp_c=None)
        t = make_threshold(min_temp=10, max_temp=35)
        assert "temp_c" not in out_of_range_fields(r, t)

    def test_skips_none_threshold(self):
        r = make_reading(temp_c=40)
        t = make_threshold(max_temp=None, min_temp=None)
        assert "temp_c" not in out_of_range_fields(r, t)

    def test_multiple_violations(self):
        """light_lux is no longer in instant OOR; only temp + EC are checked."""
        r = make_reading(temp_c=40, conductivity_us_cm=600)
        t = make_threshold(max_temp=35, max_soil_ec=500)
        violated = out_of_range_fields(r, t)
        assert "temp_c" in violated
        assert "conductivity_us_cm" in violated


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

class TestMessageBuilders:
    def test_water_needed_includes_sensor_and_moisture(self):
        msg = build_water_needed_message(42, 20.5, make_assignment(sensor_id=42))
        assert "42" in msg
        assert "20.5" in msg

    def test_water_needed_unknown_plant(self):
        msg = build_water_needed_message(42, 20.5, None)
        assert "unknown" in msg

    def test_out_of_range_includes_fields(self):
        msg = build_out_of_range_message(5, ["temp_c", "light_lux"], None)
        assert "temp_c" in msg
        assert "light_lux" in msg

    def test_sensor_name_preferred_over_id_when_available(self):
        """Grafana/Telegram should show the human-assigned name, not a raw
        id that means nothing without a lookup."""
        msg = build_out_of_range_message(3, ["moisture"], None, sensor_name="balcony-1")
        assert "balcony-1" in msg
        assert "sensor_id=3" not in msg

    def test_falls_back_to_id_when_sensor_has_no_name(self):
        msg = build_out_of_range_message(3, ["moisture"], None, sensor_name=None)
        assert "sensor_id=3" in msg


# ---------------------------------------------------------------------------
# evaluate_sensor (integration of pure logic)
# ---------------------------------------------------------------------------

class TestEvaluateSensor:

    def test_no_alerts_for_healthy_sensor(self):
        readings = [make_reading(moisture=50, temp_c=22, battery=80,
                                 time=NOW)]
        assignment = make_assignment()
        threshold = make_threshold()
        alerts, state = evaluate_sensor(
            sensor_id=1,
            recent_readings=readings,
            assignment=assignment,
            threshold=threshold,
            active_alerts=set(),
            now=NOW,
        )
        assert alerts == []

    def test_water_needed_fires_on_low_moisture(self):
        readings = [make_reading(moisture=20.0, time=NOW)]
        assignment = make_assignment()
        threshold = make_threshold(min_soil_moist=30)
        alerts, state = evaluate_sensor(
            sensor_id=1,
            recent_readings=readings,
            assignment=assignment,
            threshold=threshold,
            active_alerts=set(),
            now=NOW,
        )
        kinds = [a["kind"] for a in alerts]
        assert "water_needed" in kinds

    def test_water_needed_does_not_refire(self):
        readings = [make_reading(moisture=20.0, time=NOW)]
        assignment = make_assignment()
        threshold = make_threshold(min_soil_moist=30)

        # First cycle — fires
        alerts1, state = evaluate_sensor(1, readings, assignment, threshold, set(), NOW)
        assert any(a["kind"] == "water_needed" for a in alerts1)

        # Second cycle — same condition, should NOT refire
        alerts2, state = evaluate_sensor(1, readings, assignment, threshold, state, NOW)
        assert not any(a["kind"] == "water_needed" for a in alerts2)

    def test_out_of_range_fires(self):
        readings = [make_reading(temp_c=40.0, time=NOW)]
        assignment = make_assignment()
        threshold = make_threshold(max_temp=35)
        # Heat is judged on a 6h mean now, not one sample: a single hot reading
        # is sun on the probe, not heat stress.
        readings = [
            make_reading(temp_c=40.0, time=NOW - timedelta(hours=h))
            for h in range(0, 7)
        ]
        alerts, state = evaluate_sensor(
            sensor_id=1,
            recent_readings=readings,
            assignment=assignment,
            threshold=threshold,
            active_alerts=set(),
            now=NOW,
            sensor_location="Living room",
        )
        assert any(a["kind"] == "heat_stress" for a in alerts)

    def test_out_of_range_not_fired_without_assignment(self):
        """No assignment → no out_of_range check."""
        readings = [make_reading(temp_c=40.0, time=NOW)]
        alerts, state = evaluate_sensor(
            sensor_id=1,
            recent_readings=readings,
            assignment=None,
            threshold=None,
            active_alerts=set(),
            now=NOW,
        )
        assert not any(a["kind"] in RANGE_KINDS for a in alerts)

    def test_multiple_alert_kinds_can_fire_simultaneously(self):
        readings = [
            make_reading(moisture=20.0, battery=5, temp_c=50.0, time=NOW - timedelta(hours=h))
            for h in range(0, 7)
        ]
        assignment = make_assignment()
        threshold = make_threshold(min_soil_moist=30, max_temp=35)
        alerts, state = evaluate_sensor(
            sensor_id=1,
            recent_readings=readings,
            assignment=assignment,
            threshold=threshold,
            active_alerts=set(),
            now=NOW,
            sensor_location="Living room",
        )
        kinds = {a["kind"] for a in alerts}
        assert "water_needed" in kinds
        assert "heat_stress" in kinds

    def test_alert_contains_plant_id_when_assigned(self):
        readings = [make_reading(moisture=20.0, time=NOW)]
        assignment = make_assignment(plant_id=99)
        threshold = make_threshold(min_soil_moist=30)
        alerts, _ = evaluate_sensor(1, readings, assignment, threshold, set(), NOW)
        water = next(a for a in alerts if a["kind"] == "water_needed")
        assert water["plant_id"] == 99

    def test_no_alerts_without_assignment(self):
        """Every remaining kind is a plant-state alert, so an unassigned
        sensor can no longer produce anything. (It used to emit low_battery,
        which is a Grafana rule now.)"""
        readings = [make_reading(moisture=5, battery=5, time=NOW)]
        alerts, _ = evaluate_sensor(1, readings, None, None, set(), NOW)
        assert alerts == []

    def test_hysteresis_prevents_refire_in_band(self):
        """
        moisture drops → alert fires.
        moisture rises to mid-band → no refire, stays active.
        moisture rises above band → clears.
        moisture drops again → refires.
        """
        threshold = make_threshold(min_soil_moist=30, hysteresis_moist=5)
        assignment = make_assignment()

        # Step 1: moisture=20 → fires
        r1 = [make_reading(moisture=20.0, time=NOW)]
        alerts1, state = evaluate_sensor(1, r1, assignment, threshold, set(), NOW)
        assert any(a["kind"] == "water_needed" for a in alerts1)

        # Step 2: moisture=33 (inside band, 30<33<35) → no refire
        r2 = [make_reading(moisture=33.0, time=NOW + timedelta(minutes=5))]
        alerts2, state = evaluate_sensor(1, r2, assignment, threshold, state, NOW)
        assert not any(a["kind"] == "water_needed" for a in alerts2)

        # Step 3: moisture=40 → clears
        r3 = [make_reading(moisture=40.0, time=NOW + timedelta(minutes=10))]
        alerts3, state = evaluate_sensor(1, r3, assignment, threshold, state, NOW)
        assert not any(a["kind"] == "water_needed" for a in alerts3)

        # Step 4: moisture drops to 20 again → refires
        r4 = [make_reading(moisture=20.0, time=NOW + timedelta(minutes=15))]
        alerts4, state = evaluate_sensor(1, r4, assignment, threshold, state, NOW)
        assert any(a["kind"] == "water_needed" for a in alerts4)

    def test_state_is_per_sensor_independent(self):
        """Alert state for sensor 1 should not affect sensor 2."""
        threshold = make_threshold(min_soil_moist=30)
        assignment1 = make_assignment(sensor_id=1, plant_id=1)
        assignment2 = make_assignment(sensor_id=2, plant_id=2)

        r_low = [make_reading(sensor_id=1, moisture=20.0, time=NOW)]
        r_ok = [make_reading(sensor_id=2, moisture=60.0, time=NOW)]

        _, state = evaluate_sensor(1, r_low, assignment1, threshold, set(), NOW)
        alerts2, _ = evaluate_sensor(2, r_ok, assignment2, threshold, state, NOW)
        assert not any(a["kind"] == "water_needed" for a in alerts2)


# ---------------------------------------------------------------------------
# Cooldown gate (restart-durable dedup)
# ---------------------------------------------------------------------------

class TestWithinCooldown:
    COOLDOWN = timedelta(hours=6)

    def test_no_previous_fire_is_not_suppressed(self):
        assert within_cooldown(None, NOW, self.COOLDOWN) is False

    def test_recent_fire_is_suppressed(self):
        last = NOW - timedelta(hours=1)
        assert within_cooldown(last, NOW, self.COOLDOWN) is True

    def test_old_fire_is_not_suppressed(self):
        last = NOW - timedelta(hours=7)
        assert within_cooldown(last, NOW, self.COOLDOWN) is False

    def test_boundary_at_cooldown_is_not_suppressed(self):
        last = NOW - self.COOLDOWN
        assert within_cooldown(last, NOW, self.COOLDOWN) is False


# ---------------------------------------------------------------------------
# light_lux_sustained_oor (sustained out-of-range for light)
# ---------------------------------------------------------------------------

from app.engine import light_lux_sustained_oor


class TestLightLuxSustainedOor:
    """Tests for sustained out-of-range light_lux logic.

    The key design goal: a single low-light reading at night does NOT fire,
    but sustained insufficient light over multiple days (bad sensor placement)
    DOES fire.
    """

    def test_single_low_reading_does_not_fire(self):
        """One reading below min_light_lux is not enough — could be nighttime."""
        readings = [make_reading(light_lux=100, time=NOW)]
        threshold = make_threshold(min_light_lux=500)
        assert light_lux_sustained_oor(readings, threshold, NOW, min_days=3) is False

    def test_low_readings_spanning_one_night_does_not_fire(self):
        """A full night of low readings (12h) is not enough."""
        readings = [
            make_reading(light_lux=50, time=NOW - timedelta(hours=h))
            for h in range(0, 13)  # 12 hours of readings
        ]
        threshold = make_threshold(min_light_lux=500)
        assert light_lux_sustained_oor(readings, threshold, NOW, min_days=3) is False

    def test_sustained_low_across_min_days_fires(self):
        """Continuous low light for >= min_days fires the alert."""
        # 4 days of readings, every 6 hours, all below min
        readings = [
            make_reading(light_lux=100, time=NOW - timedelta(hours=h))
            for h in range(0, 4 * 24, 6)  # 0, 6, 12, ... 90 hours
        ]
        threshold = make_threshold(min_light_lux=500)
        assert light_lux_sustained_oor(readings, threshold, NOW, min_days=3) is True

    def test_one_good_reading_in_window_prevents_fire(self):
        """If any reading is in-range within the window, no fire."""
        readings = [
            make_reading(light_lux=100, time=NOW - timedelta(hours=h))
            for h in range(0, 4 * 24, 6)
        ]
        # Inject one good reading at hour 36
        readings.insert(6, make_reading(light_lux=600, time=NOW - timedelta(hours=36)))
        # Sort newest-first
        readings.sort(key=lambda r: r["time"], reverse=True)
        threshold = make_threshold(min_light_lux=500)
        assert light_lux_sustained_oor(readings, threshold, NOW, min_days=3) is False

    def test_no_thresholds_does_not_fire(self):
        """No light thresholds set → no alert."""
        readings = [
            make_reading(light_lux=0, time=NOW - timedelta(hours=h))
            for h in range(0, 4 * 24, 6)
        ]
        threshold = make_threshold(min_light_lux=None, max_light_lux=None)
        assert light_lux_sustained_oor(readings, threshold, NOW, min_days=3) is False

    def test_all_none_light_does_not_fire(self):
        """No non-None light readings → no alert."""
        readings = [
            make_reading(light_lux=None, time=NOW - timedelta(hours=h))
            for h in range(0, 4 * 24, 6)
        ]
        threshold = make_threshold(min_light_lux=500)
        assert light_lux_sustained_oor(readings, threshold, NOW, min_days=3) is False

    def test_high_light_sustained_fires_too(self):
        """Sustained light ABOVE max_light_lux also fires."""
        readings = [
            make_reading(light_lux=10000, time=NOW - timedelta(hours=h))
            for h in range(0, 4 * 24, 6)
        ]
        threshold = make_threshold(max_light_lux=5000)
        assert light_lux_sustained_oor(readings, threshold, NOW, min_days=3) is True

    def test_configurable_min_days(self):
        """min_days=1 fires with just 1 day of sustained OOR."""
        readings = [
            make_reading(light_lux=100, time=NOW - timedelta(hours=h))
            for h in range(0, 30, 6)  # 0..24h, every 6h
        ]
        threshold = make_threshold(min_light_lux=500)
        assert light_lux_sustained_oor(readings, threshold, NOW, min_days=1) is True

    def test_evaluate_sensor_light_lux_not_instant(self):
        """A single low light_lux reading doesn't trigger out_of_range via evaluate_sensor."""
        readings = [make_reading(light_lux=100, time=NOW)]
        assignment = make_assignment()
        threshold = make_threshold(min_light_lux=500, max_temp=99, min_temp=0)
        alerts, state = evaluate_sensor(
            sensor_id=1,
            recent_readings=readings,
            assignment=assignment,
            threshold=threshold,
            active_alerts=set(),
            now=NOW,
        )
        assert not any(a["kind"] == "out_of_range" for a in alerts)

    def test_evaluate_sensor_sustained_light_fires(self):
        """Sustained low light_lux over min_days triggers light_low via evaluate_sensor."""
        readings = [make_reading(light_lux=100, time=NOW)]
        light_oor_readings = [
            make_reading(light_lux=100, time=NOW - timedelta(hours=h))
            for h in range(0, 4 * 24, 6)
        ]
        assignment = make_assignment()
        threshold = make_threshold(min_light_lux=500, max_temp=99, min_temp=0)
        alerts, state = evaluate_sensor(
            sensor_id=1,
            recent_readings=readings,
            assignment=assignment,
            threshold=threshold,
            active_alerts=set(),
            now=NOW,
            light_oor_readings=light_oor_readings,
            light_oor_min_days=3,
        )
        assert any(a["kind"] == "light_low" for a in alerts)


class TestRangeConditions:
    """The anti-spam rules: normal plant care must not produce alerts."""

    def _hist(self, field, values, start_h=0, step_h=1):
        """History newest-first, one reading per step_h hours."""
        return [
            make_reading(time=NOW - timedelta(hours=start_h + i * step_h), **{field: v})
            for i, v in enumerate(values)
        ]

    def test_watering_spike_does_not_fire_drainage(self):
        """Freshly watered soil is soaked for hours by design. This is the
        single most important case: it would otherwise fire on every watering."""
        soaked = self._hist("moisture", [95, 94, 92, 88] + [30] * 30)
        met = dict(range_conditions(soaked, make_threshold(max_soil_moist=60), NOW))
        assert "soil_not_draining" not in met

    def test_soaked_for_a_full_day_fires_drainage(self):
        stuck = self._hist("moisture", [95] * 30)
        met = dict(range_conditions(stuck, make_threshold(max_soil_moist=60), NOW))
        assert "soil_not_draining" in met
        assert "24h" in met["soil_not_draining"]

    def test_three_soggy_spells_in_a_week_fire_drainage(self):
        """Recurring sogginess is a drainage fault even when each spell is short."""
        vals = []
        for _ in range(3):
            vals += [90, 90]        # a spell above the limit
            vals += [30, 30, 30]    # recovery in between
        met = dict(range_conditions(self._hist("moisture", vals, step_h=6),
                                    make_threshold(max_soil_moist=60), NOW))
        assert "soil_not_draining" in met
        assert "spells" in met["soil_not_draining"]

    def test_two_soggy_spells_do_not_fire(self):
        vals = ([90, 90] + [30, 30, 30]) * 2
        met = dict(range_conditions(self._hist("moisture", vals, step_h=6),
                                    make_threshold(max_soil_moist=60), NOW))
        assert "soil_not_draining" not in met

    def test_sun_on_the_probe_does_not_fire_heat(self):
        """One scorching hour against six normal ones is sunlight, not heat stress."""
        hist = self._hist("temp_c", [46, 24, 23, 22, 22, 21, 21])
        met = dict(range_conditions(hist, make_threshold(max_temp=30), NOW,
                                    location="Living room"))
        assert "heat_stress" not in met

    def test_sustained_heat_fires_indoors(self):
        hist = self._hist("temp_c", [38] * 8)
        met = dict(range_conditions(hist, make_threshold(max_temp=30), NOW,
                                    location="Living room"))
        assert "heat_stress" in met

    def test_heat_is_suppressed_outdoors(self):
        """Summer outdoors is not actionable — only frost is."""
        hist = self._hist("temp_c", [38] * 8)
        met = dict(range_conditions(hist, make_threshold(max_temp=30), NOW,
                                    location="Terrace"))
        assert "heat_stress" not in met

    def test_frost_still_fires_outdoors(self):
        hist = self._hist("temp_c", [-2, -1, -1], step_h=0.3)
        met = dict(range_conditions(hist, make_threshold(min_temp=5), NOW,
                                    location="Terrace"))
        assert "frost_risk" in met

    def test_frost_needs_more_than_one_reading(self):
        met = dict(range_conditions(self._hist("temp_c", [-2]),
                                    make_threshold(min_temp=5), NOW))
        assert "frost_risk" not in met

    def test_low_ec_fires_feed_needed(self):
        hist = self._hist("conductivity_us_cm", [80] * 24)
        met = dict(range_conditions(hist, make_threshold(min_soil_ec=200), NOW))
        assert "feed_needed" in met

    def test_high_ec_fires_salt_buildup(self):
        hist = self._hist("conductivity_us_cm", [4600] * 24)
        met = dict(range_conditions(hist, make_threshold(max_soil_ec=1500), NOW))
        assert "salt_buildup" in met

    def test_brief_ec_dip_after_watering_does_not_fire(self):
        """Watering dilutes EC for an hour or two; the daily mean absorbs it."""
        hist = self._hist("conductivity_us_cm", [50, 60] + [400] * 22)
        met = dict(range_conditions(hist, make_threshold(min_soil_ec=200), NOW))
        assert "feed_needed" not in met

    def test_outdoor_detection(self):
        assert is_outdoor("Terrace") and is_outdoor("balcony left")
        assert not is_outdoor("Living room")
        assert not is_outdoor(None)


class TestFeedNeededMoistureGate:
    """EC collapses in dry soil, so feed_needed must not fire on a thirsty pot."""

    def _hist(self, moisture, ec, n=24):
        return [
            make_reading(time=NOW - timedelta(hours=i), moisture=moisture,
                         conductivity_us_cm=ec)
            for i in range(n)
        ]

    def test_dry_soil_does_not_fire_feed_needed(self):
        """The real 2026-07-28 case: 0% moisture reading 28 uS/cm is thirst,
        not hunger — water_needed covers it."""
        met = dict(range_conditions(self._hist(0.0, 28),
                                    make_threshold(min_soil_ec=100, min_soil_moist=15), NOW))
        assert "feed_needed" not in met

    def test_watered_soil_with_low_ec_still_fires(self):
        met = dict(range_conditions(self._hist(45.0, 28),
                                    make_threshold(min_soil_ec=100, min_soil_moist=15), NOW))
        assert "feed_needed" in met

    def test_succulent_floor_is_not_the_plants_own_minimum(self):
        """A succulent may be happy at 8% moisture, but no EC reading is
        trustworthy there — the absolute floor wins over the plant's own."""
        met = dict(range_conditions(self._hist(9.0, 10),
                                    make_threshold(min_soil_ec=100, min_soil_moist=7), NOW))
        assert "feed_needed" not in met

    def test_salt_buildup_is_not_gated_on_moisture(self):
        """Dry soil reads LOW, never high — a high reading is real regardless."""
        met = dict(range_conditions(self._hist(5.0, 4600),
                                    make_threshold(max_soil_ec=1500, min_soil_moist=15), NOW))
        assert "salt_buildup" in met
