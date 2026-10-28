"""
Tests for command argument parsing (offline, no bot API).
"""
import pytest
from app.commands import (
    parse_addsensor_args,
    parse_assign_args,
    parse_setthreshold_args,
    parse_remove_sensor_args,
    parse_silence_args,
    parse_unassign_args,
)


class TestParseAssignArgs:
    def test_valid_assign(self):
        sensor, plant = parse_assign_args("/assign Window-1 Rose")
        assert sensor == "Window-1"
        assert plant == "Rose"

    def test_valid_assign_no_slash(self):
        sensor, plant = parse_assign_args("/assign sensor123 My Fancy Plant")
        assert sensor == "sensor123"
        assert plant == "My Fancy Plant"

    def test_valid_assign_multiword_plant(self):
        sensor, plant = parse_assign_args("/assign balcony-2 Golden Pothos")
        assert sensor == "balcony-2"
        assert plant == "Golden Pothos"

    def test_missing_plant_raises(self):
        with pytest.raises(ValueError, match="plant"):
            parse_assign_args("/assign OnlySensor")

    def test_empty_args_raises(self):
        with pytest.raises(ValueError):
            parse_assign_args("/assign")

    def test_extra_spaces(self):
        sensor, plant = parse_assign_args("/assign   sensor-1   Rose  ")
        assert sensor == "sensor-1"
        assert plant == "Rose"


class TestParseUnassignArgs:
    def test_valid_unassign(self):
        sensor = parse_unassign_args("/unassign 3")
        assert sensor == "3"

    def test_valid_unassign_name(self):
        sensor = parse_unassign_args("/unassign balcony-1")
        assert sensor == "balcony-1"

    def test_missing_sensor_raises(self):
        with pytest.raises(ValueError):
            parse_unassign_args("/unassign")

    def test_extra_spaces(self):
        sensor = parse_unassign_args("/unassign   sensor-2  ")
        assert sensor == "sensor-2"


class TestParseSetthresholdArgs:
    def test_valid_single_word_plant(self):
        plant, param, min_val, max_val = parse_setthreshold_args(
            "/setthreshold Rose moisture 15 60"
        )
        assert plant == "Rose"
        assert param == "moisture"
        assert min_val == 15.0
        assert max_val == 60.0

    def test_valid_multiword_plant(self):
        plant, param, min_val, max_val = parse_setthreshold_args(
            "/setthreshold Golden Pothos ec 150 2500"
        )
        assert plant == "Golden Pothos"
        assert param == "ec"
        assert min_val == 150.0
        assert max_val == 2500.0

    def test_valid_light(self):
        plant, param, min_val, max_val = parse_setthreshold_args(
            "/setthreshold Monstera deliciosa light 500 2500"
        )
        assert plant == "Monstera deliciosa"
        assert param == "light"
        assert min_val == 500.0
        assert max_val == 2500.0

    def test_valid_temp(self):
        plant, param, min_val, max_val = parse_setthreshold_args(
            "/setthreshold Cactus temp 5 40"
        )
        assert plant == "Cactus"
        assert param == "temp"
        assert min_val == 5.0
        assert max_val == 40.0

    def test_invalid_param_raises(self):
        with pytest.raises(ValueError, match="Invalid parameter"):
            parse_setthreshold_args("/setthreshold Rose humidity 10 80")

    def test_min_greater_than_max_raises(self):
        with pytest.raises(ValueError, match="Min.*less than max"):
            parse_setthreshold_args("/setthreshold Rose moisture 60 15")

    def test_non_numeric_min_raises(self):
        with pytest.raises(ValueError, match="Invalid min"):
            parse_setthreshold_args("/setthreshold Rose moisture abc 60")

    def test_non_numeric_max_raises(self):
        with pytest.raises(ValueError, match="Invalid max"):
            parse_setthreshold_args("/setthreshold Rose moisture 15 xyz")

    def test_too_few_args_raises(self):
        with pytest.raises(ValueError):
            parse_setthreshold_args("/setthreshold Rose moisture")

    def test_no_plant_raises(self):
        with pytest.raises(ValueError):
            parse_setthreshold_args("/setthreshold moisture 15 60")

    def test_case_insensitive_param(self):
        plant, param, min_val, max_val = parse_setthreshold_args(
            "/setthreshold Rose MOISTURE 15 60"
        )
        assert param == "moisture"

    def test_equal_min_max_raises(self):
        with pytest.raises(ValueError, match="Min.*less than max"):
            parse_setthreshold_args("/setthreshold Rose moisture 30 30")


class TestParseSilenceArgs:
    def test_bare_form_means_every_kind(self):
        """No kind = silence the sensor outright. This used to default to
        'stuck', which is now the one kind that never reaches Telegram
        anyway (Grafana owns it, muted) — so the default silenced nothing
        the user could hear."""
        sensor, kind = parse_silence_args("/silence balcony-1")
        assert sensor == "balcony-1"
        assert kind is None

    def test_explicit_kind(self):
        sensor, kind = parse_silence_args("/silence balcony-1 low_battery")
        assert sensor == "balcony-1"
        assert kind == "low_battery"

    def test_no_slash(self):
        sensor, kind = parse_silence_args("balcony-1")
        assert sensor == "balcony-1"
        assert kind is None

    def test_new_sensor_is_silenceable(self):
        _, kind = parse_silence_args("/silence balcony-1 new_sensor")
        assert kind == "new_sensor"

    def test_case_insensitive_kind(self):
        _, kind = parse_silence_args("/silence balcony-1 STUCK")
        assert kind == "stuck"

    def test_invalid_kind_raises(self):
        with pytest.raises(ValueError, match="Invalid alert kind"):
            parse_silence_args("/silence balcony-1 bogus")

    def test_missing_sensor_raises(self):
        with pytest.raises(ValueError):
            parse_silence_args("/silence")


