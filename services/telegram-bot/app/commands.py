"""
Command argument parsing — pure functions, no network/DB dependency.
"""
from __future__ import annotations


def parse_assign_args(text: str) -> tuple[str, str]:
    """
    Parse '/assign <sensor> <plant name>' text.

    Returns (sensor_ref, plant_name).
    Raises ValueError with a descriptive message on bad input.
    """
    parts = text.strip().split(None)
    if not parts:
        raise ValueError("usage: /assign <sensor> <plant>")

    # Drop the command token (/assign)
    if parts[0].startswith("/"):
        parts = parts[1:]

    if len(parts) < 2:
        raise ValueError("usage: /assign <sensor> <plant> — plant name is required")

    sensor = parts[0]
    plant = " ".join(parts[1:]).strip()

    if not plant:
        raise ValueError("usage: /assign <sensor> <plant> — plant name is required")

    return sensor, plant


VALID_ALERT_KINDS = (
    "water_needed", "stale", "stuck", "out_of_range", "low_battery", "new_sensor",
    # Range conditions. `out_of_range` stays valid: it is what older silences
    # are stored under, and the engine still honours it as "mute all of these".
    "frost_risk", "heat_stress", "feed_needed", "salt_buildup",
    "soil_not_draining", "light_low", "light_high",
)


def parse_silence_args(text: str) -> tuple[str, str | None]:
    """
    Parse '/silence <sensor> [kind]' (also used for /unsilence) text.

    Returns (sensor_ref, kind), where kind is None when omitted — meaning
    *every* kind, i.e. silence the sensor outright.

    The bare form used to default to "stuck", back when stuck was the noisy
    kind this command was built for. Stuck no longer reaches Telegram at all
    (Grafana owns it and its route is muted), so that default silenced the one
    thing that was already quiet. "All kinds" is what the bare form is
    actually asked for.
    """
    parts = text.strip().split(None)
    if not parts:
        raise ValueError("usage: /silence <sensor> [kind]")

    if parts[0].startswith("/"):
        parts = parts[1:]

    if len(parts) < 1:
        raise ValueError("usage: /silence <sensor> [kind]")

    sensor = parts[0]
    if len(parts) == 1:
        return sensor, None

    kind = parts[1].lower()
    if kind not in VALID_ALERT_KINDS:
        raise ValueError(
            f"Invalid alert kind '{kind}'. Must be one of: {', '.join(VALID_ALERT_KINDS)}"
        )

    return sensor, kind


def parse_remove_sensor_args(text: str) -> tuple[str, bool]:
    """
    Parse '/removesensor <sensor> [confirm]'.

    Returns (sensor_ref, confirmed). This one deletes every reading a sensor
    ever produced, so unlike the other destructive commands it is two-step:
    without the literal word "confirm" the caller is expected to show what
    would be destroyed and stop.
    """
    parts = text.strip().split(None)
    if not parts:
        raise ValueError("usage: /removesensor <sensor> [confirm]")

    if parts[0].startswith("/"):
        parts = parts[1:]

    if len(parts) < 1:
        raise ValueError("usage: /removesensor <sensor> [confirm]")

    confirmed = len(parts) > 1 and parts[1].lower() == "confirm"
    return parts[0], confirmed


def parse_addsensor_args(text: str) -> tuple[str, str]:
    """
    Parse '/addsensor <sensor> <number>'.

    Returns (sensor_ref, number). The sensor_ref is normally the MAC straight
    out of the new_sensor alert, since a freshly auto-registered sensor has no
    name to refer to it by yet.

    Only the number is set here. Plant and location arrive later through the
    /identify (photo) flow, so a trailing plant name is a misunderstanding of
    the command rather than extra credit — reject it instead of dropping it.

    The number must be digits: sensor_labels renders it as "Rose (25)", and
    /sensors orders by LENGTH(name) then name, both of which assume a numeric
    label. Letting "balcony-left" through would quietly break that ordering.
    """
    parts = text.strip().split(None)
    if not parts:
        raise ValueError("usage: /addsensor MAC NUMBER")

    if parts[0].startswith("/") or parts[0].lower().split("@")[0] == "addsensor":
        parts = parts[1:]

    if len(parts) < 2:
        raise ValueError(
            "usage: /addsensor MAC NUMBER — the sensor's MAC (from the "
            "new-sensor alert) and the number to label it with"
        )

    if len(parts) > 2:
        raise ValueError(
            "usage: /addsensor MAC NUMBER — only the number goes here. "
            "Attach the plant with a photo (/identify) once the sensor is added."
        )

    sensor, number = parts[0], parts[1]
    if not number.isdigit():
        raise ValueError(f"'{number}' is not a number — sensors are labelled 1, 2, 3, …")

    return sensor, number


