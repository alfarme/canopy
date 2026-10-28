"""
Alert poller logic — polls alert_events WHERE delivered=false,
sends each to Telegram, then marks delivered=true.

Boundaries (send_message, mark_delivered) are injected for testability.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from app.formatting import format_alert

log = logging.getLogger(__name__)

# Alert kinds whose picture is true. The card renders the plant's *current
# metric state*, so it only agrees with the alert text for these two. A
# `stale` card is worse than useless — it shows the last known readings, with
# no timestamp, under a line saying there have been no readings; `low_battery`
# and `stuck` are about the sensor, not the plant, and would show four
# in-range dots and no severity frame. Those kinds stay text-only here and are
# handled by Grafana Alerting instead (see docs/technical.md).
# Alert cards render a plant's metric state, so they only make sense for
# plant-state kinds. The range conditions split out of out_of_range are all
# plant state, so they get cards too; sensor-health kinds (stale, stuck,
# low_battery, new_sensor) stay plain text.
CARD_KINDS = frozenset({
    "water_needed", "out_of_range",
    "frost_risk", "heat_stress", "feed_needed", "salt_buildup",
    "soil_not_draining", "light_low", "light_high",
})


async def process_pending_alerts(
    *,
    alerts: list[Any],
    send_message: Callable[..., Awaitable[None]],
    mark_delivered: Callable[[int], Awaitable[None]],
    chat_id: str,
    send_photo: Callable[..., Awaitable[None]] | None = None,
) -> None:
    """
    For each undelivered alert row:
      1. Format the message.
      2. Send it — as a photo card (send_photo) when the alert is about the
         plant's metric state (CARD_KINDS), belongs to a plant, and a photo
         sender is wired up; else as plain text.
      3. Mark as delivered via mark_delivered(alert_id).

    A failing send_photo falls back to send_message: the card is decoration,
    and a missing photo/unreachable Canopy must never swallow an alert. If
    send_message raises, the exception propagates and mark_delivered is NOT
    called (safe retry on next poll cycle).
    """
    for row in alerts:
        alert_id = row["id"]
        kind = row["kind"]
        message = row["message"]
        sensor_id = row["sensor_id"]
        plant_id = row["plant_id"]

        text = format_alert(
            kind=kind,
            message=message,
            sensor_name=None,   # sensor lookup happens in the real runner
            plant_name=None,    # plant lookup happens in the real runner
            alert_id=alert_id,
        )

        log.info(
            "event=send_alert alert_id=%s kind=%s chat_id=%s",
            alert_id, kind, chat_id,
        )
        sent = False
        if send_photo is not None and plant_id is not None and kind in CARD_KINDS:
            try:
                await send_photo(plant_id=plant_id, caption=text)
                sent = True
            except Exception as exc:  # noqa: BLE001 — card is best-effort
                log.warning(
                    "event=alert_card_failed alert_id=%s plant_id=%s error=%r",
                    alert_id, plant_id, exc,
                )

        # Raises on failure — intentionally NOT caught here so the alert
        # stays undelivered and the next poll attempt retries.
        if not sent:
            await send_message(text=text)
        await mark_delivered(alert_id)
        log.info("event=alert_delivered alert_id=%s", alert_id)
