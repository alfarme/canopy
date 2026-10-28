"""
Telegram-bot service entrypoint.

Responsibilities:
 - Poll alert_events WHERE delivered=false → send to Telegram → mark delivered.
 - Handle commands: /assign, /unassign, /plants, /sensors, /status, /thresholds,
   /setthreshold, /delete, /rename, /setlocation, /help.
 - Any photo message auto-triggers plant identification (no command needed).

Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATABASE_URL (or PG*),
          POLL_INTERVAL (seconds, default 30), LOG_LEVEL (default INFO),
          PLANTNET_API_KEY, OPENPLANTBOOK_TOKEN, TELEGRAM_ADMIN_USER_ID.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from html import escape as _esc

import httpx
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from app.access import admin_only, is_admin
from app.api_clients import (
    DEFAULT_THRESHOLDS,
    fetch_plantbook_thresholds,
    has_usable_thresholds,
)
from app.commands import (
    parse_addsensor_args,
    parse_assign_args,
    parse_delete_args,
    parse_remove_sensor_args,
    parse_rename_args,
    parse_setlocation_args,
    parse_setthreshold_args,
    parse_silence_args,
    parse_unassign_args,
    VALID_ALERT_KINDS,
)
from app.db import connect_with_backoff
from app.digest import PLANT_STATE_KINDS, run_due_digests
from app.formatting import (
    format_assign_error,
    format_assign_success,
    format_help,
    format_plants_list,
    format_sensors_list,
    format_silences,
    format_status,
    format_thresholds,
)
from app.identify import build_identify_handler
from app.log import setup_logging
from app.photo_store import delete_plant_photos
from app.poller import process_pending_alerts

log = logging.getLogger(__name__)

_SHUTDOWN = asyncio.Event()

# Alert cards: rendered by Canopy (same graded photo as the alert board) and
# uploaded with the alert text as caption. ALERT_CARDS=0 reverts to text-only.
CANOPY_UI_URL = os.environ.get("CANOPY_UI_URL", "http://canopy-ui:8080")
ALERT_CARDS = os.environ.get("ALERT_CARDS", "1") not in ("0", "false", "no")


# ------------------------------------------------------------------ DB queries

async def _fetch_pending_alerts(conn) -> list:
    async with conn.cursor() as cur:
        # "stuck" alerts are intentionally never sent to Telegram — too
        # frequent/low-signal for chat, visible in Grafana instead. The engine
        # no longer writes them at all (it's a Grafana rule now, muted by
        # notification policy), but the filter stays: the table still holds
        # years of undelivered stuck rows, and dropping it would flush every
        # one of them into the chat at once.
        await cur.execute(
            "SELECT id, kind, message, sensor_id, plant_id, time "
            "FROM alert_events WHERE delivered = false AND kind != 'stuck' ORDER BY id"
        )
        return await cur.fetchall()


async def _suppress_digest_kinds(conn) -> int:
    """Retire undelivered plant-state alerts without sending them.

    These conditions are reported by the scheduled digests now, so delivering
    them individually is exactly the noise the digests exist to remove. They
    are marked delivered rather than filtered, because the digests are built
    from LIVE sensor state and never read this table -- so a filter alone would
    leave the rows accumulating forever, and any future change to the filter
    would dump months of backlog into the chat at once (the same trap the
    'stuck' filter documents).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE alert_events SET delivered = true "
            "WHERE delivered = false AND kind = ANY(%s)",
            (list(PLANT_STATE_KINDS),),
        )
        n = cur.rowcount or 0
    await conn.commit()
    if n:
        log.info("event=digest_suppressed count=%d", n)
    return n


async def _claim_digest(conn, kind: str, run_on) -> bool:
    """Take today's slot for `kind`; True only for the caller that wins it."""
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO digest_runs (kind, run_on) VALUES (%s, %s) "
            "ON CONFLICT (kind, run_on) DO NOTHING RETURNING kind",
            (kind, run_on),
        )
        won = await cur.fetchone() is not None
    await conn.commit()
    return won


async def _record_digest_items(conn, kind: str, run_on, items: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE digest_runs SET items = %s WHERE kind = %s AND run_on = %s",
            (items, kind, run_on),
        )
    await conn.commit()


async def _mark_delivered(conn, alert_id: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE alert_events SET delivered = true WHERE id = %s",
            (alert_id,),
        )
    await conn.commit()


async def _fetch_plants(conn) -> list:
    """Return plants with their current (active) sensor assignment."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT p.id, p.display_name, p.species, p.threshold_source,
                   s.name AS sensor_name, s.location
            FROM plants p
            LEFT JOIN plant_assignments pa
                   ON pa.plant_id = p.id AND pa.released_at IS NULL
            LEFT JOIN sensors s ON s.id = pa.sensor_id AND s.kind = 'bt'
            ORDER BY LENGTH(s.name), s.name, p.id
            """
        )
        return await cur.fetchall()


async def _fetch_sensors(conn) -> list:
    """Return all active sensors with their assigned plant."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT s.id, s.name, s.mac_or_device_id, s.location, s.last_seen,
                   p.display_name AS plant_name,
                   -- Newest *non-null* battery, not the newest row's battery:
                   -- battery is sampled once a day by the Pi's active-GATT job
                   -- while readings land every few minutes, so the most recent
                   -- row's battery column is almost always NULL. A correlated
                   -- scalar subquery rather than LATERAL — the test fixture is
                   -- SQLite, which has no LATERAL, and this is portable.
                   (SELECT r.battery FROM bt_readings r
                     WHERE r.sensor_id = s.id AND r.battery IS NOT NULL
                     ORDER BY r.time DESC LIMIT 1) AS battery
            FROM sensors s
            LEFT JOIN plant_assignments pa
                   ON pa.sensor_id = s.id AND pa.released_at IS NULL
            LEFT JOIN plants p ON p.id = pa.plant_id
            WHERE s.active = true AND s.kind = 'bt'
            ORDER BY LENGTH(s.name), s.name, s.id
            """
        )
        return await cur.fetchall()