def parse_unassign_args(text: str) -> str:
    """
    Parse '/unassign <sensor>' text.

    Returns sensor_ref.
    Raises ValueError on bad input.
    """
    parts = text.strip().split(None)
    if not parts:
        raise ValueError("usage: /unassign <sensor>")

    if parts[0].startswith("/"):
        parts = parts[1:]

    if len(parts) < 1:
        raise ValueError("usage: /unassign <sensor>")

    return parts[0]


def parse_delete_args(text: str) -> str:
    """
    Parse '/delete <sensor|plant-name>' text.

    Returns the reference string (sensor name/mac or plant display_name).
    Raises ValueError on bad input.
    """
    parts = text.strip().split(None)
    if not parts:
        raise ValueError("usage: /delete <sensor or plant name>")

    if parts[0].startswith("/"):
        parts = parts[1:]

    if len(parts) < 1:
        raise ValueError("usage: /delete <sensor or plant name>")

    return " ".join(parts)


def parse_rename_args(text: str) -> tuple[str, str]:
    """
    Parse '/rename <sensor> <new name>' text.

    Returns (sensor_ref, new_name).
    Raises ValueError on bad input.
    """
    parts = text.strip().split(None)
    if not parts:
        raise ValueError("usage: /rename <sensor> <new name>")

    if parts[0].startswith("/"):
        parts = parts[1:]

    if len(parts) < 2:
        raise ValueError("usage: /rename <sensor> <new name>")

    sensor = parts[0]
    new_name = " ".join(parts[1:]).strip()

    if not new_name:
        raise ValueError("usage: /rename <sensor> <new name>")

    return sensor, new_name


def parse_setlocation_args(text: str) -> tuple[str, str]:
    """
    Parse '/setlocation <sensor> <text>' text.

    Returns (sensor_ref, location).
    Raises ValueError on bad input.
    """
    parts = text.strip().split(None)
    if not parts:
        raise ValueError("usage: /setlocation <sensor> <location>")

    if parts[0].startswith("/"):
        parts = parts[1:]

    if len(parts) < 2:
        raise ValueError("usage: /setlocation <sensor> <location>")

    sensor = parts[0]
    location = " ".join(parts[1:]).strip()

    if not location:
        raise ValueError("usage: /setlocation <sensor> <location>")

    return sensor, location


def parse_setthreshold_args(text: str) -> tuple[str, str, float, float]:
    """
    Parse '/setthreshold <plant> <param> <min> <max>' text.

    Returns (plant_name, param, min_val, max_val).
    Raises ValueError on bad input.

    The param must be one of: moisture, ec, light, temp.
    The plant name can be multi-word, so we parse from the right:
    the last 3 tokens are param, min, max; everything between the
    command and those 3 is the plant name.
    """
    VALID_PARAMS = ("moisture", "ec", "light", "temp")

    parts = text.strip().split(None)
    if not parts:
        raise ValueError("usage: /setthreshold <plant> <param> <min> <max>")

    if parts[0].startswith("/"):
        parts = parts[1:]

    # Need at least: <plant_word(s)> <param> <min> <max> → 4 tokens minimum
    if len(parts) < 4:
        raise ValueError("usage: /setthreshold <plant> <param> <min> <max>")

    max_str = parts[-1]
    min_str = parts[-2]
    param = parts[-3].lower()
    plant_name = " ".join(parts[:-3]).strip()

    if not plant_name:
        raise ValueError("usage: /setthreshold <plant> <param> <min> <max>")

    if param not in VALID_PARAMS:
        raise ValueError(
            f"Invalid parameter '{param}'. Must be one of: {', '.join(VALID_PARAMS)}"
        )

    try:
        min_val = float(min_str)
    except ValueError:
        raise ValueError(f"Invalid min value: '{min_str}' — must be a number")

    try:
        max_val = float(max_str)
    except ValueError:
        raise ValueError(f"Invalid max value: '{max_str}' — must be a number")

    if min_val >= max_val:
        raise ValueError(f"Min ({min_val}) must be less than max ({max_val})")

    return plant_name, param, min_val, max_val
