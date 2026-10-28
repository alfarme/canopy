"""Unit tests for merger.py — BucketMerger.

All time is faked via monkeypatching time.time; no network/DB/broker needed.
"""
from __future__ import annotations

import time as time_module
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from app.merger import BucketMerger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_decoded(mac: str, **fields) -> dict[str, Any]:
    return {"mac": mac, **fields}


def fake_flusher():
    """Returns a flusher that records calls and their arguments."""
    calls = []

    def flusher(mac: str, bucket_time: datetime, fields: dict[str, Any]) -> None:
        calls.append({"mac": mac, "bucket_time": bucket_time, "fields": dict(fields)})

    flusher.calls = calls  # type: ignore[attr-defined]
    return flusher


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

BUCKET_MIN = 30
BUCKET_SEC = BUCKET_MIN * 60  # 1800


class TestBucketMerger:
    def test_single_ingest_no_flush_yet(self):
        """Single message ingested — no flush until bucket rolls over."""
        flusher = fake_flusher()
        merger = BucketMerger(bucket_minutes=BUCKET_MIN, flusher=flusher)

        with patch("app.merger.time.time", return_value=100.0):
            merger.ingest(make_decoded("AA:BB:CC:DD:EE:FF", temp_c=21.4))

        assert len(flusher.calls) == 0

    def test_bucket_rollover_triggers_flush(self):
        """When bucket rolls over, previous bucket is flushed."""
        flusher = fake_flusher()
        merger = BucketMerger(bucket_minutes=BUCKET_MIN, flusher=flusher)

        mac = "AA:BB:CC:DD:EE:FF"
        # First message in bucket 0 (t=100s)
        with patch("app.merger.time.time", return_value=100.0):
            merger.ingest(make_decoded(mac, temp_c=21.4))

        # Message in bucket 1 (t=1900s = bucket 1800)
        with patch("app.merger.time.time", return_value=BUCKET_SEC + 100.0):
            merger.ingest(make_decoded(mac, moisture=38.0))

        assert len(flusher.calls) == 1
        call = flusher.calls[0]
        assert call["mac"] == mac
        assert call["fields"]["temp_c"] == pytest.approx(21.4)
        # bucket_time should be start of bucket 0
        assert call["bucket_time"] == datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    def test_multiple_partials_merged_in_bucket(self):
        """Four partial messages are merged into one flush."""
        flusher = fake_flusher()
        merger = BucketMerger(bucket_minutes=BUCKET_MIN, flusher=flusher)
        mac = "AA:BB:CC:DD:EE:FF"

        # All in bucket 0 (t < 1800)
        times = [100.0, 200.0, 300.0, 400.0]
        payloads = [
            make_decoded(mac, temp_c=21.4, rssi=-68),
            make_decoded(mac, moisture=38.0, rssi=-69),
            make_decoded(mac, light_lux=1240, rssi=-70),
            make_decoded(mac, conductivity_us_cm=187, rssi=-71),
        ]
        for t, payload in zip(times, payloads):
            with patch("app.merger.time.time", return_value=t):
                merger.ingest(payload)

        # Roll into bucket 1 to trigger flush
        with patch("app.merger.time.time", return_value=BUCKET_SEC + 10.0):
            merger.ingest(make_decoded(mac, temp_c=22.0, rssi=-65))

        assert len(flusher.calls) == 1
        fields = flusher.calls[0]["fields"]
        assert fields["temp_c"] == pytest.approx(21.4)
        assert fields["moisture"] == pytest.approx(38.0)
        assert fields["light_lux"] == 1240
        assert fields["conductivity_us_cm"] == 187
        assert fields["rssi"] == -71  # last value wins

    def test_flush_all_flushes_open_buckets(self):
        """flush_all() writes all open buckets to DB."""
        flusher = fake_flusher()
        merger = BucketMerger(bucket_minutes=BUCKET_MIN, flusher=flusher)

        macs = ["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"]
        with patch("app.merger.time.time", return_value=100.0):
            for mac in macs:
                merger.ingest(make_decoded(mac, temp_c=20.0))

        with patch("app.merger.time.time", return_value=200.0):
            merger.flush_all()
        assert len(flusher.calls) == 2
        flushed_macs = {c["mac"] for c in flusher.calls}
        assert flushed_macs == set(macs)

    def test_different_macs_independent_buckets(self):
        """Each MAC has its own bucket state."""
        flusher = fake_flusher()
        merger = BucketMerger(bucket_minutes=BUCKET_MIN, flusher=flusher)

        mac1 = "AA:BB:CC:DD:EE:FF"
        mac2 = "11:22:33:44:55:66"

        with patch("app.merger.time.time", return_value=100.0):
            merger.ingest(make_decoded(mac1, temp_c=21.0))
            merger.ingest(make_decoded(mac2, temp_c=18.0))

        # Only mac1 reports again, in the next bucket
        with patch("app.merger.time.time", return_value=BUCKET_SEC + 10.0):
            merger.ingest(make_decoded(mac1, temp_c=22.0))

        # Both bucket-0 entries close: buckets are keyed per MAC, but the
        # clock that closes them is shared, so mac2's reading is written
        # rather than stranded waiting for a MAC that may never return.
        assert {c["mac"] for c in flusher.calls} == {mac1, mac2}
        assert len(flusher.calls) == 2
        # mac1's new bucket is still filling
        assert list(merger._buckets.keys()) == [(mac1, BUCKET_SEC)]

    def test_flush_all_empties_state(self):
        """After flush_all, no duplicate flushes on second call."""
        flusher = fake_flusher()
        merger = BucketMerger(bucket_minutes=BUCKET_MIN, flusher=flusher)

        with patch("app.merger.time.time", return_value=100.0):
            merger.ingest(make_decoded("AA:BB:CC:DD:EE:FF", temp_c=21.0))

        with patch("app.merger.time.time", return_value=200.0):
            merger.flush_all()
            assert len(flusher.calls) == 1
            merger.flush_all()  # second call should not produce extra flushes
        assert len(flusher.calls) == 1

    def test_missing_mac_in_decoded_skips(self):
        """Decoded dict missing 'mac' is silently skipped."""
        flusher = fake_flusher()
        merger = BucketMerger(bucket_minutes=BUCKET_MIN, flusher=flusher)

        with patch("app.merger.time.time", return_value=100.0):
            merger.ingest({"temp_c": 21.0})  # no mac

        merger.flush_all()
        assert len(flusher.calls) == 0

    def test_flusher_exception_does_not_crash_merger(self):
        """If flusher raises, merger logs and continues."""

        def bad_flusher(mac, bucket_time, fields):
            raise RuntimeError("DB down")

        merger = BucketMerger(bucket_minutes=BUCKET_MIN, flusher=bad_flusher)
        mac = "AA:BB:CC:DD:EE:FF"

        with patch("app.merger.time.time", return_value=100.0):
            merger.ingest(make_decoded(mac, temp_c=21.0))

        # Should not raise
        with patch("app.merger.time.time", return_value=200.0):
            merger.flush_all()

    def test_bucket_time_is_utc_aware(self):
        """Flushed bucket_time must be UTC timezone-aware."""
        flusher = fake_flusher()
        merger = BucketMerger(bucket_minutes=BUCKET_MIN, flusher=flusher)
        mac = "AA:BB:CC:DD:EE:FF"

        with patch("app.merger.time.time", return_value=100.0):
            merger.ingest(make_decoded(mac, temp_c=21.0))
        with patch("app.merger.time.time", return_value=200.0):
            merger.flush_all()

        assert len(flusher.calls) == 1
        bt = flusher.calls[0]["bucket_time"]
        assert bt.tzinfo is not None
        assert bt.tzinfo == timezone.utc