async def _fetch_status(conn, sensor_id: int | None = None) -> list:
    """Latest reading per sensor + associated plant.

    If sensor_id is given, filter to that one sensor instead of all of them.
    """
    filter_clause = "AND s.id = %s" if sensor_id is not None else ""
    params = (sensor_id,) if sensor_id is not None else ()
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT DISTINCT ON (s.id)
                   s.name AS sensor_name,
                   p.display_name AS plant_name,
                   r.moisture, r.temp_c, r.light_lux,
                   r.conductivity_us_cm, r.battery, r.time AS last_seen
            FROM sensors s
            LEFT JOIN plant_assignments pa
                   ON pa.sensor_id = s.id AND pa.released_at IS NULL
            LEFT JOIN plants p ON p.id = pa.plant_id
            LEFT JOIN bt_readings r ON r.sensor_id = s.id
            WHERE s.active = true AND s.kind = 'bt' {filter_clause}
            ORDER BY s.id, r.time DESC NULLS LAST
            """,
            params,
        )
        return await cur.fetchall()


async def _find_sensor(conn, ref: str) -> dict | None:
    async with conn.cursor() as cur:
        # name/mac is the identifier shown in /plants and /sensors — some sensor
        # names are themselves numeric strings (e.g. MiFlora walk-test naming),
        # which can collide with a *different* sensor's primary-key id. Always
        # prefer the displayed name/mac match; only fall back to raw id lookup
        # (e.g. from a DB query or /sensors' internal id) if nothing matches by name.
        await cur.execute(
            "SELECT id, name, mac_or_device_id, active FROM sensors "
            "WHERE (name = %s OR mac_or_device_id = %s) AND kind = 'bt' LIMIT 1",
            # MACs are stored upper-cased by ingest; the alert text a user
            # copies from is upper-case too, but typing it by hand is not.
            (ref, ref.upper()),
        )
        row = await cur.fetchone()
        if row:
            return row
        if ref.isdigit():
            await cur.execute(
                "SELECT id, name, mac_or_device_id, active FROM sensors "
                "WHERE id = %s AND kind = 'bt' LIMIT 1",
                (int(ref),),
            )
            return await cur.fetchone()
        return None


async def _silence_alert(conn, sensor_id: int, kind: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO alert_silences (sensor_id, kind) VALUES (%s, %s) "
            "ON CONFLICT (sensor_id, kind) DO NOTHING",
            (sensor_id, kind),
        )
    await conn.commit()


async def _unsilence_alert(conn, sensor_id: int, kind: str) -> bool:
    """Returns True if a silence was actually removed."""
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM alert_silences WHERE sensor_id = %s AND kind = %s RETURNING sensor_id",
            (sensor_id, kind),
        )
        removed = await cur.fetchone()
    await conn.commit()
    return removed is not None


async def _sensor_footprint(conn, sensor_id: int) -> dict:
    """What a purge would destroy — shown before asking for confirmation."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT (SELECT count(*) FROM bt_readings WHERE sensor_id = %s) AS readings, "
            "       (SELECT count(*) FROM alert_events WHERE sensor_id = %s) AS alerts, "
            "       (SELECT p.display_name FROM plant_assignments pa "
            "          JOIN plants p ON p.id = pa.plant_id "
            "         WHERE pa.sensor_id = %s AND pa.released_at IS NULL) AS plant",
            (sensor_id, sensor_id, sensor_id),
        )
        return await cur.fetchone()


async def _purge_sensor(conn, sensor_id: int) -> None:
    """
    Delete everything a sensor produced, then leave the row deactivated.

    The tombstone is the point. Deleting the sensors row outright is what
    caused the rogue-BLE loop fixed in c1306a1: ingest treats an unknown MAC
    as new, re-registers it and fires "New sensor discovered" within a minute,
    forever. `active = false` is the one state ingest's ensure_sensor() checks
    and permanently drops readings for, so the device can keep advertising to
    an empty room.

    identify_photos keeps its rows with sensor_id nulled — those photos belong
    to the plant, not the sensor, and the files stay on disk either way.
    """
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM bt_readings WHERE sensor_id = %s", (sensor_id,))
        await cur.execute("DELETE FROM plant_assignments WHERE sensor_id = %s", (sensor_id,))
        await cur.execute("DELETE FROM alert_events WHERE sensor_id = %s", (sensor_id,))
        await cur.execute("DELETE FROM alert_silences WHERE sensor_id = %s", (sensor_id,))
        await cur.execute("UPDATE identify_photos SET sensor_id = NULL WHERE sensor_id = %s", (sensor_id,))
        await cur.execute("UPDATE sensors SET active = false WHERE id = %s", (sensor_id,))
    await conn.commit()


async def _fetch_silences(conn) -> list:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT s.name AS sensor_name, a.kind, a.silenced_at "
            "FROM alert_silences a JOIN sensors s ON s.id = a.sensor_id "
            "ORDER BY a.silenced_at"
        )
        return await cur.fetchall()


async def _find_plant(conn, ref: str) -> dict | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, display_name, species, threshold_source FROM plants "
            "WHERE display_name ILIKE %s LIMIT 1",
            (ref,),
        )
        return await cur.fetchone()


async def _find_plant_by_species(conn, species: str) -> dict | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, display_name, species FROM plants WHERE species ILIKE %s LIMIT 1",
            (species,),
        )
        return await cur.fetchone()


async def _assign_sensor_to_plant(conn, sensor_id: int, plant_id: int, commit: bool = True) -> None:
    """Release any existing assignment for the sensor, then insert new one.

    Pass commit=False to make this part of a larger transaction (the caller
    then commits once, so a mid-sequence crash leaves no partial writes).
    """
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
    if commit:
        await conn.commit()


