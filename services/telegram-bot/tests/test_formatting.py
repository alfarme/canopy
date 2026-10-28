"""
Tests for message formatting and command parsing.
All tests are offline — no network, DB, or real bot API.
"""
import pytest
from datetime import datetime, timezone

from app.formatting import (
    format_alert,
    format_plants_list,
    format_status,
    format_assign_success,
    format_assign_error,
    format_sensors_list,
    format_battery,
    format_thresholds,
    format_identify_results,
)


class TestHtmlEscaping:
    """User/DB/API-derived strings must be HTML-escaped (parse_mode=HTML)."""

    def test_plant_name_with_angle_brackets_is_escaped(self):
        out = format_plants_list([
            {"display_name": "<b>evil</b>", "species": "A & B", "sensor_name": "3", "location": "sill"}
        ])
        assert "<b>evil</b>" not in out.replace("<b>&lt;", "")  # the raw tag must not survive
        assert "&lt;b&gt;evil&lt;/b&gt;" in out
        assert "A &amp; B" in out

    def test_status_escapes_names(self):
        out = format_status([
            {"sensor_name": "S<1", "plant_name": "Aloe & Co", "moisture": 20.0,
             "temp_c": 21.0, "light_lux": 100, "conductivity_us_cm": 200,
             "battery": 90, "last_seen": None}
        ])
        assert "S&lt;1" in out and "Aloe &amp; Co" in out

    def test_sensors_list_escapes(self):
        out = format_sensors_list([{"name": "x&y", "location": "<loc>", "plant_name": None, "last_seen": None}])
        assert "x&amp;y" in out and "&lt;loc&gt;" in out

    def test_thresholds_escapes_plant_name(self):
        out = format_thresholds("Fig <script>", {"min_soil_moist": 10, "max_soil_moist": 60})
        assert "Fig &lt;script&gt;" in out

    def test_identify_results_escapes_species(self):
        out = format_identify_results([{"scientific_name": "Ficus <x>", "common_names": ["Fig & co"], "score": 0.9}])
        assert "Ficus &lt;x&gt;" in out and "Fig &amp; co" in out

    def test_assign_error_escapes_ref(self):
        out = format_assign_error("plant_not_found", plant_ref="<b>x</b>")
        assert "&lt;b&gt;x&lt;/b&gt;" in out


class TestFormatAlert:
    def test_water_needed_alert(self):
        msg = format_alert(
            kind="water_needed",
            message="Rose is thirsty (moisture 18%)",
            sensor_name="Window-1",
            plant_name="Rose",
            alert_id=42,
        )
        assert "💧" in msg or "water" in msg.lower()
        assert "Rose" in msg
        assert "18%" in msg

    def test_low_battery_alert(self):
        msg = format_alert(
            kind="low_battery",
            message="Sensor battery at 12%",
            sensor_name="Balcony-2",
            plant_name=None,
            alert_id=7,
        )
        assert "🔋" in msg or "battery" in msg.lower()
        assert "12%" in msg

    def test_stale_alert(self):
        msg = format_alert(
            kind="stale",
            message="No reading for 6 hours",
            sensor_name="Kitchen-1",
            plant_name="Basil",
            alert_id=3,
        )
        assert "stale" in msg.lower() or "⚠" in msg or "🕐" in msg
        assert "Basil" in msg

    def test_new_sensor_alert(self):
        msg = format_alert(
            kind="new_sensor",
            message="New sensor detected: AA:BB:CC:DD:EE:FF",
            sensor_name="AA:BB:CC:DD:EE:FF",
            plant_name=None,
            alert_id=1,
        )
        assert "new" in msg.lower() or "🌱" in msg
        assert "AA:BB:CC:DD:EE:FF" in msg

    def test_out_of_range_alert(self):
        msg = format_alert(
            kind="out_of_range",
            message="Temperature 35°C exceeds max 30°C",
            sensor_name="Greenhouse",
            plant_name="Tomato",
            alert_id=99,
        )
        assert "Tomato" in msg or "35" in msg

    def test_stuck_alert(self):
        msg = format_alert(
            kind="stuck",
            message="Moisture reading stuck at 45% for 12h",
            sensor_name="Pot-3",
            plant_name="Ficus",
            alert_id=5,
        )
        assert "stuck" in msg.lower() or "⚠" in msg
        assert "Ficus" in msg

    def test_unknown_kind_does_not_crash(self):
        msg = format_alert(
            kind="unknown_future_kind",
            message="Something happened",
            sensor_name="Sensor-X",
            plant_name=None,
            alert_id=100,
        )
        assert isinstance(msg, str)
        assert len(msg) > 0