class TestBucketClosing:
    """Buckets must close on the clock, not on the owning MAC reappearing.

    Regression cover for 2026-08-17: three foreign BLE devices heard once
    each sat open for up to fourteen days, then all flushed together on a
    deploy's shutdown hook -- through the outgoing image, past the
    registration gate that same deploy was shipping.
    """

    DAY = 24 * 3600

    def test_silent_sensor_bucket_is_flushed_by_other_traffic(self):
        """A sensor's last reading before it dies must still be written --
        another MAC's message is enough to close the elapsed bucket."""
        flusher = fake_flusher()
        merger = BucketMerger(bucket_minutes=BUCKET_MIN, flusher=flusher)

        with patch("app.merger.time.time", return_value=100.0):
            merger.ingest(make_decoded("AA:BB:CC:DD:EE:FF", moisture=31.0))

        # A different sensor reports two buckets later; the dead one never does.
        with patch("app.merger.time.time", return_value=2 * BUCKET_SEC + 100.0):
            merger.ingest(make_decoded("11:22:33:44:55:66", temp_c=19.0))

        assert [c["mac"] for c in flusher.calls] == ["AA:BB:CC:DD:EE:FF"]
        assert flusher.calls[0]["fields"]["moisture"] == pytest.approx(31.0)

    def test_open_bucket_does_not_survive_indefinitely(self):
        """The leak itself: a one-shot device must not stay in memory."""
        flusher = fake_flusher()
        merger = BucketMerger(bucket_minutes=BUCKET_MIN, flusher=flusher)

        with patch("app.merger.time.time", return_value=100.0):
            merger.ingest(make_decoded("DE:5E:54:21:A5:A2", temp_c=20.0))
        with patch("app.merger.time.time", return_value=BUCKET_SEC + 100.0):
            merger.ingest(make_decoded("11:22:33:44:55:66", temp_c=19.0))

        assert list(merger._buckets.keys()) == [("11:22:33:44:55:66", BUCKET_SEC)]
        assert list(merger._current_bucket.keys()) == ["11:22:33:44:55:66"]

    def test_ancient_bucket_is_discarded_not_written(self):
        """Past MAX_BUCKET_AGE_HOURS the sample is noise, not a reading."""
        flusher = fake_flusher()
        merger = BucketMerger(bucket_minutes=BUCKET_MIN, flusher=flusher)

        with patch("app.merger.time.time", return_value=100.0):
            merger.ingest(make_decoded("DE:5E:54:21:A5:A2", temp_c=20.0))
        with patch("app.merger.time.time", return_value=2 * self.DAY):
            merger.ingest(make_decoded("11:22:33:44:55:66", temp_c=19.0))

        assert flusher.calls == []
        assert ("DE:5E:54:21:A5:A2", 0) not in merger._buckets

    def test_shutdown_does_not_resurrect_ancient_buckets(self):
        """The exact 2026-08-17 failure: flush_all() on a stale backlog."""
        flusher = fake_flusher()
        merger = BucketMerger(bucket_minutes=BUCKET_MIN, flusher=flusher)

        with patch("app.merger.time.time", return_value=100.0):
            merger.ingest(make_decoded("12:FF:A7:AA:3C:5A", temp_c=20.0))
        with patch("app.merger.time.time", return_value=14 * self.DAY):
            merger.flush_all()

        assert flusher.calls == []
        assert merger._buckets == {}

    def test_recent_bucket_still_flushed_on_shutdown(self):
        """The age rule must not cost us a legitimate in-flight bucket."""
        flusher = fake_flusher()
        merger = BucketMerger(bucket_minutes=BUCKET_MIN, flusher=flusher)

        with patch("app.merger.time.time", return_value=100.0):
            merger.ingest(make_decoded("AA:BB:CC:DD:EE:FF", temp_c=20.0))
        with patch("app.merger.time.time", return_value=200.0):
            merger.flush_all()

        assert len(flusher.calls) == 1