async def _fetch_thresholds(conn, plant_id: int) -> dict | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT * FROM thresholds WHERE plant_id = %s", (plant_id,)
        )
        return await cur.fetchone()


async def _create_plant(conn, display_name: str, species: str, commit: bool = True) -> int:
    """Create a plants row. Returns the new plant_id."""
    async with conn.cursor() as cur:
        await cur.execute(
            """INSERT INTO plants (display_name, species, opb_pid, threshold_source, created_at, updated_at)
               VALUES (%s, %s, %s, 'default', %s, %s) RETURNING id""",
            (display_name, species, species.lower(),
             datetime.now(tz=timezone.utc), datetime.now(tz=timezone.utc)),
        )
        row = await cur.fetchone()
    if commit:
        await conn.commit()
    return row["id"]


async def _upsert_thresholds(conn, plant_id: int, thresholds: dict, source: str, commit: bool = True) -> None:
    """Insert or update thresholds for a plant."""
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
    if commit:
        await conn.commit()


# ------------------------------------------------------------------ polling loop

async def poll_loop(bot, chat_id: str, conn) -> None:
    interval = float(os.environ.get("POLL_INTERVAL", "30"))
    log.info("event=poll_loop_start interval=%.0fs chat_id=%s", interval, chat_id)

    async def _send(*, text: str) -> None:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

    async def _send_card(*, plant_id: int, caption: str) -> None:
        """Post the alert as Canopy's alert card + caption.

        Canopy is a private service on the compose network, so Telegram can't
        fetch the URL itself — the bytes are pulled here and uploaded. Any
        failure raises and the poller falls back to a plain text alert.
        """
        url = f"{CANOPY_UI_URL.rstrip('/')}/photos/alert-card/{plant_id}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        await bot.send_photo(
            chat_id=chat_id,
            photo=resp.content,
            caption=caption,
            parse_mode="HTML",
        )

    while not _SHUTDOWN.is_set():
        try:
            # Retire plant-state rows before reading the queue, so they can
            # never be picked up for individual delivery.
            await _suppress_digest_kinds(conn)
            alerts = await _fetch_pending_alerts(conn)
            if alerts:
                async def _mark(aid: int, _conn=conn) -> None:
                    await _mark_delivered(_conn, aid)

                await process_pending_alerts(
                    alerts=alerts,
                    send_message=_send,
                    mark_delivered=_mark,
                    chat_id=chat_id,
                    send_photo=_send_card if ALERT_CARDS else None,
                )
        except Exception as exc:
            log.warning("event=poll_error error=%r", exc)

        try:
            await asyncio.wait_for(_SHUTDOWN.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# ------------------------------------------------------------------ digests

# Schedules are expressed in the user's wall-clock time, not UTC — "19:00"
# has to mean 19:00 in Zurich across both DST halves of the year.
DIGEST_TZ = ZoneInfo(os.environ.get("DIGEST_TZ", "Europe/Zurich"))
DIGEST_CHECK_INTERVAL = float(os.environ.get("DIGEST_CHECK_INTERVAL", "300"))


async def digest_loop(bot, chat_id: str, conn) -> None:
    """Post the scheduled digest sheets.

    Ticks every few minutes rather than sleeping until the next 19:00: a long
    sleep is fragile across restarts and clock changes, whereas a coarse tick
    plus an idempotent per-day claim in the database is correct regardless of
    when the process happens to be alive.
    """
    log.info(
        "event=digest_loop_start tz=%s interval=%.0fs", DIGEST_TZ, DIGEST_CHECK_INTERVAL
    )

    async def _claim(kind: str, run_on) -> bool:
        return await _claim_digest(conn, kind, run_on)

    async def _record(kind: str, run_on, items: int) -> None:
        await _record_digest_items(conn, kind, run_on, items)

    async def _fetch(kind: str) -> dict:
        url = f"{CANOPY_UI_URL.rstrip('/')}/api/digest/{kind}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    async def _send_sheet(*, kind: str, caption: str) -> None:
        """Upload Canopy's rendered contact sheet with the caption.

        Canopy is private to the compose network, so Telegram cannot fetch the
        URL itself — the bytes are pulled here and uploaded, same as the alert
        card. Rendering can take a few seconds for a full sheet, hence the
        longer timeout.
        """
        url = f"{CANOPY_UI_URL.rstrip('/')}/photos/digest/{kind}"
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        await bot.send_photo(
            chat_id=chat_id, photo=resp.content, caption=caption, parse_mode="HTML"
        )

    while not _SHUTDOWN.is_set():
        try:
            await run_due_digests(
                now=datetime.now(tz=DIGEST_TZ),
                claim=_claim,
                fetch=_fetch,
                send_sheet=_send_sheet,
                record_items=_record,
            )
        except Exception as exc:
            log.warning("event=digest_loop_error error=%r", exc)

        try:
            await asyncio.wait_for(
                _SHUTDOWN.wait(), timeout=DIGEST_CHECK_INTERVAL
            )
        except asyncio.TimeoutError:
            pass


# ------------------------------------------------------------------ command handlers

def _make_handlers(conn):
    """Return PTB handler list, capturing the DB connection."""

    async def cmd_assign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text or ""
        try:
            sensor_ref, plant_ref = parse_assign_args(text)
        except ValueError:
            await update.message.reply_text(
                format_assign_error("usage"), parse_mode="HTML"
            )
            return

        sensor = await _find_sensor(conn, sensor_ref)
        if not sensor:
            await update.message.reply_text(
                format_assign_error("sensor_not_found", sensor_ref=sensor_ref),
                parse_mode="HTML",
            )
            return

        plant = await _find_plant(conn, plant_ref)
        if not plant:
            # Phase 4: treat as species name, create plant + seed thresholds
            plant_by_species = await _find_plant_by_species(conn, plant_ref)
            if plant_by_species:
                plant = plant_by_species
            else:
                # Create new plant from species
                display_name = plant_ref  # Will use species as display_name
                species = plant_ref

                # Seed thresholds from OpenPlantbook (before writing, so the
                # whole create+seed+assign is one atomic transaction).
                thresholds = await fetch_plantbook_thresholds(species)
                source = "opb"
                if not has_usable_thresholds(thresholds):
                    thresholds = DEFAULT_THRESHOLDS.copy()
                    source = "default"

                plant_id = await _create_plant(conn, display_name, species, commit=False)
                await _upsert_thresholds(conn, plant_id, thresholds, source, commit=False)
                await _assign_sensor_to_plant(conn, sensor["id"], plant_id, commit=False)
                await conn.commit()

                sensor_name = sensor["name"] or sensor_ref
                lines = [
                    f"✅ Created &amp; assigned!\n"
                    f"📡 Sensor <b>{_esc(sensor_name)}</b> → 🌿 <b>{_esc(display_name)}</b>",
                ]
                if source == "opb":
                    lines.append("Thresholds seeded from OpenPlantbook.")
                else:
                    lines.append(
                        "⚠️ OpenPlantbook lookup failed — default thresholds applied.\n"
                        "Use /setthreshold to adjust."
                    )

                await update.message.reply_text(
                    "\n".join(lines), parse_mode="HTML"
                )
                log.info("event=assign_new_species sensor_id=%s species=%r", sensor["id"], species)
                return

        # Decision 4: re-seed thresholds if assigning a known species
        if plant.get("species"):
            thresholds = await fetch_plantbook_thresholds(plant["species"])
            if has_usable_thresholds(thresholds):
                await _upsert_thresholds(conn, plant["id"], thresholds, "opb")
                await update.message.reply_text(
                    "⚠️ Re-seeding thresholds from OpenPlantbook — previous values replaced.",
                    parse_mode="HTML",
                )

        await _assign_sensor_to_plant(conn, sensor["id"], plant["id"])
        await update.message.reply_text(
            format_assign_success(
                sensor_name=sensor["name"] or sensor_ref,
                plant_name=plant["display_name"],
            ),
            parse_mode="HTML",
        )
        log.info(
            "event=assign_done sensor_id=%s plant_id=%s",
            sensor["id"], plant["id"],
        )

    async def cmd_unassign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text or ""
        try:
            sensor_ref = parse_unassign_args(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Usage: /unassign &lt;sensor&gt;", parse_mode="HTML"
            )
            return

        sensor = await _find_sensor(conn, sensor_ref)
        if not sensor:
            await update.message.reply_text(
                f"❌ Sensor <code>{_esc(sensor_ref)}</code> not found.",
                parse_mode="HTML",
            )
            return

        # Release active assignment
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE plant_assignments SET released_at = %s "
                "WHERE sensor_id = %s AND released_at IS NULL RETURNING plant_id",
                (datetime.now(tz=timezone.utc), sensor["id"]),
            )
            released = await cur.fetchone()
        await conn.commit()

        if released:
            await update.message.reply_text(
                f"✅ Sensor <b>{_esc(sensor['name'] or sensor_ref)}</b> unassigned.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"ℹ️ Sensor <b>{_esc(sensor['name'] or sensor_ref)}</b> has no active assignment.",
                parse_mode="HTML",
            )

    async def cmd_delete_plant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text or ""
        try:
            ref = parse_delete_args(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Usage: /delete &lt;sensor_id&gt;\n"
                "Deletes the plant currently assigned to that sensor.\n"
                "Also accepts a plant name (exact, case-insensitive).",
                parse_mode="HTML",
            )
            return

        # Try to resolve via sensor first
        plant_id = None
        plant_name = None
        sensor = await _find_sensor(conn, ref)
        if sensor:
            # Get active assignment's plant_id
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT plant_id FROM plant_assignments WHERE sensor_id = %s AND released_at IS NULL",
                    (sensor["id"],),
                )
                row = await cur.fetchone()
            if row:
                plant_id = row["plant_id"]
                # Get display_name
                async with conn.cursor() as cur:
                    await cur.execute("SELECT display_name FROM plants WHERE id = %s", (plant_id,))
                    p = await cur.fetchone()
                    plant_name = p["display_name"] if p else ref
        if not plant_id:
            # Try matching plant display_name (case-insensitive)
            plant = await _find_plant(conn, ref)
            if plant:
                plant_id = plant["id"]
                plant_name = plant["display_name"]

        if not plant_id:
            await update.message.reply_text(
                f"❌ No plant found for <code>{_esc(ref)}</code>.\n"
                "Use a numeric sensor ID (see /sensors) or an exact plant name.",
                parse_mode="HTML",
            )
            return

        # Delete in one transaction
        photos_removed = await delete_plant_photos(conn, plant_id)
        async with conn.cursor() as cur:
            await cur.execute("UPDATE alert_events SET plant_id = NULL WHERE plant_id = %s", (plant_id,))
            await cur.execute("DELETE FROM plant_assignments WHERE plant_id = %s", (plant_id,))
            await cur.execute("DELETE FROM thresholds WHERE plant_id = %s", (plant_id,))
            await cur.execute("DELETE FROM plants WHERE id = %s", (plant_id,))
        await conn.commit()

        await update.message.reply_text(
            f"🗑 Deleted <b>{_esc(plant_name)}</b>. {photos_removed} photo(s) removed.",
            parse_mode="HTML",
        )
        log.info("event=delete_plant plant_id=%s photos_removed=%d", plant_id, photos_removed)

    async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text or ""
        try:
            sensor_ref, new_name = parse_rename_args(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Usage: /rename &lt;sensor&gt; &lt;new name&gt;", parse_mode="HTML"
            )
            return

        sensor = await _find_sensor(conn, sensor_ref)
        if not sensor:
            await update.message.reply_text(
                f"❌ Sensor <code>{_esc(sensor_ref)}</code> not found.", parse_mode="HTML"
            )
            return

        # Get active plant for this sensor
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT plant_id FROM plant_assignments WHERE sensor_id = %s AND released_at IS NULL",
                (sensor["id"],),
            )
            row = await cur.fetchone()

        if not row:
            await update.message.reply_text(
                f"❌ Sensor <b>{_esc(sensor['name'] or sensor_ref)}</b> has no active plant assignment.",
                parse_mode="HTML",
            )
            return

        plant_id = row["plant_id"]
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE plants SET display_name = %s, updated_at = %s WHERE id = %s",
                (new_name, datetime.now(tz=timezone.utc), plant_id),
            )
        await conn.commit()

        await update.message.reply_text(
            f"✏️ Renamed to <b>{_esc(new_name)}</b>.", parse_mode="HTML"
        )

    async def cmd_set_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text or ""
        try:
            sensor_ref, location = parse_setlocation_args(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Usage: /setlocation &lt;sensor&gt; &lt;location&gt;", parse_mode="HTML"
            )
            return

        sensor = await _find_sensor(conn, sensor_ref)
        if not sensor:
            await update.message.reply_text(
                f"❌ Sensor <code>{_esc(sensor_ref)}</code> not found.", parse_mode="HTML"
            )
            return

        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE sensors SET location = %s WHERE id = %s",
                (location, sensor["id"]),
            )
        await conn.commit()

        await update.message.reply_text(
            f"📍 Location for <b>{_esc(sensor['name'] or sensor_ref)}</b> set to <b>{_esc(location)}</b>.",
            parse_mode="HTML",
        )

    async def cmd_plants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        plants = await _fetch_plants(conn)
        await update.message.reply_text(
            format_plants_list(plants), parse_mode="HTML"
        )

    async def cmd_sensors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sensors = await _fetch_sensors(conn)
        await update.message.reply_text(
            format_sensors_list(sensors), parse_mode="HTML"
        )

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text or ""
        parts = text.strip().split(None)
        if parts and parts[0].startswith("/"):
            parts = parts[1:]

        sensor_id = None
        if parts:
            sensor_ref = " ".join(parts)
            sensor = await _find_sensor(conn, sensor_ref)
            if not sensor:
                await update.message.reply_text(
                    f"❌ Sensor <b>{_esc(sensor_ref)}</b> not found.\nUse /sensors to see available sensors.",
                    parse_mode="HTML",
                )
                return
            sensor_id = sensor["id"]

        readings = await _fetch_status(conn, sensor_id=sensor_id)
        await update.message.reply_text(
            format_status(readings), parse_mode="HTML"
        )

    async def cmd_thresholds(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text or ""
        parts = text.strip().split(None)
        if parts and parts[0].startswith("/"):
            parts = parts[1:]

        if not parts:
            await update.message.reply_text(
                "❌ Usage: /thresholds &lt;plant name&gt;", parse_mode="HTML"
            )
            return

        plant_ref = " ".join(parts)
        plant = await _find_plant(conn, plant_ref)
        if not plant:
            await update.message.reply_text(
                f"❌ Plant <b>{_esc(plant_ref)}</b> not found.\nUse /plants to see registered plants.",
                parse_mode="HTML",
            )
            return

        thresholds = await _fetch_thresholds(conn, plant["id"])
        if not thresholds:
            await update.message.reply_text(
                f"ℹ️ No thresholds set for <b>{_esc(plant['display_name'])}</b>.\n"
                f"Use /setthreshold to configure.",
                parse_mode="HTML",
            )
            return

        # Merge threshold_source from plant row
        thresholds_data = dict(thresholds)
        thresholds_data["threshold_source"] = plant.get("threshold_source", "—")
        await update.message.reply_text(
            format_thresholds(plant["display_name"], thresholds_data),
            parse_mode="HTML",
        )

    async def cmd_setthreshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text or ""
        try:
            plant_name, param, min_val, max_val = parse_setthreshold_args(text)
        except ValueError as e:
            await update.message.reply_text(f"❌ {_esc(str(e))}", parse_mode="HTML")
            return

        plant = await _find_plant(conn, plant_name)
        if not plant:
            await update.message.reply_text(
                f"❌ Plant <b>{_esc(plant_name)}</b> not found.\nUse /plants to see registered plants.",
                parse_mode="HTML",
            )
            return

        # Map param to column names
        param_map = {
            "moisture": ("min_soil_moist", "max_soil_moist"),
            "ec": ("min_soil_ec", "max_soil_ec"),
            "light": ("min_light_lux", "max_light_lux"),
            "temp": ("min_temp", "max_temp"),
        }
        min_col, max_col = param_map[param]

        # Ensure thresholds row exists
        existing = await _fetch_thresholds(conn, plant["id"])
        if not existing:
            # Insert default row first
            await _upsert_thresholds(conn, plant["id"], DEFAULT_THRESHOLDS.copy(), "manual")

        # Update the specific columns
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE thresholds SET {min_col} = %s, {max_col} = %s, updated_at = %s "
                f"WHERE plant_id = %s",
                (int(min_val), int(max_val), datetime.now(tz=timezone.utc), plant["id"]),
            )
            await cur.execute(
                "UPDATE plants SET threshold_source='manual', updated_at=%s WHERE id=%s",
                (datetime.now(tz=timezone.utc), plant["id"]),
            )
        await conn.commit()

        param_display = {
            "moisture": "💧 Moisture",
            "ec": "⚡ EC",
            "light": "☀️ Light",
            "temp": "🌡️ Temp",
        }
        await update.message.reply_text(
            f"✅ Updated <b>{_esc(plant['display_name'])}</b>:\n"
            f"  {param_display[param]}: {int(min_val)}–{int(max_val)}",
            parse_mode="HTML",
        )

    async def cmd_silence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text or ""
        try:
            sensor_ref, kind = parse_silence_args(text)
        except ValueError as e:
            await update.message.reply_text(f"❌ {_esc(str(e))}", parse_mode="HTML")
            return

        sensor = await _find_sensor(conn, sensor_ref)
        if not sensor:
            await update.message.reply_text(
                f"❌ Sensor <b>{_esc(sensor_ref)}</b> not found.\nUse /sensors to see available sensors.",
                parse_mode="HTML",
            )
            return

        label = _esc(sensor["name"] or sensor_ref)
        if kind is None:
            for k in VALID_ALERT_KINDS:
                await _silence_alert(conn, sensor["id"], k)
            await update.message.reply_text(
                f"🔇 Silenced <b>every</b> alert kind for <b>{label}</b> for good.\n"
                f"Use /unsilence &lt;sensor&gt; to re-enable all, "
                f"or /unsilence &lt;sensor&gt; &lt;kind&gt; for one.",
                parse_mode="HTML",
            )
            return

        await _silence_alert(conn, sensor["id"], kind)
        await update.message.reply_text(
            f"🔇 Silenced <b>{kind}</b> alerts for <b>{label}</b> for good.\n"
            f"Use /unsilence to re-enable.",
            parse_mode="HTML",
        )

    async def cmd_unsilence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text or ""
        try:
            sensor_ref, kind = parse_silence_args(text)
        except ValueError as e:
            await update.message.reply_text(f"❌ {_esc(str(e))}", parse_mode="HTML")
            return

        sensor = await _find_sensor(conn, sensor_ref)
        if not sensor:
            await update.message.reply_text(
                f"❌ Sensor <b>{_esc(sensor_ref)}</b> not found.\nUse /sensors to see available sensors.",
                parse_mode="HTML",
            )
            return

        if kind is None:
            n = 0
            for k in VALID_ALERT_KINDS:
                if await _unsilence_alert(conn, sensor["id"], k):
                    n += 1
            await update.message.reply_text(
                f"🔔 Un-silenced <b>{n}</b> alert kind(s) for "
                f"<b>{_esc(sensor['name'] or sensor_ref)}</b>."
                if n else
                f"ℹ️ Nothing was silenced for <b>{_esc(sensor['name'] or sensor_ref)}</b>.",
                parse_mode="HTML",
            )
            return

        removed = await _unsilence_alert(conn, sensor["id"], kind)
        if removed:
            await update.message.reply_text(
                f"🔔 Un-silenced <b>{kind}</b> alerts for <b>{_esc(sensor['name'] or sensor_ref)}</b>.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"ℹ️ <b>{kind}</b> alerts for <b>{_esc(sensor['name'] or sensor_ref)}</b> weren't silenced.",
                parse_mode="HTML",
            )

    async def cmd_remove_sensor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text or ""
        try:
            sensor_ref, confirmed = parse_remove_sensor_args(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Usage: /removesensor &lt;sensor&gt; [confirm]\n"
                "Deletes every reading, alert and assignment for that sensor "
                "and deactivates it for good.",
                parse_mode="HTML",
            )
            return

        sensor = await _find_sensor(conn, sensor_ref)
        if not sensor:
            await update.message.reply_text(
                f"❌ Sensor <b>{_esc(sensor_ref)}</b> not found.\nUse /sensors to see available sensors.",
                parse_mode="HTML",
            )
            return

        label = _esc(sensor["name"] or sensor_ref)
        foot = await _sensor_footprint(conn, sensor["id"])

        if not confirmed:
            plant_line = (
                f"\n🌿 Still assigned to <b>{_esc(foot['plant'])}</b> — the plant stays, "
                f"the assignment goes."
                if foot["plant"] else ""
            )
            await update.message.reply_text(
                f"⚠️ This will permanently delete <b>{foot['readings']}</b> reading(s) and "
                f"<b>{foot['alerts']}</b> alert(s) for <b>{label}</b>, and deactivate it "
                f"so it can never re-register.{plant_line}\n\n"
                f"This cannot be undone. To go ahead:\n"
                f"<code>/removesensor {_esc(sensor_ref)} confirm</code>",
                parse_mode="HTML",
            )
            return

        await _purge_sensor(conn, sensor["id"])
        await update.message.reply_text(
            f"🗑 Removed <b>{label}</b>: {foot['readings']} reading(s) and "
            f"{foot['alerts']} alert(s) deleted, sensor deactivated.\n"
            f"<i>It will stay silent even if the device keeps advertising.</i>",
            parse_mode="HTML",
        )
        log.info(
            "event=remove_sensor sensor_id=%s readings=%s alerts=%s",
            sensor["id"], foot["readings"], foot["alerts"],
        )

    async def cmd_addsensor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Give an auto-registered sensor its number.

        Ingest already created the row (ensure_sensor) the first time the
        device advertised; what it cannot know is which pot it is sitting in.
        Until a name is set, sensor_labels falls through to the raw MAC — and
        because Postgres propagates NULL through ||, it does so even when the
        sensor is assigned to a plant. So this is what makes a new sensor
        legible in Grafana, Canopy and every alert.
        """
        text = update.message.text or ""
        try:
            sensor_ref, number = parse_addsensor_args(text)
        except ValueError as exc:
            await update.message.reply_text(
                f"❌ {_esc(str(exc))}", parse_mode="HTML"
            )
            return

        sensor = await _find_sensor(conn, sensor_ref)
        if not sensor:
            await update.message.reply_text(
                f"❌ No sensor with MAC or name <code>{_esc(sensor_ref)}</code>.\n"
                f"A sensor only exists once it has advertised at least once — "
                f"wait for the 🌱 new-sensor alert, then copy the MAC from it.",
                parse_mode="HTML",
            )
            return

        if not sensor["active"]:
            await update.message.reply_text(
                f"❌ <code>{_esc(sensor_ref)}</code> was retired with /removesensor "
                f"and is ignored on purpose — naming it would not bring it back.",
                parse_mode="HTML",
            )
            return

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM sensors WHERE name = %s AND kind = 'bt' AND id <> %s LIMIT 1",
                (number, sensor["id"]),
            )
            clash = await cur.fetchone()
        if clash:
            await update.message.reply_text(
                f"❌ Number <b>{_esc(number)}</b> is already taken by another sensor.\n"
                f"Use /sensors to see which numbers are in use.",
                parse_mode="HTML",
            )
            return

        previous = sensor["name"]
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE sensors SET name = %s WHERE id = %s",
                (number, sensor["id"]),
            )
        await conn.commit()

        if previous:
            body = (
                f"📡 Sensor <b>{_esc(previous)}</b> is now <b>{_esc(number)}</b>.\n"
                f"<code>{_esc(sensor['mac_or_device_id'])}</code>"
            )
        else:
            body = (
                f"📡 Added sensor <b>{_esc(number)}</b>.\n"
                f"<code>{_esc(sensor['mac_or_device_id'])}</code>\n\n"
                f"📷 Send a photo of the plant to identify and attach it, "
                f"or use /assign {_esc(number)} &lt;plant&gt;."
            )

        await update.message.reply_text(body, parse_mode="HTML")
        log.info(
            "event=add_sensor sensor_id=%s number=%s previous=%r",
            sensor["id"], number, previous,
        )

    async def cmd_silences(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        silences = await _fetch_silences(conn)
        await update.message.reply_text(
            format_silences(silences), parse_mode="HTML"
        )

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(format_help(), parse_mode="HTML")

    MENU_TEXT = "🌱 <b>Canopy Menu</b>\nTap an action, or keep typing commands directly."
    MANAGE_TEXT = "⚙️ <b>Manage</b>\nAdmin actions — tap for usage, then type the full command."

    def _menu_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🌿 Plants", callback_data="menu_plants"),
                InlineKeyboardButton("📡 Sensors", callback_data="menu_sensors"),
            ],
            [
                InlineKeyboardButton("📊 Status", callback_data="menu_status"),
                InlineKeyboardButton("🔍 Identify", callback_data="menu_identify"),
            ],
            [
                InlineKeyboardButton("⚙️ Manage", callback_data="menu_manage"),
                InlineKeyboardButton("❓ Help", callback_data="menu_help"),
            ],
        ])

    def _manage_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("➕ Assign", callback_data="menu_mgr_assign"),
                InlineKeyboardButton("➖ Unassign", callback_data="menu_mgr_unassign"),
            ],
            [
                InlineKeyboardButton("🌡️ Thresholds", callback_data="menu_mgr_thresholds"),
                InlineKeyboardButton("🔕 Silence", callback_data="menu_mgr_silence"),
            ],
            [
                InlineKeyboardButton("🔔 Unsilence", callback_data="menu_mgr_unsilence"),
                InlineKeyboardButton("📋 Silences", callback_data="menu_mgr_silences"),
            ],
            [
                InlineKeyboardButton("🗑 Remove plant", callback_data="menu_mgr_delete"),
                InlineKeyboardButton("✏️ Rename", callback_data="menu_mgr_rename"),
            ],
            [
                InlineKeyboardButton("📍 Set location", callback_data="menu_mgr_setlocation"),
                InlineKeyboardButton("📡 Add sensor", callback_data="menu_mgr_addsensor"),
            ],
            [
                InlineKeyboardButton("🗑 Remove sensor", callback_data="menu_mgr_removesensor"),
                InlineKeyboardButton("⬅️ Back", callback_data="menu_back"),
            ],
        ])

    async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            MENU_TEXT,
            parse_mode="HTML",
            reply_markup=_menu_keyboard(),
        )

    async def button_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle taps on the /menu inline keyboard.

        Two levels: the main menu (read-only, argument-free actions —
        Plants, Sensors, Status, Identify, Help) and a "Manage" submenu
        holding every admin/write action (assign, unassign, thresholds,
        silence family) so the main menu doesn't keep growing. Submenu
        navigation edits the existing message's keyboard in place; actions
        that need typed arguments show their usage as a new message instead
        of opening a full conversation — kept intentionally simple rather
        than building a second state machine parallel to /identify's.
        """
        query = update.callback_query
        await query.answer()
        action = query.data

        if action == "menu_manage":
            await query.edit_message_text(MANAGE_TEXT, parse_mode="HTML", reply_markup=_manage_keyboard())
        elif action == "menu_back":
            await query.edit_message_text(MENU_TEXT, parse_mode="HTML", reply_markup=_menu_keyboard())
        elif action == "menu_plants":
            plants = await _fetch_plants(conn)
            await query.message.reply_text(format_plants_list(plants), parse_mode="HTML")
        elif action == "menu_sensors":
            sensors = await _fetch_sensors(conn)
            await query.message.reply_text(format_sensors_list(sensors), parse_mode="HTML")
        elif action == "menu_status":
            readings = await _fetch_status(conn)
            await query.message.reply_text(format_status(readings), parse_mode="HTML")
        elif action == "menu_identify":
            await query.message.reply_text(
                "📷 Just send a photo — it's identified automatically, no command needed."
            )
        elif action == "menu_help":
            await query.message.reply_text(format_help(), parse_mode="HTML")
        elif action == "menu_mgr_assign":
            await query.message.reply_text(
                "❌ Usage: /assign &lt;sensor&gt; &lt;plant name&gt;", parse_mode="HTML"
            )
        elif action == "menu_mgr_unassign":
            await query.message.reply_text(
                "❌ Usage: /unassign &lt;sensor&gt;", parse_mode="HTML"
            )
        elif action == "menu_mgr_thresholds":
            await query.message.reply_text(
                "❌ Usage: /thresholds &lt;plant name&gt;", parse_mode="HTML"
            )
        elif action == "menu_mgr_silence":
            await query.message.reply_text(
                "❌ Usage: /silence &lt;sensor&gt; [kind] — no kind silences every kind",
                parse_mode="HTML",
            )
        elif action == "menu_mgr_unsilence":
            await query.message.reply_text(
                "❌ Usage: /unsilence &lt;sensor&gt; [kind] — no kind lifts every kind",
                parse_mode="HTML",
            )
        elif action == "menu_mgr_addsensor":
            await query.message.reply_text(
                "❌ Usage: /addsensor MAC NUMBER\n"
                "Numbers a sensor that has already been discovered — take the MAC "
                "from its 🌱 new-sensor alert.",
                parse_mode="HTML",
            )
        elif action == "menu_mgr_removesensor":
            await query.message.reply_text(
                "❌ Usage: /removesensor &lt;sensor&gt;\n"
                "Deletes every reading and alert for that sensor and retires it. "
                "Asks you to confirm first.",
                parse_mode="HTML",
            )
        elif action == "menu_mgr_silences":
            silences = await _fetch_silences(conn)
            await query.message.reply_text(format_silences(silences), parse_mode="HTML")
        elif action == "menu_mgr_delete":
            await query.message.reply_text(
                "❌ Usage: /delete &lt;sensor_id&gt;\n"
                "Deletes the plant assigned to that sensor.\n"
                "Also accepts a plant name (exact, case-insensitive).",
                parse_mode="HTML",
            )
        elif action == "menu_mgr_rename":
            await query.message.reply_text(
                "❌ Usage: /rename &lt;sensor&gt; &lt;new name&gt;", parse_mode="HTML"
            )
        elif action == "menu_mgr_setlocation":
            await query.message.reply_text(
                "❌ Usage: /setlocation &lt;sensor&gt; &lt;location&gt;", parse_mode="HTML"
            )

    # Apply admin_only to write commands
    cmd_assign_wrapped = admin_only(cmd_assign)
    cmd_unassign_wrapped = admin_only(cmd_unassign)
    cmd_setthreshold_wrapped = admin_only(cmd_setthreshold)
    cmd_silence_wrapped = admin_only(cmd_silence)
    cmd_unsilence_wrapped = admin_only(cmd_unsilence)
    cmd_delete_plant_wrapped = admin_only(cmd_delete_plant)
    cmd_remove_sensor_wrapped = admin_only(cmd_remove_sensor)
    cmd_addsensor_wrapped = admin_only(cmd_addsensor)
    cmd_rename_wrapped = admin_only(cmd_rename)
    cmd_set_location_wrapped = admin_only(cmd_set_location)

    handlers = [
        CommandHandler("assign", cmd_assign_wrapped),
        CommandHandler("unassign", cmd_unassign_wrapped),
        CommandHandler("plants", cmd_plants),
        CommandHandler("sensors", cmd_sensors),
        CommandHandler("status", cmd_status),
        CommandHandler("thresholds", cmd_thresholds),
        CommandHandler("setthreshold", cmd_setthreshold_wrapped),
        CommandHandler("silence", cmd_silence_wrapped),
        CommandHandler("unsilence", cmd_unsilence_wrapped),
        CommandHandler("silences", cmd_silences),
        CommandHandler("delete", cmd_delete_plant_wrapped),
        CommandHandler("addsensor", cmd_addsensor_wrapped),
        CommandHandler("removesensor", cmd_remove_sensor_wrapped),
        CommandHandler("rename", cmd_rename_wrapped),
        CommandHandler("setlocation", cmd_set_location_wrapped),
        CommandHandler("help", cmd_help),
        CommandHandler(["menu", "start"], cmd_menu),
        CallbackQueryHandler(button_menu, pattern="^menu_"),
    ]

    return handlers


