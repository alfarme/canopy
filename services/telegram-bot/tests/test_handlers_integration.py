"""
Integration tests: exercise every command handler end-to-end with a real
(in-memory SQLite) database and stubbed Telegram API.

These tests catch the class of bug that pure-function unit tests miss:
runtime errors in async handler code that only surface when actually awaited
with a real DB connection and Update/Context objects.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import (
    FakeAsyncConnection,
    make_fake_update,
    make_fake_callback_query,
    make_fake_context,
)


# ------------------------------------------------------------------ helpers

def _get_handlers(conn):
    """Import and build handlers with our fake connection."""
    from app.__main__ import _make_handlers
    from telegram.ext import CommandHandler
    result = {}
    for h in _make_handlers(conn):
        if isinstance(h, CommandHandler):
            for cmd in h.commands:
                result[cmd] = h.callback
    return result


def _get_button_menu(conn):
    """Extract the /menu CallbackQueryHandler's callback."""
    from app.__main__ import _make_handlers
    from telegram.ext import CallbackQueryHandler
    for h in _make_handlers(conn):
        if isinstance(h, CallbackQueryHandler):
            return h.callback
    raise AssertionError("no CallbackQueryHandler registered")


# ------------------------------------------------------------------ pending alerts (telegram delivery)

class TestFetchPendingAlerts:
    """'stuck' alerts are never delivered to Telegram — too frequent/low
    signal for chat; they're visible in Grafana instead."""

    @pytest.mark.asyncio
    async def test_excludes_stuck_kind(self, db):
        from app.__main__ import _fetch_pending_alerts

        db.seed_sensor(1, "balcony-1")
        db.seed_alert(1, "stuck", "stuck moisture")
        db.seed_alert(1, "low_battery", "battery low")

        alerts = await _fetch_pending_alerts(db)

        kinds = {a["kind"] for a in alerts}
        assert kinds == {"low_battery"}


# ------------------------------------------------------------------ /assign

