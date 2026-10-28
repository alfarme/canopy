"""Unit tests for decode.py — pure dict logic, no network/DB/broker."""
import pytest
from app.decode import decode_payload, merge_partials, bucket_key


# ---------------------------------------------------------------------------
# decode_payload
# ---------------------------------------------------------------------------

class TestDecodePayload:
    def test_full_theengs_payload(self):
        """Full HHCCJCY01 payload with all four metrics."""
        raw = {
            "name": "HHCCJCY01",
            "id": "AA:BB:CC:DD:EE:FF",
            "rssi": -68,
            "brand": "Xiaomi/VegTrug",
            "model": "MiFlora",
            "model_id": "HHCCJCY01HHCC",
            "tempc": 21.4,
            "moi": 38,
            "lux": 1240,
            "fer": 187,
        }
        result = decode_payload(raw)
        assert result["mac"] == "AA:BB:CC:DD:EE:FF"
        assert result["temp_c"] == pytest.approx(21.4)
        assert result["moisture"] == pytest.approx(38.0)
        assert result["light_lux"] == 1240
        assert result["conductivity_us_cm"] == 187
        assert result["rssi"] == -68

    def test_partial_payload_temperature_only(self):
        """Single-metric advertisement (temperature only)."""
        raw = {"id": "AA:BB:CC:DD:EE:FF", "rssi": -70, "tempc": 22.1}
        result = decode_payload(raw)
        assert result["mac"] == "AA:BB:CC:DD:EE:FF"
        assert result["temp_c"] == pytest.approx(22.1)
        assert "moisture" not in result
        assert "light_lux" not in result
        assert "conductivity_us_cm" not in result

    def test_partial_payload_moisture_only(self):
        raw = {"id": "CC:DD:EE:FF:00:11", "rssi": -75, "moi": 45}
        result = decode_payload(raw)
        assert result["moisture"] == pytest.approx(45.0)
        assert "temp_c" not in result

    def test_partial_payload_lux_only(self):
        raw = {"id": "CC:DD:EE:FF:00:11", "rssi": -75, "lux": 5000}
        result = decode_payload(raw)
        assert result["light_lux"] == 5000
        assert isinstance(result["light_lux"], int)

    def test_partial_payload_conductivity_only(self):
        raw = {"id": "CC:DD:EE:FF:00:11", "rssi": -75, "fer": 200}
        result = decode_payload(raw)
        assert result["conductivity_us_cm"] == 200
        assert isinstance(result["conductivity_us_cm"], int)

    def test_battery_field(self):
        raw = {"id": "AA:BB:CC:DD:EE:FF", "rssi": -60, "batt": 85}
        result = decode_payload(raw)
        assert result["battery"] == 85
        assert isinstance(result["battery"], int)

    def test_battery_spelled_out_is_not_the_theengs_key(self):
        """`batt` is the wire key; `battery` is only the column name.

        This mapping was wrong for the project's whole history and nothing
        caught it, because MiFlora never sends battery on the passive path —
        so the dead branch was never exercised by real traffic. Anyone
        publishing to canopy/ble/# must use `batt`.
        """
        result = decode_payload({"id": "AA:BB:CC:DD:EE:FF", "rssi": -60, "battery": 85})
        assert "battery" not in result

    def test_mac_normalized_to_uppercase(self):
        raw = {"id": "aa:bb:cc:dd:ee:ff", "rssi": -65, "tempc": 20.0}
        result = decode_payload(raw)
        assert result["mac"] == "AA:BB:CC:DD:EE:FF"

    def test_missing_id_raises_value_error(self):
        raw = {"rssi": -65, "tempc": 20.0}
        with pytest.raises(ValueError, match="missing 'id'"):
            decode_payload(raw)

    def test_unknown_extra_fields_ignored(self):
        raw = {
            "id": "AA:BB:CC:DD:EE:FF",
            "rssi": -60,
            "tempc": 20.0,
            "unknown_field": "ignored",
            "another": 42,
        }
        result = decode_payload(raw)
        assert "unknown_field" not in result
        assert "another" not in result
        assert "temp_c" in result

    def test_none_field_value_skipped(self):
        raw = {"id": "AA:BB:CC:DD:EE:FF", "rssi": -60, "tempc": None, "moi": 30}
        result = decode_payload(raw)
        assert "temp_c" not in result
        assert result["moisture"] == pytest.approx(30.0)

    def test_rssi_is_integer(self):
        raw = {"id": "AA:BB:CC:DD:EE:FF", "rssi": -72}
        result = decode_payload(raw)
        assert result["rssi"] == -72
        assert isinstance(result["rssi"], int)

    def test_float_fields_are_float(self):
        raw = {"id": "AA:BB:CC:DD:EE:FF", "rssi": -60, "tempc": 21, "moi": 38}
        result = decode_payload(raw)
        assert isinstance(result["temp_c"], float)
        assert isinstance(result["moisture"], float)

    def test_integer_fields_from_float_input(self):
        """lux/fer/battery/rssi stored as int even if provided as float."""
        raw = {"id": "AA:BB:CC:DD:EE:FF", "rssi": -68.0, "lux": 1200.7, "fer": 150.0}
        result = decode_payload(raw)
        assert isinstance(result["light_lux"], int)
        assert isinstance(result["conductivity_us_cm"], int)
        assert isinstance(result["rssi"], int)

    def test_zero_values_are_kept(self):
        """Zero is a valid sensor reading (e.g. darkness → lux=0)."""
        raw = {"id": "AA:BB:CC:DD:EE:FF", "rssi": -60, "lux": 0, "moi": 0}
        result = decode_payload(raw)
        assert result["light_lux"] == 0
        assert result["moisture"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# merge_partials
# ---------------------------------------------------------------------------

class TestMergePartials:
    def test_merge_two_partials_no_overlap(self):
        p1 = {"temp_c": 21.4, "rssi": -68}
        p2 = {"moisture": 38.0, "rssi": -70}
        merged = merge_partials([p1, p2])
        assert merged["temp_c"] == pytest.approx(21.4)
        assert merged["moisture"] == pytest.approx(38.0)
        # Later value wins for rssi
        assert merged["rssi"] == -70

    def test_merge_four_partials_full_reading(self):
        partials = [
            {"temp_c": 21.4, "rssi": -68},
            {"moisture": 38.0, "rssi": -69},
            {"light_lux": 1240, "rssi": -70},
            {"conductivity_us_cm": 187, "rssi": -71},
        ]
        merged = merge_partials(partials)
        assert merged["temp_c"] == pytest.approx(21.4)
        assert merged["moisture"] == pytest.approx(38.0)
        assert merged["light_lux"] == 1240
        assert merged["conductivity_us_cm"] == 187
        assert merged["rssi"] == -71  # last value wins

    def test_later_value_overrides_earlier(self):
        p1 = {"temp_c": 20.0}
        p2 = {"temp_c": 21.0}  # updated reading — later wins
        merged = merge_partials([p1, p2])
        assert merged["temp_c"] == pytest.approx(21.0)

    def test_single_partial(self):
        p = {"temp_c": 22.5, "rssi": -65}
        merged = merge_partials([p])
        assert merged == p

    def test_empty_list_raises(self):
        with pytest.raises(ValueError, match="empty list"):
            merge_partials([])

    def test_merge_preserves_mac(self):
        p1 = {"mac": "AA:BB:CC:DD:EE:FF", "temp_c": 20.0}
        p2 = {"mac": "AA:BB:CC:DD:EE:FF", "moisture": 35.0}
        merged = merge_partials([p1, p2])
        assert merged["mac"] == "AA:BB:CC:DD:EE:FF"


# ---------------------------------------------------------------------------
# bucket_key
# ---------------------------------------------------------------------------

class TestBucketKey:
    def test_same_bucket_within_window(self):
        """Two timestamps in the same 30-min window → same bucket key."""
        # 30-min bucket: 0–1799 s within a 30-min period
        t1 = 0 * 60 + 1    # 00:00:01 UTC  → bucket start = 0
        t2 = 29 * 60 + 59   # 00:29:59 UTC  → bucket start = 0
        k1 = bucket_key("AA:BB:CC:DD:EE:FF", t1, 30)
        k2 = bucket_key("AA:BB:CC:DD:EE:FF", t2, 30)
        assert k1 == k2

    def test_different_buckets_across_boundary(self):
        """Timestamps in different 30-min windows → different bucket keys."""
        t1 = 29 * 60 + 59   # 00:29:59 → bucket 0
        t2 = 30 * 60 + 1    # 00:30:01 → bucket 1800
        k1 = bucket_key("AA:BB:CC:DD:EE:FF", t1, 30)
        k2 = bucket_key("AA:BB:CC:DD:EE:FF", t2, 30)
        assert k1 != k2

    def test_bucket_start_is_multiple_of_bucket_seconds(self):
        ts = 45 * 60 + 30   # 00:45:30 → bucket start should be 00:30:00 = 1800s
        mac, bucket_start = bucket_key("AA:BB:CC:DD:EE:FF", ts, 30)
        assert bucket_start == 1800  # 30 * 60

    def test_mac_in_bucket_key(self):
        mac1 = "AA:BB:CC:DD:EE:FF"
        mac2 = "11:22:33:44:55:66"
        k1 = bucket_key(mac1, 100, 30)
        k2 = bucket_key(mac2, 100, 30)
        assert k1[0] == mac1
        assert k2[0] == mac2
        assert k1 != k2

    def test_mac_normalized_uppercase_in_key(self):
        k_lower = bucket_key("aa:bb:cc:dd:ee:ff", 100, 30)
        k_upper = bucket_key("AA:BB:CC:DD:EE:FF", 100, 30)
        assert k_lower == k_upper

    def test_custom_bucket_minutes(self):
        """Test with 15-minute buckets."""
        t1 = 14 * 60 + 59  # 00:14:59 → bucket 0
        t2 = 15 * 60 + 1   # 00:15:01 → bucket 900
        k1 = bucket_key("AA:BB:CC:DD:EE:FF", t1, 15)
        k2 = bucket_key("AA:BB:CC:DD:EE:FF", t2, 15)
        assert k1 != k2
        assert k1[1] == 0
        assert k2[1] == 900