# ------------------------------------------------------------------ main

async def main() -> None:
    setup_logging()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error("event=missing_config TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required")
        sys.exit(1)

    conn = await connect_with_backoff()

    app = (
        Application.builder()
        .token(token)
        .build()
    )

    # Add the identify ConversationHandler first (has higher priority)
    identify_handler = build_identify_handler(conn, is_admin)
    app.add_handler(identify_handler)

    for handler in _make_handlers(conn):
        app.add_handler(handler)

    loop = asyncio.get_running_loop()

    def _handle_sigterm(*_):
        log.info("event=sigterm_received")
        _SHUTDOWN.set()
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        # Register commands with Telegram (Deliverable 6)
        await app.bot.set_my_commands([
            BotCommand("assign", "Assign a sensor to a plant"),
            BotCommand("unassign", "Release a sensor's plant assignment"),
            BotCommand("plants", "List all plants and assignments"),
            BotCommand("sensors", "List all active sensors"),
            BotCommand("status", "Latest readings (all or specific)"),
            BotCommand("thresholds", "Show thresholds for a plant"),
            BotCommand("setthreshold", "Override a threshold parameter"),
            BotCommand("silence", "Silence a sensor's alerts (all kinds, or one)"),
            BotCommand("unsilence", "Re-enable a previously silenced alert"),
            BotCommand("silences", "List currently silenced alerts"),
            BotCommand("delete", "Remove a plant and its photos"),
            BotCommand("addsensor", "Give a newly discovered sensor its number"),
            BotCommand("removesensor", "Delete a sensor's data and retire it"),
            BotCommand("rename", "Rename a plant"),
            BotCommand("setlocation", "Set a sensor's location"),
            BotCommand("help", "Show available commands"),
            BotCommand("menu", "Show a button menu instead of typing commands"),
        ])
        log.info("event=bot_started commands_registered=true")

        # Run the alert poller and the digest scheduler alongside PTB's
        # polling. Both exit on _SHUTDOWN, so awaiting them together ends the
        # service cleanly; gather propagates whichever raises first.
        await asyncio.gather(
            poll_loop(app.bot, chat_id, conn),
            digest_loop(app.bot, chat_id, conn),
        )

        await app.updater.stop()
        await app.stop()

    await conn.close()
    log.info("event=shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
