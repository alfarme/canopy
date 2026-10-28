"""
Tests for the alert polling logic (offline: fake DB rows injected).
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, call
from datetime import datetime, timezone

from app.poller import process_pending_alerts


class FakeAlertRow:
    """Mimic a psycopg Row with dict-style access."""
    def __init__(self, id, kind, message, sensor_id, plant_id, time):
        self._data = {
            "id": id,
            "kind": kind,
            "message": message,
            "sensor_id": sensor_id,
            "plant_id": plant_id,
            "time": time,
        }

    def __getitem__(self, key):
        return self._data[key]

    def keys(self):
        return self._data.keys()


# ------------------------------------------------------------------ helpers
def make_row(id=1, kind="water_needed", message="Needs water",
             sensor_id=1, plant_id=1,
             time=None):
    if time is None:
        time = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)
    return FakeAlertRow(id=id, kind=kind, message=message,
                        sensor_id=sensor_id, plant_id=plant_id, time=time)


# ------------------------------------------------------------------ tests
class TestProcessPendingAlerts:

    @pytest.mark.asyncio
    async def test_no_alerts_sends_nothing(self):
        fake_send = AsyncMock()
        fake_mark = AsyncMock()

        await process_pending_alerts(
            alerts=[],
            send_message=fake_send,
            mark_delivered=fake_mark,
            chat_id="123",
        )

        fake_send.assert_not_called()
        fake_mark.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_alert_sent_and_marked(self):
        fake_send = AsyncMock()
        fake_mark = AsyncMock()
        row = make_row(id=5, kind="low_battery", message="Battery 10%")

        await process_pending_alerts(
            alerts=[row],
            send_message=fake_send,
            mark_delivered=fake_mark,
            chat_id="456",
        )

        fake_send.assert_awaited_once()
        # Verify message contains something meaningful
        sent_text = fake_send.call_args[1].get("text") or fake_send.call_args[0][0]
        assert "10%" in sent_text or "battery" in sent_text.lower() or "Battery" in sent_text

        fake_mark.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_multiple_alerts_all_sent(self):
        fake_send = AsyncMock()
        fake_mark = AsyncMock()
        rows = [
            make_row(id=1, kind="water_needed", message="Rose needs water"),
            make_row(id=2, kind="stale", message="No reading for 6h"),
            make_row(id=3, kind="low_battery", message="Battery critical"),
        ]

        await process_pending_alerts(
            alerts=rows,
            send_message=fake_send,
            mark_delivered=fake_mark,
            chat_id="789",
        )

        assert fake_send.await_count == 3
        assert fake_mark.await_count == 3
        fake_mark.assert_any_await(1)
        fake_mark.assert_any_await(2)
        fake_mark.assert_any_await(3)

    @pytest.mark.asyncio
    async def test_send_failure_does_not_mark_delivered(self):
        """If send_message raises, the alert must NOT be marked delivered."""
        fake_send = AsyncMock(side_effect=Exception("Telegram unavailable"))
        fake_mark = AsyncMock()
        row = make_row(id=7)

        with pytest.raises(Exception, match="Telegram unavailable"):
            await process_pending_alerts(
                alerts=[row],
                send_message=fake_send,
                mark_delivered=fake_mark,
                chat_id="000",
            )

        fake_mark.assert_not_called()

    @pytest.mark.asyncio
    async def test_mark_failure_propagates(self):
        """If DB mark fails, exception propagates (will retry next poll cycle)."""
        fake_send = AsyncMock()
        fake_mark = AsyncMock(side_effect=Exception("DB error"))
        row = make_row(id=8)

        with pytest.raises(Exception, match="DB error"):
            await process_pending_alerts(
                alerts=[row],
                send_message=fake_send,
                mark_delivered=fake_mark,
                chat_id="000",
            )


class TestAlertCards:
    """send_photo path: card when a plant is known, text otherwise/on failure."""

    @pytest.mark.asyncio
    async def test_plant_alert_sent_as_card(self):
        fake_send = AsyncMock()
        fake_photo = AsyncMock()
        fake_mark = AsyncMock()
        row = make_row(id=7, plant_id=42, message="Out of range (moisture)")

        await process_pending_alerts(
            alerts=[row],
            send_message=fake_send,
            send_photo=fake_photo,
            mark_delivered=fake_mark,
            chat_id="1",
        )

        fake_photo.assert_awaited_once()
        assert fake_photo.call_args[1]["plant_id"] == 42
        assert "moisture" in fake_photo.call_args[1]["caption"]
        fake_send.assert_not_called()
        fake_mark.assert_awaited_once_with(7)

    @pytest.mark.asyncio
    async def test_alert_without_plant_stays_text(self):
        fake_send = AsyncMock()
        fake_photo = AsyncMock()
        fake_mark = AsyncMock()
        row = make_row(id=8, plant_id=None, kind="stale")

        await process_pending_alerts(
            alerts=[row],
            send_message=fake_send,
            send_photo=fake_photo,
            mark_delivered=fake_mark,
            chat_id="1",
        )

        fake_photo.assert_not_called()
        fake_send.assert_awaited_once()
        fake_mark.assert_awaited_once_with(8)

    @pytest.mark.asyncio
    async def test_out_of_range_alert_sent_as_card(self):
        fake_send = AsyncMock()
        fake_photo = AsyncMock()
        fake_mark = AsyncMock()
        row = make_row(id=11, plant_id=42, kind="out_of_range")

        await process_pending_alerts(
            alerts=[row],
            send_message=fake_send,
            send_photo=fake_photo,
            mark_delivered=fake_mark,
            chat_id="1",
        )

        fake_photo.assert_awaited_once()
        fake_send.assert_not_called()

    @pytest.mark.parametrize("kind", ["stale", "low_battery", "stuck", "new_sensor"])
    @pytest.mark.asyncio
    async def test_non_metric_alert_stays_text_even_with_a_plant(self, kind):
        """The card shows current metric state; for these kinds it would
        contradict the alert text (four in-range dots under "no reading in
        >24h"). They belong to Grafana Alerting, not to a plant card."""
        fake_send = AsyncMock()
        fake_photo = AsyncMock()
        fake_mark = AsyncMock()
        row = make_row(id=12, plant_id=42, kind=kind)

        await process_pending_alerts(
            alerts=[row],
            send_message=fake_send,
            send_photo=fake_photo,
            mark_delivered=fake_mark,
            chat_id="1",
        )

        fake_photo.assert_not_called()
        fake_send.assert_awaited_once()
        fake_mark.assert_awaited_once_with(12)

    @pytest.mark.asyncio
    async def test_card_failure_falls_back_to_text(self):
        fake_send = AsyncMock()
        fake_photo = AsyncMock(side_effect=RuntimeError("canopy down"))
        fake_mark = AsyncMock()
        row = make_row(id=9, plant_id=3)

        await process_pending_alerts(
            alerts=[row],
            send_message=fake_send,
            send_photo=fake_photo,
            mark_delivered=fake_mark,
            chat_id="1",
        )

        fake_send.assert_awaited_once()
        fake_mark.assert_awaited_once_with(9)

    @pytest.mark.asyncio
    async def test_card_and_text_both_failing_leaves_undelivered(self):
        fake_send = AsyncMock(side_effect=RuntimeError("telegram down"))
        fake_photo = AsyncMock(side_effect=RuntimeError("canopy down"))
        fake_mark = AsyncMock()

        with pytest.raises(RuntimeError):
            await process_pending_alerts(
                alerts=[make_row(id=10, plant_id=3)],
                send_message=fake_send,
                send_photo=fake_photo,
                mark_delivered=fake_mark,
                chat_id="1",
            )

        fake_mark.assert_not_called()