class TestFormatPlantsList:
    def test_empty_list(self):
        msg = format_plants_list([])
        assert "no plant" in msg.lower() or "empty" in msg.lower() or msg.strip() != ""

    def test_single_plant(self):
        plants = [
            {"id": 1, "display_name": "Rose", "species": "Rosa canina",
             "sensor_name": "Window-1", "location": "Balcony"}
        ]
        msg = format_plants_list(plants)
        assert "Rose" in msg
        assert "Window-1" in msg

    def test_multiple_plants(self):
        plants = [
            {"id": 1, "display_name": "Basil", "species": "Ocimum basilicum",
             "sensor_name": "Kitchen-1", "location": "Kitchen"},
            {"id": 2, "display_name": "Ficus", "species": None,
             "sensor_name": None, "location": None},
        ]
        msg = format_plants_list(plants)
        assert "Basil" in msg
        assert "Ficus" in msg

    def test_plant_without_sensor(self):
        plants = [
            {"id": 3, "display_name": "Orchid", "species": "Phalaenopsis",
             "sensor_name": None, "location": None}
        ]
        msg = format_plants_list(plants)
        assert "Orchid" in msg
        # Should not crash on None sensor


class TestFormatStatus:
    def test_status_with_readings(self):
        readings = [
            {
                "sensor_name": "Window-1",
                "plant_name": "Rose",
                "moisture": 42.5,
                "temp_c": 22.1,
                "light_lux": 3500,
                "conductivity_us_cm": 800,
                "battery": 75,
                "last_seen": datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc),
            }
        ]
        msg = format_status(readings)
        assert "Rose" in msg or "Window-1" in msg
        assert "42" in msg  # moisture
        assert "22" in msg  # temp

    def test_status_empty(self):
        msg = format_status([])
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_status_none_values(self):
        readings = [
            {
                "sensor_name": "Balcony-1",
                "plant_name": None,
                "moisture": None,
                "temp_c": None,
                "light_lux": None,
                "conductivity_us_cm": None,
                "battery": None,
                "last_seen": None,
            }
        ]
        msg = format_status(readings)
        assert "Balcony-1" in msg


class TestFormatAssign:
    def test_assign_success(self):
        msg = format_assign_success(sensor_name="Window-1", plant_name="Rose")
        assert "Window-1" in msg
        assert "Rose" in msg
        assert "assign" in msg.lower() or "✅" in msg

    def test_assign_error_sensor_not_found(self):
        msg = format_assign_error("sensor_not_found", sensor_ref="xyz")
        assert "xyz" in msg or "not found" in msg.lower() or "❌" in msg

    def test_assign_error_plant_not_found(self):
        msg = format_assign_error("plant_not_found", plant_ref="Cactus")
        assert "Cactus" in msg or "not found" in msg.lower() or "❌" in msg

    def test_assign_error_generic(self):
        msg = format_assign_error("db_error")
        assert isinstance(msg, str)
        assert len(msg) > 0


class TestFormatBattery:
    """Battery rendering for /sensors."""

    def test_healthy_battery_uses_full_icon(self):
        assert format_battery(84) == "🔋 84%"

    def test_low_battery_uses_empty_icon(self):
        assert format_battery(12) == "🪫 12%"

    def test_threshold_is_25(self):
        assert format_battery(25).startswith("🔋")
        assert format_battery(24).startswith("🪫")

    def test_unknown_battery_renders_nothing(self):
        """Most sensors have no battery most of the time — roughly half of the
        daily GATT connects fail, so a gap is normal and must not show as 0%."""
        assert format_battery(None) == ""

    def test_zero_is_not_confused_with_unknown(self):
        assert format_battery(0) == "🪫 0%"

    def test_non_numeric_is_ignored(self):
        assert format_battery("n/a") == ""

    def test_sensors_list_includes_battery(self):
        out = format_sensors_list([
            {"name": "7", "location": "Terrace", "plant_name": "Rose",
             "last_seen": None, "battery": 45},
        ])
        assert "🔋 45%" in out

    def test_sensors_list_without_battery_has_no_icon(self):
        out = format_sensors_list([
            {"name": "7", "location": "Terrace", "plant_name": "Rose", "last_seen": None},
        ])
        assert "🔋" not in out and "🪫" not in out


class TestAlertTitles:
    """Alert headlines name the action, not the measurement."""

    def test_range_kinds_have_actionable_titles(self):
        out = format_alert(kind="feed_needed", message="m", sensor_name="7",
                           plant_name="Rose", alert_id=1)
        assert "Fertiliser needed" in out
        assert "Feed Needed" not in out  # not the generic .title() fallback

    def test_light_direction_is_explicit(self):
        low = format_alert(kind="light_low", message="m", sensor_name=None,
                           plant_name=None, alert_id=1)
        high = format_alert(kind="light_high", message="m", sensor_name=None,
                            plant_name=None, alert_id=2)
        assert "Not enough light" in low
        assert "Too much light" in high

    def test_legacy_out_of_range_still_renders(self):
        """Old alert_events rows predate the split and must still display."""
        out = format_alert(kind="out_of_range", message="m", sensor_name=None,
                           plant_name=None, alert_id=1)
        assert "Out of range" in out

    def test_unknown_kind_falls_back(self):
        out = format_alert(kind="some_new_thing", message="m", sensor_name=None,
                           plant_name=None, alert_id=1)
        assert "Some New Thing" in out
