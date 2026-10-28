"""
Tests for the scheduled digest logic (offline: clock, DB and Telegram faked).
"""
import pytest
from datetime import datetime, date
from zoneinfo import ZoneInfo

from app.digest import (
    DIGEST_HOUR,
    PLANT_STATE_KINDS,
    caption_for,
    due_kinds,
    is_due,
    run_due_digests,
)

TZ = ZoneInfo("Europe/Zurich")


def at(y, m, d, hour, minute=0):
    return datetime(y, m, d, hour, minute, tzinfo=TZ)


# ─── schedule ───────────────────────────────────────────────────────────────

class TestSchedule:
    def test_water_fires_daily_at_the_digest_hour(self):
        # 2026-08-19 is a Wednesday, not the 1st — only water is due.
        assert due_kinds(at(2026, 8, 19, DIGEST_HOUR)) == ["water"]

    def test_nothing_fires_before_the_digest_hour(self):
        assert due_kinds(at(2026, 8, 19, DIGEST_HOUR - 1, 59)) == []

    def test_sensors_fires_only_on_sunday(self):
        # 2026-08-16 is a Sunday; the 17th is a Monday.
        assert "sensors" in due_kinds(at(2026, 8, 16, DIGEST_HOUR))
        assert "sensors" not in due_kinds(at(2026, 8, 17, DIGEST_HOUR))

    def test_feed_fires_only_on_the_first(self):
        assert "feed" in due_kinds(at(2026, 9, 1, DIGEST_HOUR))
        assert "feed" not in due_kinds(at(2026, 9, 2, DIGEST_HOUR))

    def test_a_sunday_the_first_fires_all_three(self):
        # 2026-11-01 is a Sunday.
        assert due_kinds(at(2026, 11, 1, DIGEST_HOUR)) == ["water", "sensors", "feed"]

    def test_still_due_later_in_the_evening(self):
        """A restart across 19:00 must not skip the day — being late is fine,
        the per-day claim is what prevents a double post."""
        assert is_due("water", at(2026, 8, 19, 23, 59))

    def test_not_due_after_midnight(self):
        assert not is_due("water", at(2026, 8, 20, 0, 30))


# ─── orchestration ──────────────────────────────────────────────────────────

class Harness:
    """Records what the orchestrator did, standing in for DB + Telegram."""

    def __init__(self, items_by_kind, claim_result=True):
        self.items_by_kind = items_by_kind
        self.claim_result = claim_result
        self.claimed = []
        self.sent = []
        self.recorded = []

    async def claim(self, kind, run_on):
        self.claimed.append((kind, run_on))
        return self.claim_result

    async def fetch(self, kind):
        return {"kind": kind, "items": self.items_by_kind.get(kind, [])}

    async def send_sheet(self, *, kind, caption):
        self.sent.append((kind, caption))

    async def record_items(self, kind, run_on, items):
        self.recorded.append((kind, run_on, items))

    async def run(self, now, **kw):
        return await run_due_digests(
            now=now,
            claim=self.claim,
            fetch=self.fetch,
            send_sheet=self.send_sheet,
            record_items=self.record_items,
            **kw,
        )


ITEM = {"sensorName": "30", "plantName": "Cannabis (#30)"}


@pytest.mark.asyncio
class TestRunDueDigests:
    async def test_posts_a_due_digest_with_items(self):
        h = Harness({"water": [ITEM]})
        posted = await h.run(at(2026, 8, 19, DIGEST_HOUR))
        assert posted == ["water"]
        assert len(h.sent) == 1
        assert h.recorded == [("water", date(2026, 8, 19), 1)]

    async def test_empty_digest_posts_nothing_but_keeps_the_claim(self):
        """'Checked, nothing to report' is a completed run — otherwise the
        scheduler re-checks every tick for the rest of the evening."""
        h = Harness({"water": []})
        posted = await h.run(at(2026, 8, 19, DIGEST_HOUR))
        assert posted == []
        assert h.sent == []
        assert h.recorded == [("water", date(2026, 8, 19), 0)]

    async def test_losing_the_claim_skips_all_work(self):
        h = Harness({"water": [ITEM]}, claim_result=False)
        posted = await h.run(at(2026, 8, 19, DIGEST_HOUR))
        assert posted == []
        assert h.sent == []
        assert h.recorded == []

    async def test_claim_is_taken_before_any_work(self):
        """Claiming up front is what makes a redeploy at 19:00 safe."""
        h = Harness({"water": [ITEM]})
        await h.run(at(2026, 8, 19, DIGEST_HOUR))
        assert h.claimed == [("water", date(2026, 8, 19))]

    async def test_nothing_runs_before_the_hour(self):
        h = Harness({"water": [ITEM]})
        assert await h.run(at(2026, 8, 19, 9)) == []
        assert h.claimed == []

    async def test_a_failing_send_does_not_stop_the_other_digests(self):
        h = Harness({"water": [ITEM], "sensors": [ITEM]})

        async def boom(*, kind, caption):
            if kind == "water":
                raise RuntimeError("telegram down")
            h.sent.append((kind, caption))

        h.send_sheet = boom
        # Sunday: water + sensors both due.
        posted = await h.run(at(2026, 8, 16, DIGEST_HOUR))
        assert posted == ["sensors"]
        assert [k for k, _ in h.sent] == ["sensors"]

    async def test_a_failed_digest_is_not_retried_that_day(self):
        """The claim is deliberately not released on failure — a retry loop on
        a broken render would post repeatedly once it recovered."""
        h = Harness({"water": [ITEM]})

        async def boom(*, kind, caption):
            raise RuntimeError("render failed")

        h.send_sheet = boom
        await h.run(at(2026, 8, 19, DIGEST_HOUR))
        assert h.claimed == [("water", date(2026, 8, 19))]
        assert h.recorded == []


# ─── captions ───────────────────────────────────────────────────────────────

class TestCaption:
    def test_uses_the_users_sensor_number(self):
        cap = caption_for("water", [ITEM])
        assert "(30)" in cap
        assert "Cannabis (#30)" in cap

    def test_summarises_a_long_list(self):
        items = [{"sensorName": str(i), "plantName": f"P{i}"} for i in range(7)]
        cap = caption_for("water", items)
        assert "+4 more" in cap
        assert cap.startswith("<b>Water needed</b>")

    def test_unassigned_sensor_has_a_label(self):
        cap = caption_for("sensors", [{"sensorName": "11", "plantName": None}])
        assert "unassigned (11)" in cap


class TestSuppressedKinds:
    def test_covers_every_plant_state_condition(self):
        assert "water_needed" in PLANT_STATE_KINDS
        assert "feed_needed" in PLANT_STATE_KINDS
        assert "salt_buildup" in PLANT_STATE_KINDS

    def test_leaves_hardware_alerts_alone(self):
        """These stay immediate: a digest cannot tell you a sensor died,
        because a dead sensor stops appearing in the data behind it."""
        for kind in ("stale", "low_battery", "new_sensor"):
            assert kind not in PLANT_STATE_KINDS
