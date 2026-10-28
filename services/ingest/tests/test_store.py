"""Tests for ensure_sensor's return-from-absence hook.

There is no DB fixture in this service, so the connection is faked down to the
two calls ensure_sensor actually makes.
"""
from datetime import datetime, timedelta, timezone

import app.store as store


class FakeCursor:
    def __init__(self, row):
        self._row = row
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self, row):
        self.cur = FakeCursor(row)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


def _run(last_seen, active=True):
    """ensure_sensor against an existing sensor last seen at `last_seen`."""
    calls = []
    store.on_sensor_return = lambda mac, gap: calls.append((mac, gap))
    try:
        conn = FakeConn((7, active, last_seen))
        sid = store.ensure_sensor(conn, "aa:bb:cc:dd:ee:ff")
    finally:
        store.on_sensor_return = None
    return sid, calls


def test_returning_sensor_triggers_battery_check():
    """A sensor silent for longer than the threshold gets an immediate read —
    dropping out and coming back is the classic dying-cell pattern."""
    long_ago = datetime.now(tz=timezone.utc) - timedelta(hours=30)
    sid, calls = _run(long_ago)
    assert sid == 7
    assert len(calls) == 1
    mac, gap = calls[0]
    assert mac == "AA:BB:CC:DD:EE:FF"   # normalised to upper case
    assert 29 < gap < 31


def test_regularly_reporting_sensor_does_not_trigger():
    """The common case by far — must not fire on every reading."""
    recent = datetime.now(tz=timezone.utc) - timedelta(minutes=20)
    _, calls = _run(recent)
    assert calls == []


def test_retired_sensor_never_triggers():
    """An inactive sensor is a tombstone; it must stay silent in every path."""
    long_ago = datetime.now(tz=timezone.utc) - timedelta(days=5)
    sid, calls = _run(long_ago, active=False)
    assert sid is None
    assert calls == []


def test_hook_failure_does_not_break_ingestion():
    """A broken publisher must never stop readings being written."""
    def boom(mac, gap):
        raise RuntimeError("mqtt down")

    store.on_sensor_return = boom
    try:
        conn = FakeConn((7, True, datetime.now(tz=timezone.utc) - timedelta(hours=30)))
        assert store.ensure_sensor(conn, "AA:BB:CC:DD:EE:FF") == 7
    finally:
        store.on_sensor_return = None


# ─── auto-registration gate ─────────────────────────────────────────────────

class RecordingCursor(FakeCursor):
    """Cursor that remembers every statement, so we can assert nothing was
    inserted for a refused device.

    fetchone() is sequenced rather than canned: the registration path does a
    SELECT that must miss (None), then an INSERT ... RETURNING id that must
    yield the new id.
    """

    def __init__(self, row):
        super().__init__(row)
        self.statements = []
        self._returns = [None, (7,)]

    def execute(self, q, params=None):
        self.statements.append(q)
        return super().execute(q, params)

    def fetchone(self):
        return self._returns.pop(0) if self._returns else None


class RecordingConn(FakeConn):
    def __init__(self, row):
        super().__init__(row)
        self.cur = RecordingCursor(row)


def _register(model_id):
    """ensure_sensor against an UNKNOWN mac (no existing row)."""
    conn = RecordingConn(None)
    sid = store.ensure_sensor(conn, "aa:bb:cc:dd:ee:ff", model_id=model_id)
    return sid, conn


def test_known_plant_sensor_model_is_registered():
    sid, conn = _register("HHCCJCY01HHCC")
    assert sid == 7   # FakeCursor returns the canned RETURNING row
    assert any("INSERT INTO sensors" in s for s in conn.cur.statements)


def test_foreign_ble_device_is_not_registered():
    """A neighbour's thermometer decoded by the gateway must not become a
    plant sensor — this is what put aa:bb:cc:dd:ee:ff in the fleet."""
    sid, conn = _register("LYWSD03MMC")
    assert sid is None
    assert not any("INSERT INTO sensors" in s for s in conn.cur.statements)
    assert not any("alert_events" in s for s in conn.cur.statements)


def test_payload_without_a_model_is_not_registered():
    """Conservative default: an unidentifiable new device is refused. A real
    MiFlora sends model_id on every message, so it registers on the next one."""
    sid, _ = _register(None)
    assert sid is None


def test_gate_applies_only_to_new_macs():
    """An already-known sensor keeps ingesting whatever it sends — notably the
    Pi's battery collector, which publishes its own minimal payload."""
    conn = FakeConn((7, True, datetime.now(tz=timezone.utc) - timedelta(minutes=5)))
    assert store.ensure_sensor(conn, "AA:BB:CC:DD:EE:FF", model_id=None) == 7
