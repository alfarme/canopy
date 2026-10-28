"""
Photo-identify conversation handler — PlantNet identification + assign workflow.

Entry point is any photo message (no /identify or /id command needed): every
photo an admin sends is identified automatically.

States:
  AWAITING_CHOICE → user taps "Assign to sensor" or "Cancel"
  AWAITING_SENSOR → user replies with sensor number
  AWAITING_DUPLICATE_CHOICE → user picks reuse existing plant or create new
  AWAITING_LOCATION → user replies with location/room or /skip
"""
from __future__ import annotations

import logging
import warnings
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from html import escape as _esc

import asyncio

from app.api_clients import (
    DEFAULT_THRESHOLDS,
    fetch_plantbook_thresholds,
    fetch_reference_image,
    has_usable_thresholds,
    identify_plant,
)
from app.formatting import format_identify_results
from app.photo_store import link_identify_photos, save_identify_photo

log = logging.getLogger(__name__)

# Conversation states
AWAITING_CHOICE = 0
AWAITING_SENSOR = 1
AWAITING_DUPLICATE_CHOICE = 2
AWAITING_LOCATION = 3


def build_identify_handler(conn, admin_check):
    """
    Build and return the ConversationHandler for /identify.

    Args:
        conn: async DB connection
        admin_check: callable(user_id) -> bool
    """

    async def _download_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> list[tuple[str, bytes]]:
        """Download photo(s) from the message or replied-to message."""
        photos = []
        msg = update.message

        # Check if this is a reply to a photo
        if msg.reply_to_message and msg.reply_to_message.photo:
            target = msg.reply_to_message
        elif msg.photo:
            target = msg
        else:
            return photos

        # Get the largest photo size
        photo = target.photo[-1]  # Largest resolution
        try:
            file = await photo.get_file()
            data = await file.download_as_bytearray()
        except Exception as exc:
            log.warning("event=photo_download_failed error=%r", exc)
            return photos
        photos.append((f"photo_{photo.file_unique_id}.jpg", bytes(data)))

        return photos

    async def _send_top_matches(update: Update, results: list[dict[str, Any]]) -> None:
        """Send up to 3 reference photos (via Wikipedia, since PlantNet's
        response carries none) as a media group so the user can visually
        compare the top hits before assigning a sensor."""
        top = results[:3]
        if not top:
            return
        image_urls = await asyncio.gather(
            *(fetch_reference_image(r["scientific_name"]) for r in top)
        )
        media = [
            InputMediaPhoto(
                media=url,
                caption=f"#{i} {_esc(r['scientific_name'])} ({r['score']:.0%})",
                parse_mode="HTML",
            )
            for i, (r, url) in enumerate(zip(top, image_urls), start=1)
            if url
        ]
        if not media:
            return
        try:
            await update.message.reply_media_group(media)
        except Exception as exc:
            # Reference photos are a nice-to-have; never let a fetch/send
            # failure break the identify flow.
            log.warning("event=identify_reference_photos_failed error=%r", exc)

    async def _save_photos(conn, photos: list[tuple[str, bytes]], results: list[dict[str, Any]]) -> list[int]:
        """Persist the captured photos + top species guess for a future
        training/dataset feature. Best-effort — never blocks the flow."""
        top = results[0] if results else {}
        species = top.get("scientific_name")
        score = top.get("score")
        ids = []
        for _name, data in photos:
            photo_id = await save_identify_photo(conn, data, species=species, score=score)
            if photo_id is not None:
                ids.append(photo_id)
        return ids

    async def _show_results(update: Update, context: ContextTypes.DEFAULT_TYPE, results: list[dict[str, Any]]) -> int:
        """Send reference photos + the text/button prompt for a set of
        PlantNet results, and store them for the assign step."""
        context.user_data["identify_results"] = results

        await _send_top_matches(update, results)

        text = format_identify_results(results)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Assign to sensor", callback_data="identify_assign"),
                InlineKeyboardButton("Cancel", callback_data="identify_cancel"),
            ]
        ])
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
        return AWAITING_CHOICE

    async def _prompt_location(msg_target, context, sensor_id: int) -> int:
        """Ask the user for the plant's location after a successful assign."""
        context.user_data["identify_location_sensor_id"] = sensor_id
        await msg_target.reply_text(
            "📍 What location/room is this plant in? Reply with a name, or /skip."
        )
        return AWAITING_LOCATION

    async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Entry point: any photo message. Identification is automatic — no
        /identify or /id command, no caption needed.

        Note: multi-image (media-group/album) identification is intentionally
        NOT supported here. Telegram delivers album photos as separate updates,
        so each lands as its own identify attempt; reliable batching would need
        a job-queue debounce (extra dependency).
        """
        user = update.effective_user
        if user is None or not admin_check(user.id):
            await update.message.reply_text("Not authorized")
            return ConversationHandler.END

        photos = await _download_photos(update, context)

        if not photos:
            await update.message.reply_text("📷 No photo found.")
            return ConversationHandler.END

        await update.message.reply_text("🔍 Identifying plant... (Powered by Pl@ntNet)")

        try:
            results = await identify_plant(photos[:5])
        except Exception as exc:
            log.warning("event=plantnet_error error=%r", exc)
            await update.message.reply_text(
                "❌ Plant identification failed. Please try again later."
            )
            return ConversationHandler.END

        if not results:
            await update.message.reply_text(
                "🔍 No matches found. Try with a clearer photo of a leaf or flower."
            )
            return ConversationHandler.END

        context.user_data["identify_photo_ids"] = await _save_photos(conn, photos, results)
        return await _show_results(update, context, results)

    async def button_assign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """User tapped 'Assign to sensor'."""
        query = update.callback_query
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Which sensor? Reply with the sensor number (e.g. 3)")
        return AWAITING_SENSOR

    async def button_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """User tapped 'Cancel' or typed /cancel."""
        query = update.callback_query
        if query:
            await query.answer()
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("Cancelled.")
        else:
            # Called as /cancel command fallback
            await update.message.reply_text("Cancelled.")
        context.user_data.pop("identify_results", None)
        context.user_data.pop("identify_photo_ids", None)
        return ConversationHandler.END

    async def receive_sensor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """User replied with a sensor number to assign."""
        from datetime import datetime, timezone

        sensor_ref = update.message.text.strip()
        results = context.user_data.get("identify_results", [])

        if not results:
            await update.message.reply_text("❌ No identification results. Please start over with /identify.")
            return ConversationHandler.END

        # Use top result
        top = results[0]
        scientific_name = top["scientific_name"]
        common_names = top.get("common_names", [])
        display_name = common_names[0] if common_names else scientific_name

        # Find sensor
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, name FROM sensors WHERE (name = %s OR mac_or_device_id = %s) AND kind = 'bt' LIMIT 1",
                (sensor_ref, sensor_ref),
            )
            sensor = await cur.fetchone()

        if not sensor:
            await update.message.reply_text(
                f"❌ Sensor <code>{_esc(sensor_ref)}</code> not found. Use /sensors to see available sensors.",
                parse_mode="HTML",
            )
            return AWAITING_SENSOR

        # Check for existing plant with this species (Decision 3: ask inline)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, display_name FROM plants WHERE species ILIKE %s",
                (scientific_name,),
            )
            existing = await cur.fetchone()

        if existing:
            # Store data for duplicate choice
            context.user_data["identify_sensor"] = sensor
            context.user_data["identify_species"] = scientific_name
            context.user_data["identify_display_name"] = display_name

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        f"Reuse \"{existing['display_name']}\"",
                        callback_data="dup_reuse",
                    ),
                    InlineKeyboardButton(
                        f"Create new",
                        callback_data="dup_new",
                    ),
                ]
            ])
            await update.message.reply_text(
                f"🌿 A plant with species <i>{_esc(scientific_name)}</i> already exists: "
                f"<b>{_esc(existing['display_name'])}</b>\n\n"
                f"Reuse existing, or create a new one?",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            context.user_data["existing_plant"] = existing
            return AWAITING_DUPLICATE_CHOICE

        # No duplicate — create plant and assign
        photo_ids = context.user_data.get("identify_photo_ids", [])
        plant_id = await _create_and_assign(
            conn, sensor, scientific_name, display_name, update, photo_ids=photo_ids
        )
        context.user_data.pop("identify_results", None)
        context.user_data.pop("identify_photo_ids", None)
        return await _prompt_location(update.message, context, sensor["id"])

    async def handle_duplicate_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle reuse vs create-new choice for duplicate species."""
        from datetime import datetime, timezone

        query = update.callback_query
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)

        sensor = context.user_data.get("identify_sensor")
        scientific_name = context.user_data.get("identify_species", "")
        display_name = context.user_data.get("identify_display_name", "")
        existing = context.user_data.get("existing_plant")

        if not sensor:
            await query.message.reply_text(
                "❌ Session expired. Please start over with /identify."
            )
            context.user_data.pop("identify_results", None)
            context.user_data.pop("identify_photo_ids", None)
            return ConversationHandler.END

        if query.data == "dup_reuse" and existing:
            plant_id = existing["id"]
            plant_name = existing["display_name"]

            # Decision 4: re-seed thresholds from OpenPlantbook
            thresholds = await fetch_plantbook_thresholds(scientific_name)
            if has_usable_thresholds(thresholds):
                async with conn.cursor() as cur:
                    await cur.execute(
                        """INSERT INTO thresholds (plant_id, min_soil_moist, max_soil_moist,
                                min_soil_ec, max_soil_ec, min_light_lux, max_light_lux,
                                min_temp, max_temp, updated_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (plant_id) DO UPDATE SET
                                min_soil_moist=EXCLUDED.min_soil_moist,
                                max_soil_moist=EXCLUDED.max_soil_moist,
                                min_soil_ec=EXCLUDED.min_soil_ec,
                                max_soil_ec=EXCLUDED.max_soil_ec,
                                min_light_lux=EXCLUDED.min_light_lux,
                                max_light_lux=EXCLUDED.max_light_lux,
                                min_temp=EXCLUDED.min_temp,
                                max_temp=EXCLUDED.max_temp,
                                updated_at=EXCLUDED.updated_at""",
                        (plant_id, thresholds["min_soil_moist"], thresholds["max_soil_moist"],
                         thresholds["min_soil_ec"], thresholds["max_soil_ec"],
                         thresholds["min_light_lux"], thresholds["max_light_lux"],
                         thresholds["min_temp"], thresholds["max_temp"],
                         datetime.now(tz=timezone.utc)),
                    )
                    await cur.execute(
                        "UPDATE plants SET threshold_source='opb', updated_at=%s WHERE id=%s",
                        (datetime.now(tz=timezone.utc), plant_id),
                    )
                # Committed together with the assignment below (atomic).
                await query.message.reply_text(
                    f"⚠️ Re-seeding thresholds from OpenPlantbook — previous manual values replaced."
                )

            # Assign sensor to existing plant (commits the re-seed too)
            await _do_assign(conn, sensor["id"], plant_id)
            sensor_label = sensor.get("name") or str(sensor["id"])
            await query.message.reply_text(
                f"✅ Sensor <b>{_esc(sensor_label)}</b> → 🌿 <b>{_esc(plant_name)}</b>",
                parse_mode="HTML",
            )
            await link_identify_photos(
                conn, context.user_data.get("identify_photo_ids", []),
                sensor["id"], sensor.get("name"), plant_name, plant_id=plant_id,
            )
        else:
            # Create new — suffix with the sensor's own number, not a
            # running count. A count ("Cannabis 2", "Cannabis 3") tells you
            # nothing about which physical sensor is which; the sensor
            # number does ("Cannabis 10" is on Sensor 10).
            sensor_number = sensor.get("name") or str(sensor["id"])
            new_name = f"{display_name} (#{sensor_number})"

            # The existing plant may still be bare (named before it had a
            # sibling) — backfill its own sensor's number now that it's no
            # longer unique, so both read consistently.
            if existing:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """SELECT s.id AS sensor_id, s.name AS sensor_name
                           FROM plant_assignments pa JOIN sensors s ON s.id = pa.sensor_id
                           WHERE pa.plant_id = %s AND pa.released_at IS NULL
                           LIMIT 1""",
                        (existing["id"],),
                    )
                    existing_sensor = await cur.fetchone()
                if existing_sensor:
                    existing_number = existing_sensor["sensor_name"] or str(existing_sensor["sensor_id"])
                    if not existing["display_name"].endswith(f" (#{existing_number})"):
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "UPDATE plants SET display_name = %s WHERE id = %s",
                                (f"{existing['display_name']} (#{existing_number})", existing["id"]),
                            )
                            # Committed together with the new plant's create+assign below (atomic).

            photo_ids = context.user_data.get("identify_photo_ids", [])
            plant_id = await _create_and_assign(
                conn, sensor, scientific_name, new_name, None, message=query.message,
                photo_ids=photo_ids,
            )

        # Cleanup
        sensor_id = sensor["id"]
        for key in ("identify_results", "identify_photo_ids", "identify_sensor",
                    "identify_species", "identify_display_name", "existing_plant"):
            context.user_data.pop(key, None)
        return await _prompt_location(query.message, context, sensor_id)

    async def _create_and_assign(conn, sensor, scientific_name, display_name, update, message=None, photo_ids=None):
        """Create plant, seed thresholds, assign sensor. Send confirmation."""
        from datetime import datetime, timezone

        msg_target = message if message else (update.message if update else None)

        # Create plant
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO plants (display_name, species, opb_pid, threshold_source, created_at, updated_at)
                   VALUES (%s, %s, %s, 'default', %s, %s) RETURNING id""",
                (display_name, scientific_name, scientific_name.lower(),
                 datetime.now(tz=timezone.utc), datetime.now(tz=timezone.utc)),
            )
            row = await cur.fetchone()
            plant_id = row["id"]
        # No commit here — create + seed + assign commit together (atomic).

        # Fetch thresholds from OpenPlantbook
        thresholds = await fetch_plantbook_thresholds(scientific_name)
        source = "opb"
        if not has_usable_thresholds(thresholds):
            thresholds = DEFAULT_THRESHOLDS.copy()
            source = "default"

        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO thresholds (plant_id, min_soil_moist, max_soil_moist,
                        min_soil_ec, max_soil_ec, min_light_lux, max_light_lux,
                        min_temp, max_temp, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (plant_id) DO UPDATE SET
                        min_soil_moist=EXCLUDED.min_soil_moist,
                        max_soil_moist=EXCLUDED.max_soil_moist,
                        min_soil_ec=EXCLUDED.min_soil_ec,
                        max_soil_ec=EXCLUDED.max_soil_ec,
                        min_light_lux=EXCLUDED.min_light_lux,
                        max_light_lux=EXCLUDED.max_light_lux,
                        min_temp=EXCLUDED.min_temp,
                        max_temp=EXCLUDED.max_temp,
                        updated_at=EXCLUDED.updated_at""",
                (plant_id, thresholds["min_soil_moist"], thresholds["max_soil_moist"],
                 thresholds["min_soil_ec"], thresholds["max_soil_ec"],
                 thresholds["min_light_lux"], thresholds["max_light_lux"],
                 thresholds["min_temp"], thresholds["max_temp"],
                 datetime.now(tz=timezone.utc)),
            )
            await cur.execute(
                "UPDATE plants SET threshold_source=%s, updated_at=%s WHERE id=%s",
                (source, datetime.now(tz=timezone.utc), plant_id),
            )
        # No commit here — the assignment below commits the whole unit atomically.

        # Assign sensor (commits plant + thresholds + assignment together)
        await _do_assign(conn, sensor["id"], plant_id)

        # Build confirmation
        sensor_name = _esc(sensor.get("name") or str(sensor["id"]))
        lines = [
            f"✅ Done! Sensor <b>{sensor_name}</b> → 🌿 <b>{_esc(display_name)}</b> (<i>{_esc(scientific_name)}</i>)",
            f"Thresholds seeded from {'OpenPlantbook' if source == 'opb' else 'defaults'}:",
            f"  💧 Moisture: {thresholds['min_soil_moist']}–{thresholds['max_soil_moist']}%",
            f"  ☀️ Light: {thresholds['min_light_lux']}–{thresholds['max_light_lux']} lux",
            f"  🌡️ Temp: {thresholds['min_temp']}–{thresholds['max_temp']} °C",
            f"  ⚡ EC: {thresholds['min_soil_ec']}–{thresholds['max_soil_ec']} µS/cm",
        ]
        if source == "default":
            lines.append("\n⚠️ OpenPlantbook lookup failed. Edit with /setthreshold if needed.")

        if msg_target:
            await msg_target.reply_text("\n".join(lines), parse_mode="HTML")

        log.info("event=identify_assign plant_id=%s sensor_id=%s species=%r",
                 plant_id, sensor["id"], scientific_name)

        await link_identify_photos(
            conn, photo_ids or [], sensor["id"], sensor.get("name"), display_name, plant_id=plant_id,
        )

        return plant_id

    async def _do_assign(conn, sensor_id: int, plant_id: int):
        """Release prior assignment and insert new one."""
        from datetime import datetime, timezone
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE plant_assignments SET released_at = %s "
                "WHERE sensor_id = %s AND released_at IS NULL",
                (datetime.now(tz=timezone.utc), sensor_id),
            )
            await cur.execute(
                "INSERT INTO plant_assignments (sensor_id, plant_id, assigned_at) "
                "VALUES (%s, %s, %s)",
                (sensor_id, plant_id, datetime.now(tz=timezone.utc)),
            )
        await conn.commit()

    async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """User replied with a location name for the sensor."""
        location = update.message.text.strip()
        sensor_id = context.user_data.pop("identify_location_sensor_id", None)
        if sensor_id is None:
            await update.message.reply_text("❌ Session expired.")
            return ConversationHandler.END
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE sensors SET location = %s WHERE id = %s",
                (location, sensor_id),
            )
        await conn.commit()
        await update.message.reply_text(f"📍 Location set to <b>{_esc(location)}</b>.", parse_mode="HTML")
        return ConversationHandler.END

    async def handle_skip_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """User typed /skip — skip location setting."""
        context.user_data.pop("identify_location_sensor_id", None)
        await update.message.reply_text("Skipped.")
        return ConversationHandler.END

    async def timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Conversation timeout handler."""
        context.user_data.pop("identify_results", None)
        context.user_data.pop("identify_photo_ids", None)
        context.user_data.pop("identify_location_sensor_id", None)
        return ConversationHandler.END

    # Build the ConversationHandler
    # Suppress the expected PTBUserWarning about per_message=False with
    # CallbackQueryHandler. Our entry_point is a MessageHandler (not
    # CallbackQueryHandler), so per_message=True would BREAK the handler
    # (PTB requires ALL handlers be CallbackQueryHandler when per_message=True).
    # per_message=False is semantically correct for our flow shape.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r".*per_message=False.*CallbackQueryHandler.*",
            category=PTBUserWarning,
        )
        conv_handler = ConversationHandler(
            entry_points=[
                MessageHandler(filters.PHOTO, handle_photo),
            ],
            states={
                AWAITING_CHOICE: [
                    CallbackQueryHandler(button_assign, pattern="^identify_assign$"),
                    CallbackQueryHandler(button_cancel, pattern="^identify_cancel$"),
                ],
                AWAITING_SENSOR: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_sensor),
                ],
                AWAITING_DUPLICATE_CHOICE: [
                    CallbackQueryHandler(handle_duplicate_choice, pattern="^dup_"),
                ],
                AWAITING_LOCATION: [
                    CommandHandler("skip", handle_skip_location),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_location),
                ],
                ConversationHandler.TIMEOUT: [
                    MessageHandler(filters.ALL, timeout),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", button_cancel),
            ],
            conversation_timeout=300,  # 5 minutes
            per_user=True,
            per_chat=True,
        )

    return conv_handler
