"""
Scheduled digests — the replacement for per-condition plant alerts in chat.

Plant-state conditions no longer page the user the moment they trip. They are
collected into a small number of scheduled sheets instead:

    water    daily,    19:00  — top 5 thirstiest, longest-waiting first
    sensors  Sundays,  19:00  — sensors not reporting, or reporting nonsense
    feed     the 1st,  19:00  — fertiliser and salt, the slow soil chemistry

Sensor-health alerting is deliberately NOT part of this: `fp_stale` and
`fp_low_battery` stay live in Grafana, because a digest cannot tell you a
sensor died — a dead sensor simply stops appearing in the data the digest is
built from.

Boundaries (claim, fetch, send) are injected so the schedule logic and the
orchestration can be tested without a database, a clock, or Telegram.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Awaitable, Callable, Iterable

log = logging.getLogger(__name__)

# Alert kinds that describe the PLANT's state. These are what the digests
# cover, so the poller must stop delivering them one by one. Kinds absent from
# this set (stale, low_battery, new_sensor) still arrive immediately: they are
# about the hardware, they are rare, and they are actionable the moment they
# fire.
PLANT_STATE_KINDS = frozenset({
    "water_needed",
    "out_of_range",
    "frost_risk",
    "heat_stress",
    "feed_needed",
    "salt_buildup",
    "soil_not_draining",
    "light_low",
    "light_high",
})

# The hour (local) every digest fires at. One slot for all three so a Sunday
# the 1st posts three sheets together rather than trickling them through the
# evening.
DIGEST_HOUR = 19

KINDS = ("water", "sensors", "feed")

CAPTIONS = {
    "water": "<b>Water needed</b>",
    "sensors": "<b>Sensors down</b>",
    "feed": "<b>Fertiliser &amp; salt</b>",
}


def is_due(kind: str, now: datetime) -> bool:
    """True when `kind` should have fired by `now` (local time) today.

    Deliberately ">= DIGEST_HOUR" rather than "== DIGEST_HOUR": the scheduler
    ticks on a coarse interval and the service restarts, so an equality test
    would silently skip a day whenever 19:00 fell inside a redeploy. Being late
    is recoverable, being skipped is not — and the once-per-day claim in the
    database is what stops the open-ended window from posting twice.
    """
    if now.hour < DIGEST_HOUR:
        return False
    if kind == "water":
        return True
    if kind == "sensors":
        return now.weekday() == 6  # Sunday
    if kind == "feed":
        return now.day == 1
    return False


def due_kinds(now: datetime) -> list[str]:
    """Every digest kind due at `now`, in a stable order."""
    return [k for k in KINDS if is_due(k, now)]


async def run_due_digests(
    *,
    now: datetime,
    claim: Callable[[str, date], Awaitable[bool]],
    fetch: Callable[[str], Awaitable[dict[str, Any]]],
    send_sheet: Callable[..., Awaitable[None]],
    record_items: Callable[[str, date, int], Awaitable[None]],
    kinds: Iterable[str] | None = None,
) -> list[str]:
    """Fire whichever digests are due. Returns the kinds actually posted.

    Order of operations per kind:

      1. CLAIM first, before doing any work. The claim is a unique insert on
         (kind, local date); losing it means another instance -- or this one
         before a restart -- already handled today, so we stop. Claiming
         up-front is what makes a redeploy at 19:00 safe.
      2. Fetch the item list. An empty digest posts NOTHING, but the claim is
         kept: "checked, nothing to report" is a completed run, and releasing
         it would re-check every tick for the rest of the evening.
      3. Send the sheet, then record the count.

    A failure after the claim means the digest is skipped for the day rather
    than retried. That is the intended trade: a missed sheet is a minor loss,
    while a retry loop on a failing render would post the same sheet repeatedly
    once it recovered.
    """
    posted: list[str] = []
    for kind in due_kinds(now) if kinds is None else kinds:
        run_on = now.date()
        try:
            if not await claim(kind, run_on):
                continue
        except Exception as exc:
            log.warning("event=digest_claim_failed kind=%s error=%r", kind, exc)
            continue

        try:
            digest = await fetch(kind)
            items = digest.get("items") or []
            if not items:
                log.info("event=digest_empty kind=%s", kind)
                await record_items(kind, run_on, 0)
                continue

            await send_sheet(kind=kind, caption=caption_for(kind, items))
            await record_items(kind, run_on, len(items))
            posted.append(kind)
            log.info("event=digest_sent kind=%s items=%d", kind, len(items))
        except Exception as exc:
            log.warning("event=digest_failed kind=%s error=%r", kind, exc)

    return posted


def caption_for(kind: str, items: list[dict[str, Any]]) -> str:
    """Short caption under the sheet.

    The sheet already names every plant, so the caption exists for the phone
    notification preview, which shows text and not the image. It leads with the
    count and names the first few, using the user's sensor numbers.
    """
    head = CAPTIONS.get(kind, "<b>Digest</b>")
    names = []
    for it in items[:3]:
        label = it.get("plantName") or "unassigned"
        names.append(f"{label} ({it.get('sensorName')})")
    tail = ", ".join(names)
    if len(items) > 3:
        tail += f", +{len(items) - 3} more"
    return f"{head}\n{len(items)} · {tail}"
