"""In-memory per-MAC partial-payload bucket merger.

The HHCCJCY01 broadcasts ONE metric per BLE advertisement in rotation
(temp, moisture, lux, conductivity). This class accumulates partials
within a time bucket and flushes complete-enough rows to the DB.

Thread-safety: single-threaded — paho-mqtt callbacks run in one thread.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from .decode import bucket_key, merge_partials
from .log import get_logger

log = get_logger(__name__)

# A flusher callable: (mac, bucket_time_utc, merged_fields) → None
FlusherFn = Callable[[str, datetime, dict[str, Any]], None]

# Fields we consider "sensor data" (not rssi/battery which appear on every msg)
SENSOR_FIELDS = {"temp_c", "moisture", "light_lux", "conductivity_us_cm"}

# A bucket older than this is dropped instead of written. Reaching this age
# means the service was down or idle for a day; a lone sample stamped that far
# back is noise, and flushing it late is how ancient state gets resurrected
# (see the bucket-close note on BucketMerger).
MAX_BUCKET_AGE_HOURS = float(os.environ.get("MAX_BUCKET_AGE_HOURS", "24"))


class BucketMerger:
    """Accumulates partial BLE payloads and flushes completed buckets.

    When a new bucket starts for a MAC, the old bucket is flushed regardless
    of completeness (partial data is still valuable).

    A bucket also closes once its own window has elapsed, on any traffic --
    not only when that same MAC advertises again. The per-MAC-only trigger
    left a bucket open forever whenever a device was heard once and never
    returned: the gateway decodes every BLE advertiser in range, so passing
    strangers accumulated in memory indefinitely, and shutdown's flush_all()
    then wrote all of them at once. On 2026-08-17 a deploy did exactly that,
    flushing three foreign devices last heard up to fourteen days earlier --
    through the OLD image, since the dying container is what runs the shutdown
    hook, so they slipped past the registration gate shipped in that very
    deploy. Closing buckets on time keeps flushes contemporaneous with the
    code that is running, and bounds the dict.

    A sensor's final reading before it dies is still written: the bucket is
    closed and flushed on schedule, not discarded.
    """

    def __init__(self, bucket_minutes: int, flusher: FlusherFn) -> None:
        self.bucket_minutes = bucket_minutes
        self.flusher = flusher
        # { (mac, bucket_start_sec): [partial_dict, ...] }
        self._buckets: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        # { mac: current bucket_start_sec }
        self._current_bucket: dict[str, int] = {}

    def ingest(self, decoded: dict[str, Any]) -> None:
        """Accept a decoded partial dict (as returned by decode_payload).

        Closing is handled by the clock (see sweep), not by this MAC's own
        rollover -- which is what previously stranded one-shot devices.
        """
        mac = decoded.get("mac", "")
        if not mac:
            log.warning("ingest: decoded payload missing mac")
            return

        ts_now = time.time()
        # Any traffic is a clock tick: close whatever is due, for every MAC.
        self.sweep(ts_now)
        key = bucket_key(mac, ts_now, self.bucket_minutes)

        self._current_bucket[mac] = key[1]
        # Store a copy without the mac key (it's in the bucket key)
        partial = {k: v for k, v in decoded.items() if k != "mac"}
        self._buckets[key].append(partial)

    def sweep(self, now: float | None = None) -> None:
        """Close every bucket whose window has elapsed.

        Called on each ingest, so a device that goes silent still gets its
        last bucket written within one bucket period rather than waiting for
        an advertisement that may never come.
        """
        now = time.time() if now is None else now
        window = self.bucket_minutes * 60
        for key in list(self._buckets.keys()):
            age = now - key[1]
            if age < window:
                continue  # still filling
            if age > MAX_BUCKET_AGE_HOURS * 3600:
                self._discard(key, age)
            else:
                self._flush(key)

    def flush_all(self) -> None:
        """Flush open buckets (called on shutdown).

        The age rule applies here too: a shutdown must not resurrect state
        the service has been sitting on since before the current code was
        deployed.
        """
        now = time.time()
        for key in list(self._buckets.keys()):
            age = now - key[1]
            if age > MAX_BUCKET_AGE_HOURS * 3600:
                self._discard(key, age)
            else:
                self._flush(key)

    def _forget(self, key: tuple[str, int]) -> None:
        """Drop the rollover marker if it points at the bucket just closed,
        so _current_bucket does not grow one entry per MAC ever heard."""
        mac, bucket_start_sec = key
        if self._current_bucket.get(mac) == bucket_start_sec:
            del self._current_bucket[mac]

    def _discard(self, key: tuple[str, int], age_sec: float) -> None:
        partials = self._buckets.pop(key, [])
        self._forget(key)
        log.info(
            f"event=bucket_discarded mac={key[0]} age_hours={age_sec / 3600:.1f} "
            f"partials={len(partials)} reason=older_than_max_bucket_age"
        )

    def _flush(self, key: tuple[str, int]) -> None:
        partials = self._buckets.pop(key, [])
        self._forget(key)
        if not partials:
            return
        mac, bucket_start_sec = key
        bucket_time = datetime.fromtimestamp(bucket_start_sec, tz=timezone.utc)
        merged = merge_partials(partials)
        log.info(
            f"flushing bucket mac={mac} "
            f"bucket={bucket_time.isoformat()} "
            f"fields={list(merged.keys())}"
        )
        try:
            self.flusher(mac, bucket_time, merged)
        except Exception as exc:
            log.error(f"flush failed mac={mac}: {exc!r}")