class TestParseRemoveSensorArgs:
    def test_without_confirm(self):
        ref, confirmed = parse_remove_sensor_args("/removesensor balcony-1")
        assert ref == "balcony-1"
        assert confirmed is False

    def test_with_confirm(self):
        ref, confirmed = parse_remove_sensor_args("/removesensor balcony-1 confirm")
        assert ref == "balcony-1"
        assert confirmed is True

    def test_confirm_is_case_insensitive(self):
        _, confirmed = parse_remove_sensor_args("/removesensor balcony-1 CONFIRM")
        assert confirmed is True

    def test_any_other_word_is_not_confirmation(self):
        """Only the literal word confirms — a typo must not delete readings."""
        _, confirmed = parse_remove_sensor_args("/removesensor balcony-1 yes")
        assert confirmed is False

    def test_missing_sensor_raises(self):
        with pytest.raises(ValueError):
            parse_remove_sensor_args("/removesensor")


class TestParseAddsensorArgs:
    def test_mac_and_number(self):
        ref, number = parse_addsensor_args("/addsensor A4:C1:38:AA:BB:CC 25")
        assert ref == "A4:C1:38:AA:BB:CC"
        assert number == "25"

    def test_bot_mention_suffix(self):
        """Telegram appends @botname when a command is picked from the menu in
        a group chat."""
        ref, number = parse_addsensor_args("/addsensor@SoilSentryBot C4:7C:8D:6C:E9:12 6")
        assert ref == "C4:7C:8D:6C:E9:12"
        assert number == "6"

    def test_no_slash(self):
        ref, number = parse_addsensor_args("addsensor A4:C1:38:AA:BB:CC 7")
        assert ref == "A4:C1:38:AA:BB:CC"
        assert number == "7"

    def test_missing_number_raises(self):
        with pytest.raises(ValueError):
            parse_addsensor_args("/addsensor A4:C1:38:AA:BB:CC")

    def test_missing_everything_raises(self):
        with pytest.raises(ValueError):
            parse_addsensor_args("/addsensor")

    @pytest.mark.parametrize("bad", ["twentyfive", "25a", "2.5", "-3"])
    def test_non_numeric_number_raises(self, bad):
        """The label convention is a plain number — sensor_labels renders it as
        'Rose (25)'. Reject anything else rather than write a weird label."""
        with pytest.raises(ValueError, match="number"):
            parse_addsensor_args(f"/addsensor A4:C1:38:AA:BB:CC {bad}")

    def test_trailing_words_raise(self):
        """A plant name here is a misunderstanding of the command — the plant
        comes from /identify. Erroring beats silently dropping the words."""
        with pytest.raises(ValueError, match="plant"):
            parse_addsensor_args("/addsensor A4:C1:38:AA:BB:CC 25 Monstera deliciosa")


# Bugfix regression: setlocation with multi-word location and numeric sensor ID
from app.commands import parse_setlocation_args, parse_rename_args, parse_delete_args


class TestParseSetlocationArgs:
    def test_single_word_location(self):
        sensor, loc = parse_setlocation_args("/setlocation balcony-1 Kitchen")
        assert sensor == "balcony-1"
        assert loc == "Kitchen"

    def test_multi_word_location(self):
        sensor, loc = parse_setlocation_args("/setlocation 28 living room")
        assert sensor == "28"
        assert loc == "living room"

    def test_three_word_location(self):
        sensor, loc = parse_setlocation_args("/setlocation 28 south facing window")
        assert sensor == "28"
        assert loc == "south facing window"

    def test_missing_location_raises(self):
        with pytest.raises(ValueError):
            parse_setlocation_args("/setlocation 28")

    def test_missing_all_raises(self):
        with pytest.raises(ValueError):
            parse_setlocation_args("/setlocation")

    def test_extra_spaces_collapsed(self):
        sensor, loc = parse_setlocation_args("/setlocation   28   living   room  ")
        assert sensor == "28"
        assert loc == "living room"


class TestParseRenameArgs:
    def test_multi_word_name(self):
        sensor, name = parse_rename_args("/rename 26 Monstera Deliciosa")
        assert sensor == "26"
        assert name == "Monstera Deliciosa"

    def test_single_word_name(self):
        sensor, name = parse_rename_args("/rename balcony-1 Fern")
        assert sensor == "balcony-1"
        assert name == "Fern"

    def test_missing_name_raises(self):
        with pytest.raises(ValueError):
            parse_rename_args("/rename 26")


class TestParseDeleteArgs:
    def test_numeric_sensor_id(self):
        ref = parse_delete_args("/delete 28")
        assert ref == "28"

    def test_sensor_name(self):
        ref = parse_delete_args("/delete balcony-1")
        assert ref == "balcony-1"

    def test_multi_word_plant_name(self):
        ref = parse_delete_args("/delete Dracaena reflexa")
        assert ref == "Dracaena reflexa"

    def test_missing_arg_raises(self):
        with pytest.raises(ValueError):
            parse_delete_args("/delete")