class TestCmdAssign:
    """Integration tests for /assign handler."""

    @pytest.mark.asyncio
    async def test_assign_existing_plant_to_sensor(self, db, admin_env, monkeypatch):
        """Happy path: sensor + plant both exist → assign succeeds."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "")

        db.seed_sensor(1, "balcony-1")
        db.seed_plant(1, "Rose", species="Rosa")

        handlers = _get_handlers(db)
        update = make_fake_update("/assign balcony-1 Rose")
        ctx = make_fake_context()

        await handlers["assign"](update, ctx)

        update.message.reply_text.assert_awaited()
        reply = update.message.reply_text.call_args[0][0]
        assert "✅" in reply or "Assigned" in reply
        # Verify DB state
        assignments = db.get_assignments(1)
        assert len(assignments) == 1
        assert assignments[0]["plant_id"] == 1

    @pytest.mark.asyncio
    async def test_assign_unknown_sensor(self, db, admin_env, monkeypatch):
        """Sensor not found → error message."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "")

        handlers = _get_handlers(db)
        update = make_fake_update("/assign nonexistent Rose")
        ctx = make_fake_context()

        await handlers["assign"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply or "not found" in reply.lower()

    @pytest.mark.asyncio
    async def test_assign_bad_usage(self, db, admin_env, monkeypatch):
        """No arguments → usage message."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "")

        handlers = _get_handlers(db)
        update = make_fake_update("/assign")
        ctx = make_fake_context()

        await handlers["assign"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Usage" in reply or "usage" in reply.lower()

    @pytest.mark.asyncio
    async def test_assign_new_species_creates_plant(self, db, admin_env, monkeypatch):
        """Unknown plant name + no matching species → creates new plant."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "")

        db.seed_sensor(1, "balcony-1")

        handlers = _get_handlers(db)
        update = make_fake_update("/assign balcony-1 Monstera deliciosa")
        ctx = make_fake_context()

        await handlers["assign"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "✅" in reply or "Created" in reply
        # Should have created a plant
        assert db.count_plants() == 1
        # Should have assigned
        assignments = db.get_assignments(1)
        assert len(assignments) == 1

    @pytest.mark.asyncio
    async def test_assign_existing_species_reuses_plant(self, db, admin_env, monkeypatch):
        """Plant name doesn't match display_name but matches species → reuses existing."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "")

        db.seed_sensor(1, "balcony-1")
        db.seed_plant(1, "Swiss Cheese Plant", species="Monstera deliciosa")

        handlers = _get_handlers(db)
        update = make_fake_update("/assign balcony-1 Monstera deliciosa")
        ctx = make_fake_context()

        await handlers["assign"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "✅" in reply or "Assigned" in reply
        # Should NOT have created a new plant
        assert db.count_plants() == 1
        # Should have assigned to existing plant
        assignments = db.get_assignments(1)
        assert len(assignments) == 1
        assert assignments[0]["plant_id"] == 1

    @pytest.mark.asyncio
    async def test_assign_re_seeds_thresholds_from_opb(self, db, admin_env, monkeypatch):
        """Assigning a known species re-seeds thresholds from OpenPlantbook."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "test")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "test")

        db.seed_sensor(1, "balcony-1")
        db.seed_plant(1, "Rose", species="Rosa chinensis")
        db.seed_thresholds(1)

        # Mock the plantbook to return usable thresholds
        import app.api_clients as api_mod
        monkeypatch.setattr(api_mod, "fetch_plantbook_thresholds", AsyncMock(return_value={
            "min_soil_moist": 20, "max_soil_moist": 70,
            "min_soil_ec": 200, "max_soil_ec": 2000,
            "min_light_lux": 300, "max_light_lux": 40000,
            "min_temp": 10, "max_temp": 32,
        }))

        handlers = _get_handlers(db)
        update = make_fake_update("/assign balcony-1 Rose")
        ctx = make_fake_context()

        await handlers["assign"](update, ctx)

        # Should have re-seeded
        calls = update.message.reply_text.call_args_list
        all_text = " ".join(c[0][0] for c in calls)
        assert "Re-seeding" in all_text or "✅" in all_text


# ------------------------------------------------------------------ /unassign

class TestCmdUnassign:
    """Integration tests for /unassign handler."""

    @pytest.mark.asyncio
    async def test_unassign_existing(self, db, admin_env, monkeypatch):
        """Happy path: release an active assignment."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "balcony-1")
        db.seed_plant(1, "Rose")
        db.seed_assignment(1, 1)

        handlers = _get_handlers(db)
        update = make_fake_update("/unassign balcony-1")
        ctx = make_fake_context()

        await handlers["unassign"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "✅" in reply or "unassigned" in reply.lower()
        # Verify no active assignment
        assignments = db.get_assignments(1)
        assert len(assignments) == 0

    @pytest.mark.asyncio
    async def test_unassign_no_assignment(self, db, admin_env, monkeypatch):
        """Sensor has no assignment → informational message."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "balcony-1")

        handlers = _get_handlers(db)
        update = make_fake_update("/unassign balcony-1")
        ctx = make_fake_context()

        await handlers["unassign"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "no active" in reply.lower() or "ℹ️" in reply

    @pytest.mark.asyncio
    async def test_unassign_unknown_sensor(self, db, admin_env, monkeypatch):
        """Sensor not found → error."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        handlers = _get_handlers(db)
        update = make_fake_update("/unassign ghost")
        ctx = make_fake_context()

        await handlers["unassign"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply or "not found" in reply.lower()

    @pytest.mark.asyncio
    async def test_unassign_bad_usage(self, db, admin_env, monkeypatch):
        """No sensor arg → usage message."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        handlers = _get_handlers(db)
        update = make_fake_update("/unassign")
        ctx = make_fake_context()

        await handlers["unassign"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Usage" in reply or "usage" in reply.lower()


# ------------------------------------------------------------------ /plants

class TestCmdPlants:
    """Integration tests for /plants handler."""

    @pytest.mark.asyncio
    async def test_plants_empty(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        handlers = _get_handlers(db)
        update = make_fake_update("/plants")
        ctx = make_fake_context()

        await handlers["plants"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "No plants" in reply or "🌵" in reply

    @pytest.mark.asyncio
    async def test_plants_with_data(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "balcony-1")
        db.seed_plant(1, "Rose", species="Rosa")
        db.seed_assignment(1, 1)

        handlers = _get_handlers(db)
        update = make_fake_update("/plants")
        ctx = make_fake_context()

        await handlers["plants"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Rose" in reply

    @pytest.mark.asyncio
    async def test_plants_ordered_by_sensor_number_not_alphabetically(
        self, db, admin_env, monkeypatch
    ):
        """Numeric sensor names must sort as numbers (1, 4, 27), not as
        strings ("1", "27", "4") — reproduces a real scattered listing."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        for sensor_id, name in [(1, "27"), (2, "8"), (3, "5"), (4, "1")]:
            db.seed_sensor(sensor_id, name)
        for plant_id, sensor_id, display_name in [
            (1, 1, "Monstera"),
            (2, 2, "Anthurium"),
            (3, 3, "Aloe Vera"),
            (4, 4, "ZZ Plant"),
        ]:
            db.seed_plant(plant_id, display_name)
            db.seed_assignment(sensor_id, plant_id)

        handlers = _get_handlers(db)
        update = make_fake_update("/plants")
        ctx = make_fake_context()

        await handlers["plants"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        positions = [reply.index(name) for name in ["ZZ Plant", "Aloe Vera", "Anthurium", "Monstera"]]
        assert positions == sorted(positions), reply


# ------------------------------------------------------------------ /sensors

class TestCmdSensors:
    """Integration tests for /sensors handler."""

    @pytest.mark.asyncio
    async def test_sensors_empty(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        handlers = _get_handlers(db)
        update = make_fake_update("/sensors")
        ctx = make_fake_context()

        await handlers["sensors"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "No active" in reply or "📡" in reply

    @pytest.mark.asyncio
    async def test_sensors_with_data(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "balcony-1", location="Balcony")

        handlers = _get_handlers(db)
        update = make_fake_update("/sensors")
        ctx = make_fake_context()

        await handlers["sensors"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "balcony-1" in reply

    @pytest.mark.asyncio
    async def test_sensors_ordered_by_number_not_alphabetically(
        self, db, admin_env, monkeypatch
    ):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        for sensor_id, name in [(1, "27"), (2, "8"), (3, "5"), (4, "1")]:
            db.seed_sensor(sensor_id, name)

        handlers = _get_handlers(db)
        update = make_fake_update("/sensors")
        ctx = make_fake_context()

        await handlers["sensors"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        positions = [reply.index(f">{n}<") for n in ["1", "5", "8", "27"]]
        assert positions == sorted(positions), reply


# ------------------------------------------------------------------ /status

class TestCmdStatus:
    """Integration tests for /status handler."""

    @pytest.mark.asyncio
    async def test_status_no_readings(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        handlers = _get_handlers(db)
        update = make_fake_update("/status")
        ctx = make_fake_context()

        await handlers["status"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "No sensor readings" in reply or "📊" in reply

    @pytest.mark.asyncio
    async def test_status_with_readings(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "balcony-1")
        db.seed_plant(1, "Rose")
        db.seed_assignment(1, 1)
        db.seed_reading(1, moisture=42.5, temp_c=23.1)

        handlers = _get_handlers(db)
        update = make_fake_update("/status")
        ctx = make_fake_context()

        await handlers["status"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "balcony-1" in reply or "📊" in reply

    @pytest.mark.asyncio
    async def test_status_filters_to_one_sensor(self, db, admin_env, monkeypatch):
        """Regression: /status <sensor> previously ignored the argument and
        always showed every sensor. Must show only the requested one."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "balcony-1")
        db.seed_sensor(2, "kitchen-1")
        db.seed_plant(1, "Rose")
        db.seed_assignment(1, 1)
        db.seed_reading(1, moisture=42.5, temp_c=23.1)
        db.seed_reading(2, moisture=10.0, temp_c=19.0)

        handlers = _get_handlers(db)
        update = make_fake_update("/status balcony-1")
        ctx = make_fake_context()

        await handlers["status"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "balcony-1" in reply
        assert "kitchen-1" not in reply

    @pytest.mark.asyncio
    async def test_status_unknown_sensor(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        handlers = _get_handlers(db)
        update = make_fake_update("/status nonexistent")
        ctx = make_fake_context()

        await handlers["status"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "not found" in reply.lower()


# ------------------------------------------------------------------ /thresholds

class TestCmdThresholds:
    """Integration tests for /thresholds handler."""

    @pytest.mark.asyncio
    async def test_thresholds_no_args(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        handlers = _get_handlers(db)
        update = make_fake_update("/thresholds")
        ctx = make_fake_context()

        await handlers["thresholds"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Usage" in reply or "usage" in reply.lower()

    @pytest.mark.asyncio
    async def test_thresholds_plant_not_found(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        handlers = _get_handlers(db)
        update = make_fake_update("/thresholds Ghost Plant")
        ctx = make_fake_context()

        await handlers["thresholds"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "not found" in reply.lower()

    @pytest.mark.asyncio
    async def test_thresholds_no_thresholds_set(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_plant(1, "Rose")

        handlers = _get_handlers(db)
        update = make_fake_update("/thresholds Rose")
        ctx = make_fake_context()

        await handlers["thresholds"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "No thresholds" in reply or "ℹ️" in reply

    @pytest.mark.asyncio
    async def test_thresholds_with_data(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_plant(1, "Rose")
        db.seed_thresholds(1, min_soil_moist=20, max_soil_moist=70)

        handlers = _get_handlers(db)
        update = make_fake_update("/thresholds Rose")
        ctx = make_fake_context()

        await handlers["thresholds"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Rose" in reply
        assert "Moisture" in reply or "💧" in reply


# ------------------------------------------------------------------ /setthreshold

class TestCmdSetthreshold:
    """Integration tests for /setthreshold handler."""

    @pytest.mark.asyncio
    async def test_setthreshold_moisture(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_plant(1, "Rose")

        handlers = _get_handlers(db)
        update = make_fake_update("/setthreshold Rose moisture 20 70")
        ctx = make_fake_context()

        await handlers["setthreshold"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "✅" in reply or "Updated" in reply
        # Verify in DB
        t = db.get_thresholds(1)
        assert t is not None
        assert t["min_soil_moist"] == 20
        assert t["max_soil_moist"] == 70

    @pytest.mark.asyncio
    async def test_setthreshold_ec(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_plant(1, "Rose")
        db.seed_thresholds(1)

        handlers = _get_handlers(db)
        update = make_fake_update("/setthreshold Rose ec 100 2000")
        ctx = make_fake_context()

        await handlers["setthreshold"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "✅" in reply
        t = db.get_thresholds(1)
        assert t["min_soil_ec"] == 100
        assert t["max_soil_ec"] == 2000

    @pytest.mark.asyncio
    async def test_setthreshold_light(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_plant(1, "Rose")
        db.seed_thresholds(1)

        handlers = _get_handlers(db)
        update = make_fake_update("/setthreshold Rose light 500 30000")
        ctx = make_fake_context()

        await handlers["setthreshold"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "✅" in reply
        t = db.get_thresholds(1)
        assert t["min_light_lux"] == 500
        assert t["max_light_lux"] == 30000

    @pytest.mark.asyncio
    async def test_setthreshold_temp(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_plant(1, "Rose")
        db.seed_thresholds(1)

        handlers = _get_handlers(db)
        update = make_fake_update("/setthreshold Rose temp 5 40")
        ctx = make_fake_context()

        await handlers["setthreshold"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "✅" in reply
        t = db.get_thresholds(1)
        assert t["min_temp"] == 5
        assert t["max_temp"] == 40

    @pytest.mark.asyncio
    async def test_setthreshold_bad_param(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_plant(1, "Rose")

        handlers = _get_handlers(db)
        update = make_fake_update("/setthreshold Rose humidity 10 80")
        ctx = make_fake_context()

        await handlers["setthreshold"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Invalid parameter" in reply or "❌" in reply

    @pytest.mark.asyncio
    async def test_setthreshold_min_gt_max(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_plant(1, "Rose")

        handlers = _get_handlers(db)
        update = make_fake_update("/setthreshold Rose moisture 70 20")
        ctx = make_fake_context()

        await handlers["setthreshold"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Min" in reply or "❌" in reply

    @pytest.mark.asyncio
    async def test_setthreshold_non_numeric(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_plant(1, "Rose")

        handlers = _get_handlers(db)
        update = make_fake_update("/setthreshold Rose moisture abc 70")
        ctx = make_fake_context()

        await handlers["setthreshold"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply

    @pytest.mark.asyncio
    async def test_setthreshold_plant_not_found(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        handlers = _get_handlers(db)
        update = make_fake_update("/setthreshold Ghost moisture 10 60")
        ctx = make_fake_context()

        await handlers["setthreshold"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "not found" in reply.lower()


# ------------------------------------------------------------------ /silence, /unsilence, /silences

class TestCmdSilence:
    @pytest.mark.asyncio
    async def test_bare_silence_covers_every_kind(self, db, admin_env, monkeypatch):
        """/silence <sensor> with no kind shuts the sensor up entirely."""
        from app.commands import VALID_ALERT_KINDS
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "balcony-1")
        handlers = _get_handlers(db)
        update = make_fake_update("/silence balcony-1")
        ctx = make_fake_context()

        await handlers["silence"](update, ctx)

        assert db.count_silences() == len(VALID_ALERT_KINDS)
        reply = update.message.reply_text.call_args[0][0]
        assert "every" in reply.lower()
        assert "balcony-1" in reply

    @pytest.mark.asyncio
    async def test_explicit_kind_silences_only_that_kind(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "balcony-1")
        handlers = _get_handlers(db)

        await handlers["silence"](make_fake_update("/silence balcony-1 stale"), make_fake_context())

        assert db.count_silences() == 1

    @pytest.mark.asyncio
    async def test_bare_unsilence_lifts_every_kind(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "balcony-1")
        db.seed_silence(1, "stale")
        db.seed_silence(1, "water_needed")
        handlers = _get_handlers(db)

        await handlers["unsilence"](make_fake_update("/unsilence balcony-1"), make_fake_context())

        assert db.count_silences() == 0

    @pytest.mark.asyncio
    async def test_silence_unknown_sensor(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        handlers = _get_handlers(db)
        update = make_fake_update("/silence nonexistent")
        ctx = make_fake_context()

        await handlers["silence"](update, ctx)

        assert db.count_silences() == 0
        reply = update.message.reply_text.call_args[0][0]
        assert "not found" in reply.lower()

    @pytest.mark.asyncio
    async def test_silence_bad_kind(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "balcony-1")
        handlers = _get_handlers(db)
        update = make_fake_update("/silence balcony-1 bogus")
        ctx = make_fake_context()

        await handlers["silence"](update, ctx)

        assert db.count_silences() == 0
        reply = update.message.reply_text.call_args[0][0]
        assert "invalid" in reply.lower()

    @pytest.mark.asyncio
    async def test_unsilence_removes_it(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "balcony-1")
        db.seed_silence(1, "stuck")
        handlers = _get_handlers(db)
        update = make_fake_update("/unsilence balcony-1")
        ctx = make_fake_context()

        await handlers["unsilence"](update, ctx)

        assert db.count_silences() == 0
        reply = update.message.reply_text.call_args[0][0]
        assert "un-silenced" in reply.lower() or "unsilenced" in reply.lower()

    @pytest.mark.asyncio
    async def test_unsilence_when_not_silenced(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "balcony-1")
        handlers = _get_handlers(db)
        update = make_fake_update("/unsilence balcony-1")
        ctx = make_fake_context()

        await handlers["unsilence"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "weren't silenced" in reply.lower() or "not" in reply.lower()

    @pytest.mark.asyncio
    async def test_silences_lists_active_silences(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "balcony-1")
        db.seed_silence(1, "stuck")
        handlers = _get_handlers(db)
        update = make_fake_update("/silences")
        ctx = make_fake_context()

        await handlers["silences"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "balcony-1" in reply
        assert "stuck" in reply

    @pytest.mark.asyncio
    async def test_silences_empty(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        handlers = _get_handlers(db)
        update = make_fake_update("/silences")
        ctx = make_fake_context()

        await handlers["silences"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "no alerts" in reply.lower()


# ------------------------------------------------------------------ /help

class TestCmdHelp:
    """Integration tests for /help handler."""

    @pytest.mark.asyncio
    async def test_help_returns_content(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        handlers = _get_handlers(db)
        update = make_fake_update("/help")
        ctx = make_fake_context()

        await handlers["help"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "/assign" in reply
        assert "/help" in reply


class TestMenu:
    """Integration tests for /menu and its inline-keyboard button taps."""

    @pytest.mark.asyncio
    async def test_menu_shows_keyboard(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        handlers = _get_handlers(db)
        update = make_fake_update("/menu")
        ctx = make_fake_context()

        await handlers["menu"](update, ctx)

        _, kwargs = update.message.reply_text.call_args
        assert kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_menu_plants_button(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_plant(1, "Rose")
        button_menu = _get_button_menu(db)
        update = make_fake_callback_query("menu_plants")
        ctx = make_fake_context()

        await button_menu(update, ctx)

        update.callback_query.answer.assert_awaited_once()
        reply = update.callback_query.message.reply_text.call_args[0][0]
        assert "Rose" in reply

    @pytest.mark.asyncio
    async def test_menu_identify_button_prompts_for_photo(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        button_menu = _get_button_menu(db)
        update = make_fake_callback_query("menu_identify")
        ctx = make_fake_context()

        await button_menu(update, ctx)

        reply = update.callback_query.message.reply_text.call_args[0][0]
        assert "photo" in reply.lower()

    @pytest.mark.asyncio
    async def test_menu_manage_opens_submenu(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        button_menu = _get_button_menu(db)
        update = make_fake_callback_query("menu_manage")
        ctx = make_fake_context()

        await button_menu(update, ctx)

        update.callback_query.edit_message_text.assert_awaited_once()
        _, kwargs = update.callback_query.edit_message_text.call_args
        assert kwargs["reply_markup"] is not None
        # No new message should be sent — the submenu replaces the menu in place
        update.callback_query.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_menu_back_returns_to_main_menu(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        button_menu = _get_button_menu(db)
        update = make_fake_callback_query("menu_back")
        ctx = make_fake_context()

        await button_menu(update, ctx)

        update.callback_query.edit_message_text.assert_awaited_once()
        args, kwargs = update.callback_query.edit_message_text.call_args
        assert "Menu" in args[0]

    @pytest.mark.asyncio
    async def test_menu_mgr_unassign_shows_usage(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        button_menu = _get_button_menu(db)
        update = make_fake_callback_query("menu_mgr_unassign")
        ctx = make_fake_context()

        await button_menu(update, ctx)

        reply = update.callback_query.message.reply_text.call_args[0][0]
        assert "/unassign" in reply

    @pytest.mark.asyncio
    async def test_menu_mgr_silences_lists_active(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "balcony-1")
        db.seed_silence(1, "stuck")
        button_menu = _get_button_menu(db)
        update = make_fake_callback_query("menu_mgr_silences")
        ctx = make_fake_context()

        await button_menu(update, ctx)

        reply = update.callback_query.message.reply_text.call_args[0][0]
        assert "balcony-1" in reply


# ------------------------------------------------------------------ admin_only decorator

class TestAdminOnly:
    """Test that write commands reject non-admin users."""

    @pytest.mark.asyncio
    async def test_assign_rejects_non_admin(self, db, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_ID", "99999")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "s1")
        db.seed_plant(1, "Rose")

        handlers = _get_handlers(db)
        update = make_fake_update("/assign s1 Rose", user_id=12345)  # not admin
        ctx = make_fake_context()

        await handlers["assign"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Not authorized" in reply

    @pytest.mark.asyncio
    async def test_unassign_rejects_non_admin(self, db, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_ID", "99999")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        handlers = _get_handlers(db)
        update = make_fake_update("/unassign s1", user_id=12345)
        ctx = make_fake_context()

        await handlers["unassign"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Not authorized" in reply

    @pytest.mark.asyncio
    async def test_setthreshold_rejects_non_admin(self, db, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_ID", "99999")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        handlers = _get_handlers(db)
        update = make_fake_update("/setthreshold Rose moisture 10 60", user_id=12345)
        ctx = make_fake_context()

        await handlers["setthreshold"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Not authorized" in reply

    @pytest.mark.asyncio
    async def test_silence_rejects_non_admin(self, db, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_ID", "99999")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "s1")

        handlers = _get_handlers(db)
        update = make_fake_update("/silence s1", user_id=12345)
        ctx = make_fake_context()

        await handlers["silence"](update, ctx)

        assert db.count_silences() == 0
        reply = update.message.reply_text.call_args[0][0]
        assert "Not authorized" in reply

    @pytest.mark.asyncio
    async def test_no_admin_configured_denies_all(self, db, monkeypatch):
        """If TELEGRAM_ADMIN_USER_ID is not set, deny all write commands."""
        monkeypatch.delenv("TELEGRAM_ADMIN_USER_ID", raising=False)
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "s1")
        db.seed_plant(1, "Rose")

        handlers = _get_handlers(db)
        update = make_fake_update("/assign s1 Rose", user_id=12345)
        ctx = make_fake_context()

        await handlers["assign"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Not authorized" in reply


# ------------------------------------------------------------------ /delete

class TestCmdDeletePlant:
    """Integration tests for /delete handler."""

    @pytest.mark.asyncio
    async def test_delete_by_sensor_removes_all(self, db, admin_env, monkeypatch, tmp_path):
        """Delete via sensor ref removes plant, thresholds, assignments, photos, nulls alert_events."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setattr("app.photo_store.PHOTO_STORAGE_DIR", str(tmp_path))

        db.seed_sensor(1, "balcony-1")
        db.seed_plant(1, "Rose", species="Rosa")
        db.seed_assignment(1, 1)
        db.seed_thresholds(1)
        db.seed_alert_with_plant(1, 1, "water_needed", "needs water")

        # Create a photo file
        photo_file = tmp_path / "2026-07-20" / "test.jpg"
        photo_file.parent.mkdir(parents=True, exist_ok=True)
        photo_file.write_bytes(b"jpg-data")
        db.seed_identify_photo(10, file_path=str(photo_file), plant_id=1)

        handlers = _get_handlers(db)
        update = make_fake_update("/delete balcony-1")
        ctx = make_fake_context()

        await handlers["delete"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "🗑" in reply
        assert "Rose" in reply
        assert "1 photo" in reply

        # Plant gone
        assert db.get_plant(1) is None
        # Thresholds gone
        assert db.get_thresholds(1) is None
        # Assignment gone
        assert db.get_assignments(1) == []
        # Photo row gone
        assert db.get_identify_photos() == []
        # Photo file gone
        assert not photo_file.exists()
        # Alert event has plant_id nulled
        alerts = db.get_alert_events()
        assert alerts[0]["plant_id"] is None

    @pytest.mark.asyncio
    async def test_delete_by_plant_name(self, db, admin_env, monkeypatch):
        """Delete via plant display_name (case-insensitive)."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        db.seed_plant(1, "Rose", species="Rosa")
        db.seed_thresholds(1)

        handlers = _get_handlers(db)
        update = make_fake_update("/delete rose")
        ctx = make_fake_context()

        await handlers["delete"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "🗑" in reply
        assert db.get_plant(1) is None

    @pytest.mark.asyncio
    async def test_delete_not_found(self, db, admin_env, monkeypatch):
        """Unknown ref → error."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        handlers = _get_handlers(db)
        update = make_fake_update("/delete nonexistent")
        ctx = make_fake_context()

        await handlers["delete"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply or "No plant" in reply

    @pytest.mark.asyncio
    async def test_delete_bad_usage(self, db, admin_env, monkeypatch):
        """No args → usage."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        handlers = _get_handlers(db)
        update = make_fake_update("/delete")
        ctx = make_fake_context()

        await handlers["delete"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Usage" in reply or "usage" in reply.lower()


# ------------------------------------------------------------------ /rename

class TestCmdRename:
    """Integration tests for /rename handler."""

    @pytest.mark.asyncio
    async def test_rename_updates_display_name(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        db.seed_sensor(1, "balcony-1")
        db.seed_plant(1, "Rose", species="Rosa")
        db.seed_assignment(1, 1)

        handlers = _get_handlers(db)
        update = make_fake_update("/rename balcony-1 Beautiful Rose")
        ctx = make_fake_context()

        await handlers["rename"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "✏️" in reply
        assert "Beautiful Rose" in reply
        plant = db.get_plant(1)
        assert plant["display_name"] == "Beautiful Rose"

    @pytest.mark.asyncio
    async def test_rename_sensor_not_found(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        handlers = _get_handlers(db)
        update = make_fake_update("/rename ghost New Name")
        ctx = make_fake_context()

        await handlers["rename"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "not found" in reply.lower()

    @pytest.mark.asyncio
    async def test_rename_no_assignment(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        db.seed_sensor(1, "balcony-1")

        handlers = _get_handlers(db)
        update = make_fake_update("/rename balcony-1 New Name")
        ctx = make_fake_context()

        await handlers["rename"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "no active" in reply.lower()


# ------------------------------------------------------------------ /setlocation

class TestCmdSetLocation:
    """Integration tests for /setlocation handler."""

    @pytest.mark.asyncio
    async def test_setlocation_updates_sensor(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        db.seed_sensor(1, "balcony-1", location="Old")

        handlers = _get_handlers(db)
        update = make_fake_update("/setlocation balcony-1 Living Room")
        ctx = make_fake_context()

        await handlers["setlocation"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "📍" in reply
        assert "Living Room" in reply
        sensor = db.get_sensor(1)
        assert sensor["location"] == "Living Room"

    @pytest.mark.asyncio
    async def test_setlocation_sensor_not_found(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        handlers = _get_handlers(db)
        update = make_fake_update("/setlocation ghost Kitchen")
        ctx = make_fake_context()

        await handlers["setlocation"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "not found" in reply.lower()


# ------------------------------------------------------------------ New Manage buttons

class TestManageNewButtons:
    """Test that the new Manage buttons exist and reply with usage strings."""

    @pytest.mark.asyncio
    async def test_menu_mgr_delete_shows_usage(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        button_menu = _get_button_menu(db)
        update = make_fake_callback_query("menu_mgr_delete")
        ctx = make_fake_context()

        await button_menu(update, ctx)

        reply = update.callback_query.message.reply_text.call_args[0][0]
        assert "/delete" in reply

    @pytest.mark.asyncio
    async def test_menu_mgr_rename_shows_usage(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        button_menu = _get_button_menu(db)
        update = make_fake_callback_query("menu_mgr_rename")
        ctx = make_fake_context()

        await button_menu(update, ctx)

        reply = update.callback_query.message.reply_text.call_args[0][0]
        assert "/rename" in reply

    @pytest.mark.asyncio
    async def test_menu_mgr_setlocation_shows_usage(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        button_menu = _get_button_menu(db)
        update = make_fake_callback_query("menu_mgr_setlocation")
        ctx = make_fake_context()

        await button_menu(update, ctx)

        reply = update.callback_query.message.reply_text.call_args[0][0]
        assert "/setlocation" in reply


# ------------------------------------------------------------------ Bug fix regression tests

class TestBugfixSetlocationMultiword:
    """Bug fix: /setlocation <sensor_id> <multi-word location> must work."""

    @pytest.mark.asyncio
    async def test_setlocation_numeric_id_multiword(self, db, admin_env, monkeypatch):
        """Using numeric sensor ID with multi-word location should succeed."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        db.seed_sensor(28, "sensor-28", location="Old")

        handlers = _get_handlers(db)
        update = make_fake_update("/setlocation 28 living room")
        ctx = make_fake_context()

        await handlers["setlocation"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "📍" in reply
        assert "living room" in reply
        sensor = db.get_sensor(28)
        assert sensor["location"] == "living room"

    @pytest.mark.asyncio
    async def test_setlocation_three_word_location(self, db, admin_env, monkeypatch):
        """Location with 3+ words."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        db.seed_sensor(1, "balcony-1", location="Old")

        handlers = _get_handlers(db)
        update = make_fake_update("/setlocation balcony-1 south facing window sill")
        ctx = make_fake_context()

        await handlers["setlocation"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "📍" in reply
        assert "south facing window sill" in reply
        sensor = db.get_sensor(1)
        assert sensor["location"] == "south facing window sill"


class TestBugfixRenameTargetsOnlyOnePlant:
    """Bug fix: /rename must only rename the plant assigned to the given sensor,
    not all plants sharing the same old display_name."""

    @pytest.mark.asyncio
    async def test_rename_does_not_affect_other_plant_with_same_name(self, db, admin_env, monkeypatch):
        """Two plants with same name assigned to different sensors.
        Renaming via sensor 26 must NOT touch sensor 28's plant."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        db.seed_sensor(26, "sensor-26")
        db.seed_sensor(28, "sensor-28")
        db.seed_plant(1, "Dracaena reflexa", species="Dracaena reflexa")
        db.seed_plant(2, "Dracaena reflexa", species="Dracaena reflexa")
        db.seed_assignment(26, 1)
        db.seed_assignment(28, 2)

        handlers = _get_handlers(db)
        update = make_fake_update("/rename 26 Monstera Deliciosa")
        ctx = make_fake_context()

        await handlers["rename"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "✏️" in reply
        assert "Monstera Deliciosa" in reply

        # Plant 1 (sensor 26) was renamed
        plant1 = db.get_plant(1)
        assert plant1["display_name"] == "Monstera Deliciosa"

        # Plant 2 (sensor 28) is UNTOUCHED
        plant2 = db.get_plant(2)
        assert plant2["display_name"] == "Dracaena reflexa"

    @pytest.mark.asyncio
    async def test_rename_via_numeric_sensor_id(self, db, admin_env, monkeypatch):
        """Rename works with numeric sensor ID."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        db.seed_sensor(42, "sensor-42")
        db.seed_plant(1, "Fern", species="Nephrolepis")
        db.seed_assignment(42, 1)

        handlers = _get_handlers(db)
        update = make_fake_update("/rename 42 Boston Fern")
        ctx = make_fake_context()

        await handlers["rename"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "✏️" in reply
        plant = db.get_plant(1)
        assert plant["display_name"] == "Boston Fern"


class TestBugfixDeleteNumericSensorId:
    """Bug fix: /delete <sensor_id> (numeric) must work."""

    @pytest.mark.asyncio
    async def test_delete_by_numeric_sensor_id(self, db, admin_env, monkeypatch, tmp_path):
        """Delete using numeric sensor ID resolves to the assigned plant."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setattr("app.photo_store.PHOTO_STORAGE_DIR", str(tmp_path))

        db.seed_sensor(28, "sensor-28")
        db.seed_plant(1, "Rose", species="Rosa")
        db.seed_assignment(28, 1)

        handlers = _get_handlers(db)
        update = make_fake_update("/delete 28")
        ctx = make_fake_context()

        await handlers["delete"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "🗑" in reply
        assert "Rose" in reply
        assert db.get_plant(1) is None

    @pytest.mark.asyncio
    async def test_delete_prefers_name_match_over_colliding_id(self, db, admin_env, monkeypatch, tmp_path):
        """
        Sensor names are the identifier shown in /plants and /sensors, and can
        themselves be numeric strings (e.g. MiFlora walk-test naming). When a
        sensor's *name* collides with a *different* sensor's primary-key id,
        the name match must win — resolving by raw id first would silently
        target the wrong sensor's plant.
        """
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setattr("app.photo_store.PHOTO_STORAGE_DIR", str(tmp_path))

        # sensor id=26 is named "10" (no relation to "26"); sensor id=23 is
        # named "26" — this is the sensor the user means by "/delete 26".
        db.seed_sensor(26, "10")
        db.seed_sensor(23, "26")
        db.seed_plant(1, "Wrong Plant (id=26)", species="Ficus")
        db.seed_plant(2, "Right Plant (name=26)", species="Monstera")
        db.seed_assignment(26, 1)
        db.seed_assignment(23, 2)

        handlers = _get_handlers(db)
        update = make_fake_update("/delete 26")
        ctx = make_fake_context()

        await handlers["delete"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Right Plant" in reply
        assert db.get_plant(2) is None
        assert db.get_plant(1) is not None  # untouched — id=26's plant survives

    @pytest.mark.asyncio
    async def test_delete_invalid_sensor_id_shows_clear_error(self, db, admin_env, monkeypatch):
        """Unknown numeric sensor ID gives helpful error."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        handlers = _get_handlers(db)
        update = make_fake_update("/delete 999")
        ctx = make_fake_context()

        await handlers["delete"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply
        assert "999" in reply

    @pytest.mark.asyncio
    async def test_delete_sensor_no_assignment_falls_back_to_plant_name(self, db, admin_env, monkeypatch, tmp_path):
        """If sensor has no active assignment, falls back to plant name lookup."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setattr("app.photo_store.PHOTO_STORAGE_DIR", str(tmp_path))

        db.seed_plant(1, "Orchid", species="Phalaenopsis")

        handlers = _get_handlers(db)
        update = make_fake_update("/delete Orchid")
        ctx = make_fake_context()

        await handlers["delete"](update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "🗑" in reply
        assert "Orchid" in reply
        assert db.get_plant(1) is None


# ------------------------------------------------------------------ /removesensor

class TestCmdAddSensor:
    """Naming an auto-registered sensor — the step that makes it legible
    everywhere else, since sensor_labels falls back to the raw MAC until then."""

    @pytest.mark.asyncio
    async def test_names_unnamed_sensor_by_mac(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, None, mac="A4:C1:38:AA:BB:CC")
        handlers = _get_handlers(db)
        update = make_fake_update("/addsensor A4:C1:38:AA:BB:CC 25")

        await handlers["addsensor"](update, make_fake_context())

        assert db.sensor_name(1) == "25"
        reply = update.message.reply_text.call_args[0][0]
        assert "25" in reply

    @pytest.mark.asyncio
    async def test_lowercase_mac_still_matches(self, db, admin_env, monkeypatch):
        """Ingest upper-cases MACs; a hand-typed one should still land."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, None, mac="A4:C1:38:AA:BB:CC")
        handlers = _get_handlers(db)
        update = make_fake_update("/addsensor a4:c1:38:aa:bb:cc 25")

        await handlers["addsensor"](update, make_fake_context())

        assert db.sensor_name(1) == "25"

    @pytest.mark.asyncio
    async def test_duplicate_number_rejected(self, db, admin_env, monkeypatch):
        """Two sensors sharing a name would make /assign, /status and every
        label ambiguous — _find_sensor resolves by name first."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "25")
        db.seed_sensor(2, None, mac="A4:C1:38:AA:BB:CC")
        handlers = _get_handlers(db)
        update = make_fake_update("/addsensor A4:C1:38:AA:BB:CC 25")

        await handlers["addsensor"](update, make_fake_context())

        assert db.sensor_name(2) is None
        assert "already taken" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_renumbering_same_sensor_is_allowed(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, "25", mac="A4:C1:38:AA:BB:CC")
        handlers = _get_handlers(db)
        update = make_fake_update("/addsensor 25 25")

        await handlers["addsensor"](update, make_fake_context())

        assert db.sensor_name(1) == "25"

    @pytest.mark.asyncio
    async def test_retired_sensor_rejected(self, db, admin_env, monkeypatch):
        """A tombstoned sensor is ignored by ingest for good — naming it would
        suggest it is coming back."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, None, mac="A4:C1:38:AA:BB:CC", active=False)
        handlers = _get_handlers(db)
        update = make_fake_update("/addsensor A4:C1:38:AA:BB:CC 25")

        await handlers["addsensor"](update, make_fake_context())

        assert db.sensor_name(1) is None
        assert "retired" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_unknown_mac(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        handlers = _get_handlers(db)
        update = make_fake_update("/addsensor A4:C1:38:00:00:00 25")

        await handlers["addsensor"](update, make_fake_context())

        assert "advertised" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_bad_number_reports_reason(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        db.seed_sensor(1, None, mac="A4:C1:38:AA:BB:CC")
        handlers = _get_handlers(db)
        update = make_fake_update("/addsensor A4:C1:38:AA:BB:CC balcony")

        await handlers["addsensor"](update, make_fake_context())

        assert db.sensor_name(1) is None
        assert "not a number" in update.message.reply_text.call_args[0][0]


class TestCmdRemoveSensor:
    """Purge a sensor's data but leave a deactivated tombstone behind — see
    _purge_sensor for why the row must not be deleted outright."""

    def _seed(self, db):
        db.seed_sensor(1, "balcony-1")
        db.seed_plant(1, "Rose")
        db.seed_assignment(1, 1)
        db.seed_reading(1)
        db.seed_alert(1, "stale")
        db.seed_silence(1, "stale")

    @pytest.mark.asyncio
    async def test_without_confirm_destroys_nothing(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        self._seed(db)
        handlers = _get_handlers(db)
        update = make_fake_update("/removesensor balcony-1")

        await handlers["removesensor"](update, make_fake_context())

        assert db.count("bt_readings", 1) == 1
        assert db.sensor_active(1) is True
        reply = update.message.reply_text.call_args[0][0]
        assert "confirm" in reply.lower()
        assert "Rose" in reply          # warns that the assignment goes

    @pytest.mark.asyncio
    async def test_confirm_purges_and_deactivates(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        self._seed(db)
        db.seed_identify_photo(1, sensor_id=1)
        handlers = _get_handlers(db)
        update = make_fake_update("/removesensor balcony-1 confirm")

        await handlers["removesensor"](update, make_fake_context())

        for table in ("bt_readings", "plant_assignments",
                      "alert_events", "alert_silences"):
            assert db.count(table, 1) == 0, table
        # The row survives, deactivated: deleting it is what let rogue BLE
        # sensors re-register and re-alert (c1306a1).
        assert db.sensor_active(1) is False
        # The plant itself is not collateral damage.
        assert db.count("plants") == 1
        # Photos keep their row (and their file), just unlinked from the sensor.
        assert db.count("identify_photos") == 1
        assert db.count("identify_photos", 1) == 0

    @pytest.mark.asyncio
    async def test_unknown_sensor(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        handlers = _get_handlers(db)
        update = make_fake_update("/removesensor nope confirm")

        await handlers["removesensor"](update, make_fake_context())

        reply = update.message.reply_text.call_args[0][0]
        assert "not found" in reply.lower()
