"""
Integration tests: photo-identify ConversationHandler state machine.

Exercises the full flow: any photo message → PlantNet results →
assign/cancel, sensor lookup, duplicate species handling
(reuse/create-new), and authorization checks at each entry point.
No /identify or /id command — a photo alone triggers the flow.
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

# Shared test data
PLANTNET_RESULTS = [
    {
        "scientific_name": "Monstera deliciosa",
        "common_names": ["Swiss cheese plant", "Ceriman"],
        "score": 0.952,
    },
    {
        "scientific_name": "Philodendron bipinnatifidum",
        "common_names": ["Tree philodendron"],
        "score": 0.031,
    },
]


def _make_photo_update(user_id=12345, has_photo=True, reply_to_photo=False):
    """Create an Update with a photo attached (for the auto-identify entry)."""
    update = make_fake_update("", user_id=user_id)

    if has_photo:
        fake_photo = MagicMock()
        fake_photo.file_unique_id = "ABC123"
        fake_file = MagicMock()
        fake_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"fake-jpeg-data"))
        fake_photo.get_file = AsyncMock(return_value=fake_file)

        if reply_to_photo:
            update.message.photo = []
            update.message.reply_to_message = MagicMock()
            update.message.reply_to_message.photo = [fake_photo]
        else:
            update.message.photo = [fake_photo]
            update.message.reply_to_message = None
    else:
        update.message.photo = []
        update.message.reply_to_message = None

    return update


def _get_identify_funcs(conn):
    """
    Extract the inner functions of build_identify_handler for direct invocation.

    We can't easily drive the ConversationHandler state machine through PTB's
    dispatcher without a running Application, so we extract the handler functions
    and call them directly. This mirrors what PTB does internally.
    """
    from app.identify import build_identify_handler, AWAITING_CHOICE, AWAITING_SENSOR, AWAITING_DUPLICATE_CHOICE, AWAITING_LOCATION
    from app.access import is_admin
    from telegram.ext import ConversationHandler

    handler = build_identify_handler(conn, is_admin)

    # Extract handlers from the ConversationHandler
    # entry_points[0] is MessageHandler(filters.PHOTO, handle_photo) — the
    # sole entry point; any photo triggers identification automatically.
    handle_photo = handler.entry_points[0].callback

    # states
    button_assign = handler.states[AWAITING_CHOICE][0].callback  # identify_assign
    button_cancel = handler.states[AWAITING_CHOICE][1].callback  # identify_cancel
    receive_sensor = handler.states[AWAITING_SENSOR][0].callback
    handle_duplicate_choice = handler.states[AWAITING_DUPLICATE_CHOICE][0].callback

    return {
        "handle_photo": handle_photo,
        "button_assign": button_assign,
        "button_cancel": button_cancel,
        "receive_sensor": receive_sensor,
        "handle_duplicate_choice": handle_duplicate_choice,
        "AWAITING_CHOICE": AWAITING_CHOICE,
        "AWAITING_SENSOR": AWAITING_SENSOR,
        "AWAITING_DUPLICATE_CHOICE": AWAITING_DUPLICATE_CHOICE,
        "AWAITING_LOCATION": AWAITING_LOCATION,
        "END": ConversationHandler.END,
    }


# ------------------------------------------------------------------ auto photo entry

class TestIdentifyEntry:
    """Tests for the automatic photo entry point (no /identify or /id command)."""

    def test_entry_point_is_photo_only_no_command(self, db):
        """There must be exactly one entry point: any photo message. No
        CommandHandler for /identify or /id — those commands don't exist."""
        from app.identify import build_identify_handler
        from app.access import is_admin
        from telegram.ext import MessageHandler, filters as ptb_filters

        handler = build_identify_handler(db, is_admin)
        assert len(handler.entry_points) == 1
        entry = handler.entry_points[0]
        assert isinstance(entry, MessageHandler)
        assert entry.filters == ptb_filters.PHOTO

    @pytest.mark.asyncio
    async def test_photo_with_no_caption_still_identifies(self, db, admin_env, monkeypatch):
        """A bare photo, no caption or command, must trigger identification —
        this is the whole point of the auto-trigger."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        import app.identify as identify_mod
        monkeypatch.setattr(identify_mod, "identify_plant", AsyncMock(return_value=PLANTNET_RESULTS))

        funcs = _get_identify_funcs(db)
        update = _make_photo_update()
        assert update.message.caption is None

        result = await funcs["handle_photo"](update, make_fake_context())

        assert result == funcs["AWAITING_CHOICE"]

    @pytest.mark.asyncio
    async def test_identify_no_photo_tells_user(self, db, admin_env, monkeypatch):
        """No photo attached → tell user, end conversation."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "")

        funcs = _get_identify_funcs(db)
        update = _make_photo_update(has_photo=False)
        ctx = make_fake_context()

        result = await funcs["handle_photo"](update, ctx)

        assert result == funcs["END"]
        reply = update.message.reply_text.call_args[0][0]
        assert "photo" in reply.lower()

    @pytest.mark.asyncio
    async def test_identify_non_admin_rejected(self, db, monkeypatch):
        """Non-admin user → rejected."""
        monkeypatch.setenv("TELEGRAM_ADMIN_USER_ID", "99999")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        funcs = _get_identify_funcs(db)
        update = _make_photo_update(user_id=12345)
        ctx = make_fake_context()

        result = await funcs["handle_photo"](update, ctx)

        assert result == funcs["END"]
        reply = update.message.reply_text.call_args[0][0]
        assert "Not authorized" in reply

    @pytest.mark.asyncio
    async def test_identify_with_photo_shows_results(self, db, admin_env, monkeypatch):
        """Photo attached + PlantNet returns results → show them with keyboard."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        import app.identify as identify_mod
        monkeypatch.setattr(identify_mod, "identify_plant", AsyncMock(return_value=PLANTNET_RESULTS))

        funcs = _get_identify_funcs(db)
        update = _make_photo_update()
        ctx = make_fake_context()

        result = await funcs["handle_photo"](update, ctx)

        assert result == funcs["AWAITING_CHOICE"]
        # Should have stored results
        assert "identify_results" in ctx.user_data
        assert len(ctx.user_data["identify_results"]) == 2
        # Last reply_text should have the results formatted
        calls = update.message.reply_text.call_args_list
        last_call_text = calls[-1][0][0]
        assert "Monstera" in last_call_text

    @pytest.mark.asyncio
    async def test_identify_sends_reference_photos_for_top_matches(self, db, admin_env, monkeypatch):
        """Wikipedia lookups resolving to thumbnails → sent as a media group
        so the user can compare the top hits, ranked and captioned."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        import app.identify as identify_mod
        monkeypatch.setattr(identify_mod, "identify_plant", AsyncMock(return_value=PLANTNET_RESULTS))
        monkeypatch.setattr(
            identify_mod, "fetch_reference_image",
            AsyncMock(side_effect=lambda species: f"https://example.org/{species.replace(' ', '_')}.jpg"),
        )

        funcs = _get_identify_funcs(db)
        update = _make_photo_update()
        ctx = make_fake_context()

        result = await funcs["handle_photo"](update, ctx)

        assert result == funcs["AWAITING_CHOICE"]
        update.message.reply_media_group.assert_awaited_once()
        media = update.message.reply_media_group.call_args[0][0]
        assert len(media) == 2
        assert media[0].caption.startswith("#1")
        assert "Monstera" in media[0].caption
        assert media[1].caption.startswith("#2")

    @pytest.mark.asyncio
    async def test_identify_skips_media_group_without_images(self, db, admin_env, monkeypatch):
        """No Wikipedia thumbnail found for any candidate → no media group
        sent, text/button flow unaffected."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        import app.identify as identify_mod
        monkeypatch.setattr(identify_mod, "identify_plant", AsyncMock(return_value=PLANTNET_RESULTS))
        monkeypatch.setattr(identify_mod, "fetch_reference_image", AsyncMock(return_value=None))

        funcs = _get_identify_funcs(db)
        update = _make_photo_update()
        ctx = make_fake_context()

        result = await funcs["handle_photo"](update, ctx)

        assert result == funcs["AWAITING_CHOICE"]
        update.message.reply_media_group.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_identify_media_group_failure_does_not_break_flow(self, db, admin_env, monkeypatch):
        """Telegram rejecting the media group (e.g. bad URL, CDN fetch fails
        on Telegram's side) must not crash the identify flow — the
        text/button prompt still lands."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        import app.identify as identify_mod
        monkeypatch.setattr(identify_mod, "identify_plant", AsyncMock(return_value=PLANTNET_RESULTS))
        monkeypatch.setattr(
            identify_mod, "fetch_reference_image",
            AsyncMock(return_value="https://example.org/photo.jpg"),
        )

        funcs = _get_identify_funcs(db)
        update = _make_photo_update()
        update.message.reply_media_group = AsyncMock(side_effect=Exception("telegram CDN error"))
        ctx = make_fake_context()

        result = await funcs["handle_photo"](update, ctx)

        assert result == funcs["AWAITING_CHOICE"]
        calls = update.message.reply_text.call_args_list
        assert "Monstera" in calls[-1][0][0]

    @pytest.mark.asyncio
    async def test_identify_plantnet_error(self, db, admin_env, monkeypatch):
        """PlantNet API error → friendly error message."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        import app.api_clients as api_mod
        monkeypatch.setattr(api_mod, "identify_plant", AsyncMock(side_effect=Exception("API down")))

        funcs = _get_identify_funcs(db)
        update = _make_photo_update()
        ctx = make_fake_context()

        result = await funcs["handle_photo"](update, ctx)

        assert result == funcs["END"]
        calls = update.message.reply_text.call_args_list
        last_text = calls[-1][0][0]
        assert "failed" in last_text.lower() or "❌" in last_text

    @pytest.mark.asyncio
    async def test_identify_no_matches(self, db, admin_env, monkeypatch):
        """PlantNet returns empty results → tell user to try again."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        import app.identify as identify_mod
        monkeypatch.setattr(identify_mod, "identify_plant", AsyncMock(return_value=[]))

        funcs = _get_identify_funcs(db)
        update = _make_photo_update()
        ctx = make_fake_context()

        result = await funcs["handle_photo"](update, ctx)

        assert result == funcs["END"]
        calls = update.message.reply_text.call_args_list
        last_text = calls[-1][0][0]
        assert "No matches" in last_text or "clearer" in last_text.lower()

    @pytest.mark.asyncio
    async def test_identify_reply_to_photo(self, db, admin_env, monkeypatch):
        """Reply-to-photo entry → works the same as direct photo."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        import app.identify as identify_mod
        monkeypatch.setattr(identify_mod, "identify_plant", AsyncMock(return_value=PLANTNET_RESULTS))

        funcs = _get_identify_funcs(db)
        update = _make_photo_update(reply_to_photo=True)
        ctx = make_fake_context()

        result = await funcs["handle_photo"](update, ctx)

        assert result == funcs["AWAITING_CHOICE"]


# ------------------------------------------------------------------ Assign button → sensor prompt

class TestIdentifyAssignButton:
    """Tests for the 'Assign to sensor' button callback."""

    @pytest.mark.asyncio
    async def test_button_assign_prompts_sensor(self, db, admin_env, monkeypatch):
        """Tap 'Assign to sensor' → ask for sensor number."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        funcs = _get_identify_funcs(db)
        update = make_fake_callback_query("identify_assign")
        ctx = make_fake_context(user_data={"identify_results": PLANTNET_RESULTS})

        result = await funcs["button_assign"](update, ctx)

        assert result == funcs["AWAITING_SENSOR"]
        update.callback_query.answer.assert_awaited()
        reply = update.callback_query.message.reply_text.call_args[0][0]
        assert "sensor" in reply.lower()


# ------------------------------------------------------------------ Cancel button

class TestIdentifyCancel:
    """Tests for the 'Cancel' button callback."""

    @pytest.mark.asyncio
    async def test_button_cancel_ends_conversation(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        funcs = _get_identify_funcs(db)
        update = make_fake_callback_query("identify_cancel")
        ctx = make_fake_context(user_data={"identify_results": PLANTNET_RESULTS})

        result = await funcs["button_cancel"](update, ctx)

        assert result == funcs["END"]
        # Results should be cleaned up
        assert "identify_results" not in ctx.user_data


# ------------------------------------------------------------------ Sensor number → assign

class TestIdentifyReceiveSensor:
    """Tests for the sensor-number reply in AWAITING_SENSOR state."""

    @pytest.mark.asyncio
    async def test_valid_sensor_creates_plant_and_assigns(self, db, admin_env, monkeypatch):
        """Valid sensor number, no duplicate species → create plant + assign."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "")

        db.seed_sensor(1, "balcony-1")

        funcs = _get_identify_funcs(db)
        update = make_fake_update("balcony-1")
        ctx = make_fake_context(user_data={"identify_results": PLANTNET_RESULTS})

        result = await funcs["receive_sensor"](update, ctx)

        assert result == funcs["AWAITING_LOCATION"]
        # Plant should have been created
        assert db.count_plants() == 1
        # Should be assigned
        assignments = db.get_assignments(1)
        assert len(assignments) == 1
        # Confirmation message
        reply_calls = update.message.reply_text.call_args_list
        assert any("✅" in c[0][0] or "Done" in c[0][0] for c in reply_calls)

    @pytest.mark.asyncio
    async def test_valid_sensor_links_saved_identify_photos(self, db, admin_env, monkeypatch):
        """Photos saved during identification get sensor/plant name attached
        once the sensor is assigned (future training-dataset feature)."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "")

        db.seed_sensor(1, "balcony-1")
        db.seed_identify_photo(10, species="Monstera deliciosa")

        funcs = _get_identify_funcs(db)
        update = make_fake_update("balcony-1")
        ctx = make_fake_context(user_data={
            "identify_results": PLANTNET_RESULTS,
            "identify_photo_ids": [10],
        })

        await funcs["receive_sensor"](update, ctx)

        rows = {r["id"]: r for r in db.get_identify_photos()}
        assert rows[10]["sensor_id"] == 1
        assert rows[10]["sensor_name"] == "balcony-1"
        assert rows[10]["plant_name"] == "Swiss cheese plant"

    @pytest.mark.asyncio
    async def test_invalid_sensor_stays_in_state(self, db, admin_env, monkeypatch):
        """Invalid sensor → error, stays in AWAITING_SENSOR."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        funcs = _get_identify_funcs(db)
        update = make_fake_update("nonexistent-sensor")
        ctx = make_fake_context(user_data={"identify_results": PLANTNET_RESULTS})

        result = await funcs["receive_sensor"](update, ctx)

        assert result == funcs["AWAITING_SENSOR"]
        reply = update.message.reply_text.call_args[0][0]
        assert "not found" in reply.lower() or "❌" in reply

    @pytest.mark.asyncio
    async def test_duplicate_species_prompts_choice(self, db, admin_env, monkeypatch):
        """Sensor found + species already exists → ask reuse/create-new."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "")

        db.seed_sensor(1, "balcony-1")
        db.seed_plant(1, "Swiss Cheese", species="Monstera deliciosa")

        funcs = _get_identify_funcs(db)
        update = make_fake_update("balcony-1")
        ctx = make_fake_context(user_data={"identify_results": PLANTNET_RESULTS})

        result = await funcs["receive_sensor"](update, ctx)

        assert result == funcs["AWAITING_DUPLICATE_CHOICE"]
        reply = update.message.reply_text.call_args[0][0]
        assert "already exists" in reply.lower() or "Reuse" in reply

    @pytest.mark.asyncio
    async def test_no_results_in_context_ends(self, db, admin_env, monkeypatch):
        """If identify_results is missing from user_data → end conversation."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        funcs = _get_identify_funcs(db)
        update = make_fake_update("balcony-1")
        ctx = make_fake_context(user_data={})  # no results

        result = await funcs["receive_sensor"](update, ctx)

        assert result == funcs["END"]


# ------------------------------------------------------------------ Duplicate choice

class TestIdentifyDuplicateChoice:
    """Tests for the reuse/create-new duplicate species decision."""

    @pytest.mark.asyncio
    async def test_reuse_existing_plant(self, db, admin_env, monkeypatch):
        """User picks 'Reuse' → assign to existing plant."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "")

        db.seed_sensor(1, "balcony-1")
        db.seed_plant(1, "Swiss Cheese", species="Monstera deliciosa")
        db.seed_identify_photo(20, species="Monstera deliciosa")

        import app.api_clients as api_mod
        monkeypatch.setattr(api_mod, "fetch_plantbook_thresholds", AsyncMock(return_value=None))

        funcs = _get_identify_funcs(db)
        update = make_fake_callback_query("dup_reuse")
        ctx = make_fake_context(user_data={
            "identify_results": PLANTNET_RESULTS,
            "identify_sensor": {"id": 1, "name": "balcony-1"},
            "identify_species": "Monstera deliciosa",
            "identify_display_name": "Swiss cheese plant",
            "existing_plant": {"id": 1, "display_name": "Swiss Cheese"},
            "identify_photo_ids": [20],
        })

        result = await funcs["handle_duplicate_choice"](update, ctx)

        assert result == funcs["AWAITING_LOCATION"]
        # Should have assigned to existing plant (id=1)
        assignments = db.get_assignments(1)
        assert len(assignments) == 1
        assert assignments[0]["plant_id"] == 1
        # Should not have created a new plant
        assert db.count_plants() == 1
        # Cleanup
        assert "identify_results" not in ctx.user_data
        assert "identify_photo_ids" not in ctx.user_data
        # Saved photo linked to the sensor + reused plant name
        photo = db.get_identify_photos()[0]
        assert photo["sensor_id"] == 1
        assert photo["plant_name"] == "Swiss Cheese"

    @pytest.mark.asyncio
    async def test_create_new_plant(self, db, admin_env, monkeypatch):
        """User picks 'Create new' → new plant suffixed with its own sensor
        number (not a running count), and the existing bare plant gets
        backfilled with its sensor's number too."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "")

        db.seed_sensor(1, "5")
        db.seed_sensor(2, "14")
        db.seed_plant(1, "Swiss Cheese", species="Monstera deliciosa")
        db.seed_assignment(1, 1)
        db.seed_identify_photo(21, species="Monstera deliciosa")

        import app.api_clients as api_mod
        monkeypatch.setattr(api_mod, "fetch_plantbook_thresholds", AsyncMock(return_value=None))

        funcs = _get_identify_funcs(db)
        update = make_fake_callback_query("dup_new")
        ctx = make_fake_context(user_data={
            "identify_results": PLANTNET_RESULTS,
            "identify_sensor": {"id": 2, "name": "14"},
            "identify_species": "Monstera deliciosa",
            "identify_display_name": "Swiss cheese plant",
            "existing_plant": {"id": 1, "display_name": "Swiss Cheese"},
            "identify_photo_ids": [21],
        })

        result = await funcs["handle_duplicate_choice"](update, ctx)

        assert result == funcs["AWAITING_LOCATION"]
        # Should have created a NEW plant (total 2)
        assert db.count_plants() == 2
        # Saved photo linked to sensor 2 + the new plant name, suffixed with
        # its own sensor number (14), not a running count
        photo = db.get_identify_photos()[0]
        assert photo["sensor_id"] == 2
        assert photo["plant_name"] == "Swiss cheese plant (#14)"
        # Should have assigned sensor 2 to the new plant
        assignments = db.get_assignments(2)
        assert len(assignments) == 1
        assert assignments[0]["plant_id"] != 1  # different from existing
        # The existing bare plant was backfilled with its own sensor's number
        existing = db.get_plant(1)
        assert existing["display_name"] == "Swiss Cheese (#5)"
        # Cleanup
        assert "identify_results" not in ctx.user_data

    @pytest.mark.asyncio
    async def test_reuse_with_opb_thresholds(self, db, admin_env, monkeypatch):
        """Reuse + OpenPlantbook has thresholds → re-seeds them."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "test")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "test")

        db.seed_sensor(1, "balcony-1")
        db.seed_plant(1, "Swiss Cheese", species="Monstera deliciosa")
        db.seed_thresholds(1, min_soil_moist=10, max_soil_moist=50)

        opb_thresholds = {
            "min_soil_moist": 25, "max_soil_moist": 75,
            "min_soil_ec": 200, "max_soil_ec": 2500,
            "min_light_lux": 500, "max_light_lux": 3000,
            "min_temp": 15, "max_temp": 30,
        }
        import app.identify as identify_mod
        monkeypatch.setattr(identify_mod, "fetch_plantbook_thresholds", AsyncMock(return_value=opb_thresholds))
        monkeypatch.setattr(identify_mod, "has_usable_thresholds", lambda t: t is not None and any(v is not None for v in t.values()))

        funcs = _get_identify_funcs(db)
        update = make_fake_callback_query("dup_reuse")
        ctx = make_fake_context(user_data={
            "identify_results": PLANTNET_RESULTS,
            "identify_sensor": {"id": 1, "name": "balcony-1"},
            "identify_species": "Monstera deliciosa",
            "identify_display_name": "Swiss cheese plant",
            "existing_plant": {"id": 1, "display_name": "Swiss Cheese"},
        })

        result = await funcs["handle_duplicate_choice"](update, ctx)

        assert result == funcs["AWAITING_LOCATION"]
        # Thresholds should have been re-seeded
        t = db.get_thresholds(1)
        assert t["min_soil_moist"] == 25
        assert t["max_soil_moist"] == 75


# ------------------------------------------------------------------ Regression tests for bugs found

class TestRegressionPhotoDownloadCrash:
    """Regression: _download_photos crashed with unhandled exception when
    photo.get_file() raised (e.g. Telegram API timeout, file too large).
    The exception propagated up through handle_photo without a user-friendly
    error message, silently crashing the conversation.
    """

    @pytest.mark.asyncio
    async def test_get_file_error_gracefully_handled(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        funcs = _get_identify_funcs(db)

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 12345
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        update.message.reply_to_message = None

        fake_photo = MagicMock()
        fake_photo.file_unique_id = "ABC"
        fake_photo.get_file = AsyncMock(side_effect=Exception("Telegram timeout"))
        update.message.photo = [fake_photo]

        ctx = make_fake_context()
        result = await funcs["handle_photo"](update, ctx)

        # Should end gracefully, not crash
        assert result == funcs["END"]
        reply = update.message.reply_text.call_args[0][0]
        assert "photo" in reply.lower()

    @pytest.mark.asyncio
    async def test_download_as_bytearray_error_gracefully_handled(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        funcs = _get_identify_funcs(db)

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 12345
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        update.message.reply_to_message = None

        fake_file = MagicMock()
        fake_file.download_as_bytearray = AsyncMock(side_effect=Exception("Network error"))
        fake_photo = MagicMock()
        fake_photo.file_unique_id = "DEF"
        fake_photo.get_file = AsyncMock(return_value=fake_file)
        update.message.photo = [fake_photo]

        ctx = make_fake_context()
        result = await funcs["handle_photo"](update, ctx)

        assert result == funcs["END"]
        reply = update.message.reply_text.call_args[0][0]
        assert "photo" in reply.lower()


class TestRegressionCancelCommandCrash:
    """Regression: button_cancel was registered as a /cancel command fallback
    but assumed update.callback_query was always present. When triggered as
    /cancel (not via inline button), it crashed with:
    AttributeError: 'NoneType' object has no attribute 'answer'
    """

    @pytest.mark.asyncio
    async def test_cancel_command_does_not_crash(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        funcs = _get_identify_funcs(db)

        update = make_fake_update("/cancel")
        update.callback_query = None
        ctx = make_fake_context(user_data={"identify_results": PLANTNET_RESULTS})

        result = await funcs["button_cancel"](update, ctx)

        assert result == funcs["END"]
        reply = update.message.reply_text.call_args[0][0]
        assert "Cancelled" in reply
        assert "identify_results" not in ctx.user_data

    @pytest.mark.asyncio
    async def test_cancel_via_button_still_works(self, db, admin_env, monkeypatch):
        """Ensure the fix didn't break the normal callback-query cancel path."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        funcs = _get_identify_funcs(db)
        update = make_fake_callback_query("identify_cancel")
        ctx = make_fake_context(user_data={"identify_results": PLANTNET_RESULTS})

        result = await funcs["button_cancel"](update, ctx)

        assert result == funcs["END"]
        assert "identify_results" not in ctx.user_data


class TestRegressionDuplicateChoiceSessionExpiry:
    """Regression: handle_duplicate_choice crashed with TypeError when
    context.user_data['identify_sensor'] was None (session expired/corrupted).
    TypeError: 'NoneType' object is not subscriptable (on sensor["id"])
    """

    @pytest.mark.asyncio
    async def test_expired_session_graceful_error(self, db, admin_env, monkeypatch):
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        import app.identify as identify_mod
        monkeypatch.setattr(identify_mod, "fetch_plantbook_thresholds", AsyncMock(return_value=None))

        funcs = _get_identify_funcs(db)
        update = make_fake_callback_query("dup_reuse")
        ctx = make_fake_context(user_data={
            "identify_results": PLANTNET_RESULTS,
            "identify_sensor": None,  # expired/corrupted
            "identify_species": "Monstera deliciosa",
            "identify_display_name": "Swiss cheese plant",
            "existing_plant": {"id": 1, "display_name": "Swiss Cheese"},
        })

        result = await funcs["handle_duplicate_choice"](update, ctx)

        assert result == funcs["END"]
        reply = update.callback_query.message.reply_text.call_args[0][0]
        assert "expired" in reply.lower() or "start over" in reply.lower()

    @pytest.mark.asyncio
    async def test_dup_new_with_expired_session(self, db, admin_env, monkeypatch):
        """Same bug but via the 'Create new' path."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        import app.identify as identify_mod
        monkeypatch.setattr(identify_mod, "fetch_plantbook_thresholds", AsyncMock(return_value=None))

        funcs = _get_identify_funcs(db)
        update = make_fake_callback_query("dup_new")
        ctx = make_fake_context(user_data={
            "identify_results": PLANTNET_RESULTS,
            "identify_sensor": None,  # expired/corrupted
            "identify_species": "Monstera deliciosa",
            "identify_display_name": "Swiss cheese plant",
            "existing_plant": {"id": 1, "display_name": "Swiss Cheese"},
        })

        result = await funcs["handle_duplicate_choice"](update, ctx)

        assert result == funcs["END"]
        reply = update.callback_query.message.reply_text.call_args[0][0]
        assert "expired" in reply.lower() or "start over" in reply.lower()


# ------------------------------------------------------------------ AWAITING_LOCATION state

class TestIdentifyLocationFlow:
    """Tests for the AWAITING_LOCATION state after a successful assign."""

    def _get_identify_funcs_with_location(self, conn):
        """Extract identify funcs including AWAITING_LOCATION handlers."""
        from app.identify import build_identify_handler, AWAITING_CHOICE, AWAITING_SENSOR, AWAITING_DUPLICATE_CHOICE, AWAITING_LOCATION
        from app.access import is_admin
        from telegram.ext import ConversationHandler

        handler = build_identify_handler(conn, is_admin)

        handle_photo = handler.entry_points[0].callback
        button_assign = handler.states[AWAITING_CHOICE][0].callback
        button_cancel = handler.states[AWAITING_CHOICE][1].callback
        receive_sensor = handler.states[AWAITING_SENSOR][0].callback
        handle_duplicate_choice = handler.states[AWAITING_DUPLICATE_CHOICE][0].callback

        # AWAITING_LOCATION: first handler is /skip command, second is text
        handle_skip_location = handler.states[AWAITING_LOCATION][0].callback
        handle_location = handler.states[AWAITING_LOCATION][1].callback

        return {
            "handle_photo": handle_photo,
            "button_assign": button_assign,
            "button_cancel": button_cancel,
            "receive_sensor": receive_sensor,
            "handle_duplicate_choice": handle_duplicate_choice,
            "handle_location": handle_location,
            "handle_skip_location": handle_skip_location,
            "AWAITING_CHOICE": AWAITING_CHOICE,
            "AWAITING_SENSOR": AWAITING_SENSOR,
            "AWAITING_DUPLICATE_CHOICE": AWAITING_DUPLICATE_CHOICE,
            "AWAITING_LOCATION": AWAITING_LOCATION,
            "END": ConversationHandler.END,
        }

    @pytest.mark.asyncio
    async def test_assign_transitions_to_awaiting_location(self, db, admin_env, monkeypatch):
        """After a successful create-and-assign, conversation enters AWAITING_LOCATION."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "")

        db.seed_sensor(1, "balcony-1")

        funcs = self._get_identify_funcs_with_location(db)
        update = make_fake_update("balcony-1")
        ctx = make_fake_context(user_data={"identify_results": PLANTNET_RESULTS})

        result = await funcs["receive_sensor"](update, ctx)

        assert result == funcs["AWAITING_LOCATION"]
        # Should have asked for location
        calls = update.message.reply_text.call_args_list
        last_text = calls[-1][0][0]
        assert "location" in last_text.lower() or "📍" in last_text
        # sensor_id stored for location handler
        assert ctx.user_data.get("identify_location_sensor_id") == 1

    @pytest.mark.asyncio
    async def test_location_text_sets_sensor_location(self, db, admin_env, monkeypatch):
        """User replies with a location → sensor.location is updated."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        db.seed_sensor(1, "balcony-1", location=None)

        funcs = self._get_identify_funcs_with_location(db)
        update = make_fake_update("Living Room")
        ctx = make_fake_context(user_data={"identify_location_sensor_id": 1})

        result = await funcs["handle_location"](update, ctx)

        assert result == funcs["END"]
        sensor = db.get_sensor(1)
        assert sensor["location"] == "Living Room"
        reply = update.message.reply_text.call_args[0][0]
        assert "Living Room" in reply

    @pytest.mark.asyncio
    async def test_skip_ends_without_setting_location(self, db, admin_env, monkeypatch):
        """User types /skip → conversation ends, location unchanged."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")

        db.seed_sensor(1, "balcony-1", location="Old")

        funcs = self._get_identify_funcs_with_location(db)
        update = make_fake_update("/skip")
        ctx = make_fake_context(user_data={"identify_location_sensor_id": 1})

        result = await funcs["handle_skip_location"](update, ctx)

        assert result == funcs["END"]
        sensor = db.get_sensor(1)
        assert sensor["location"] == "Old"  # unchanged

    @pytest.mark.asyncio
    async def test_reuse_also_transitions_to_awaiting_location(self, db, admin_env, monkeypatch):
        """After dup_reuse completion, conversation enters AWAITING_LOCATION."""
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_ID", "")
        monkeypatch.setenv("OPENPLANTBOOK_CLIENT_SECRET", "")

        db.seed_sensor(1, "balcony-1")
        db.seed_plant(1, "Swiss Cheese", species="Monstera deliciosa")

        import app.identify as identify_mod
        monkeypatch.setattr(identify_mod, "fetch_plantbook_thresholds", AsyncMock(return_value=None))

        funcs = self._get_identify_funcs_with_location(db)
        update = make_fake_callback_query("dup_reuse")
        ctx = make_fake_context(user_data={
            "identify_results": PLANTNET_RESULTS,
            "identify_sensor": {"id": 1, "name": "balcony-1"},
            "identify_species": "Monstera deliciosa",
            "identify_display_name": "Swiss cheese plant",
            "existing_plant": {"id": 1, "display_name": "Swiss Cheese"},
            "identify_photo_ids": [],
        })

        result = await funcs["handle_duplicate_choice"](update, ctx)

        assert result == funcs["AWAITING_LOCATION"]
        # Should have asked for location
        calls = update.callback_query.message.reply_text.call_args_list
        last_text = calls[-1][0][0]
        assert "location" in last_text.lower() or "📍" in last_text
