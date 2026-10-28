"""
Message formatting for Telegram alerts and command responses.
Pure functions — no network or DB dependency.
"""
from __future__ import annotations
from datetime import datetime, timezone
from html import escape
from typing import Any


def _esc(value: Any) -> str:
    """HTML-escape a value for safe interpolation into parse_mode=HTML text.

    Plant/species/sensor names and locations are user-controlled; without this
    a name like 'Aloe & vera' or '<i>x</i>' breaks Telegram's HTML parser.
    """
    return escape(str(value))

# Alert kind → emoji
_KIND_EMOJI = {
    "water_needed": "💧",
    "low_battery":  "🔋",
    "stale":        "🕐",
    "stuck":        "⚠️",
    "out_of_range": "🌡️",   # legacy — kept so old alert_events still render
    "new_sensor":   "🌱",
    # Range conditions, split out of out_of_range so each can be silenced,
    # titled and timed on its own.
    "frost_risk":        "🧊",
    "heat_stress":       "🔥",
    "feed_needed":       "🌾",
    "salt_buildup":      "🧂",
    "soil_not_draining": "🪣",
    "light_low":         "🌑",
    "light_high":        "🔆",
}

# Headline per kind. Without this the title is derived as
# kind.replace("_", " ").title(), which produced "Out Of Range" — a
# measurement, not a problem anyone can act on. Each of these names the action.
_KIND_TITLE = {
    "water_needed":      "Water needed",
    "low_battery":       "Battery low",
    "stale":             "Sensor silent",
    "stuck":             "Stuck sensor value",
    "new_sensor":        "New sensor",
    "out_of_range":      "Out of range",
    "frost_risk":        "Frost risk",
    "heat_stress":       "Too hot",
    "feed_needed":       "Fertiliser needed",
    "salt_buildup":      "Salt build-up — flush the soil",
    "soil_not_draining": "Soil not draining",
    "light_low":         "Not enough light",
    "light_high":        "Too much light",
}


def _title(kind: str) -> str:
    return _KIND_TITLE.get(kind, kind.replace("_", " ").title())


def _emoji(kind: str) -> str:
    return _KIND_EMOJI.get(kind, "🔔")


def format_alert(
    *,
    kind: str,
    message: str,
    sensor_name: str | None,
    plant_name: str | None,
    alert_id: int,
) -> str:
    """Format an alert_events row into a human-readable Telegram message."""
    emoji = _emoji(kind)
    lines = [f"{emoji} <b>{_esc(_title(kind))}</b>"]
    if plant_name:
        lines.append(f"🌿 Plant: {_esc(plant_name)}")
    if sensor_name:
        lines.append(f"📡 Sensor: {_esc(sensor_name)}")
    lines.append(_esc(message))
    if kind == "new_sensor":
        # The alert is the only place the MAC is surfaced, and the sensor stays
        # unlabelled until someone acts on it — so carry the next step with it.
        lines.append("Number it with /addsensor MAC NUMBER.")
    lines.append(f"<i>alert #{alert_id}</i>")
    return "\n".join(lines)


def format_plants_list(plants: list[dict[str, Any]]) -> str:
    """Format the list of plants for /plants command."""
    if not plants:
        return "🌵 No plants registered yet.\nUse /assign &lt;sensor&gt; &lt;plant&gt; to get started."

    lines = ["🌿 <b>Plants</b>"]
    for p in plants:
        name = _esc(p["display_name"])
        species = _esc(p.get("species") or "—")
        sensor = _esc(p.get("sensor_name") or "unassigned")
        location = _esc(p.get("location") or "—")
        lines.append(
            f"\n• <b>{name}</b> (<i>{species}</i>)\n"
            f"  📡 {sensor}  |  📍 {location}"
        )
    return "\n".join(lines)


def _fmt_val(value: Any, unit: str = "", precision: int = 1) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{precision}f}{unit}"
    return f"{value}{unit}"


def _fmt_time(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    return ts.strftime("%Y-%m-%d %H:%M UTC")


def format_status(readings: list[dict[str, Any]]) -> str:
    """Format sensor/plant status for /status command."""
    if not readings:
        return "📊 No sensor readings available yet."

    lines = ["📊 <b>Status</b>"]
    for r in readings:
        sensor = _esc(r.get("sensor_name") or "?")
        plant = _esc(r.get("plant_name") or "unassigned")
        moisture = _fmt_val(r.get("moisture"), "%")
        temp = _fmt_val(r.get("temp_c"), "°C")
        lux = _fmt_val(r.get("light_lux"), " lux", precision=0)
        ec = _fmt_val(r.get("conductivity_us_cm"), " µS/cm", precision=0)
        battery = _fmt_val(r.get("battery"), "%", precision=0)
        last_seen = _fmt_time(r.get("last_seen"))

        lines.append(
            f"\n📡 <b>{sensor}</b> → 🌿 {plant}\n"
            f"  💧 {moisture}  🌡️ {temp}  ☀️ {lux}\n"
            f"  ⚡ {ec}  🔋 {battery}  🕐 {last_seen}"
        )
    return "\n".join(lines)


def format_assign_success(*, sensor_name: str, plant_name: str) -> str:
    """Confirmation message after successful /assign."""
    return (
        f"✅ Assigned!\n"
        f"📡 Sensor <b>{_esc(sensor_name)}</b> → 🌿 <b>{_esc(plant_name)}</b>"
    )


def format_assign_error(error_code: str, **context: str) -> str:
    """Error message for failed /assign."""
    if error_code == "sensor_not_found":
        ref = _esc(context.get("sensor_ref", ""))
        return f"❌ Sensor <code>{ref}</code> not found.\nUse /status to see available sensors."
    if error_code == "plant_not_found":
        ref = _esc(context.get("plant_ref", ""))
        return f"❌ Plant <b>{ref}</b> not found.\nUse /plants to see registered plants."
    if error_code == "usage":
        return "❌ Usage: /assign &lt;sensor&gt; &lt;plant name&gt;"
    return "❌ Assignment failed. Please try again later."


def format_battery(battery: Any) -> str:
    """Render a battery percentage as "🔋 42%", or "" when unknown.

    Unknown is common and not a fault: a BT sensor only gets a battery reading
    when the Pi's daily active-GATT job manages to connect to it, and roughly
    half of those connects fail on any given attempt. An empty string keeps the
    line clean rather than advertising a gap.
    """
    if battery is None:
        return ""
    try:
        pct = int(battery)
    except (TypeError, ValueError):
        return ""
    # The cells in service are old stock and sag hard under the load of a GATT
    # connect, so a single low sample is weak evidence — icon only, no alarm.
    icon = "🪫" if pct < 25 else "🔋"
    return f"{icon} {pct}%"


def format_sensors_list(sensors: list[dict[str, Any]]) -> str:
    """Format sensor list for /sensors command."""
    if not sensors:
        return "📡 No active sensors found."

    lines = ["📡 <b>Sensors</b>"]
    for s in sensors:
        name = _esc(s.get("name") or s.get("mac_or_device_id") or "?")
        location = _esc(s.get("location") or "—")
        plant = _esc(s.get("plant_name") or "unassigned")
        last_seen = _fmt_time(s.get("last_seen"))
        lines.append(
            f"\n• <b>{name}</b>  {format_battery(s.get('battery'))}\n"
            f"  📍 {location}  |  🌿 {plant}  |  🕐 {last_seen}"
        )
    return "\n".join(lines)


def format_silences(silences: list[dict[str, Any]]) -> str:
    """Format the silenced-alerts list for /silences command."""
    if not silences:
        return "🔇 No alerts are currently silenced."

    lines = ["🔇 <b>Silenced alerts</b>"]
    for s in silences:
        sensor = _esc(s.get("sensor_name") or "?")
        kind = _esc(s.get("kind") or "?")
        since = _fmt_time(s.get("silenced_at"))
        lines.append(f"\n• <b>{sensor}</b> — {kind}  |  since {since}")
    return "\n".join(lines)


def format_thresholds(plant_name: str, thresholds: dict[str, Any]) -> str:
    """Format thresholds for /thresholds command."""
    lines = [f"🎯 <b>Thresholds for {_esc(plant_name)}</b>"]
    lines.append(
        f"\n  💧 Moisture: {_fmt_val(thresholds.get('min_soil_moist'))}–{_fmt_val(thresholds.get('max_soil_moist'))}%"
    )
    lines.append(
        f"  ⚡ EC: {_fmt_val(thresholds.get('min_soil_ec'))}–{_fmt_val(thresholds.get('max_soil_ec'))} µS/cm"
    )
    lines.append(
        f"  ☀️ Light: {_fmt_val(thresholds.get('min_light_lux'))}–{_fmt_val(thresholds.get('max_light_lux'))} lux"
    )
    lines.append(
        f"  🌡️ Temp: {_fmt_val(thresholds.get('min_temp'))}–{_fmt_val(thresholds.get('max_temp'))} °C"
    )
    hyst = thresholds.get("hysteresis_moist")
    if hyst is not None:
        lines.append(f"  📐 Hysteresis (moisture): {hyst}%")
    source = _esc(thresholds.get("threshold_source") or "—")
    lines.append(f"\n  Source: <i>{source}</i>")
    return "\n".join(lines)


def format_identify_results(results: list[dict[str, Any]]) -> str:
    """Format PlantNet identification results (top 3)."""
    if not results:
        return "🔍 No matches found. Try with a clearer photo of a leaf or flower."

    lines = ["🔍 <b>Plant identification</b>"]
    for i, r in enumerate(results[:3], 1):
        name = _esc(r.get("scientific_name", "Unknown"))
        score = r.get("score", 0) * 100
        common = r.get("common_names", [])
        common_str = f" — {_esc(common[0])}" if common else ""
        lines.append(f"  {i}. <b>{name}</b> ({score:.1f}%){common_str}")

    lines.append("\n<i>Powered by Pl@ntNet</i>")
    return "\n".join(lines)


def format_help() -> str:
    """Format /help command output."""
    return (
        "🌱 <b>Canopy Bot Commands</b>\n\n"
        "/addsensor MAC NUMBER — Number a newly discovered sensor\n"
        "/assign &lt;sensor&gt; &lt;plant/species&gt; — Assign sensor to plant\n"
        "/unassign &lt;sensor&gt; — Release sensor assignment\n"
        "/delete &lt;sensor_id&gt; — Delete the plant assigned to that sensor\n"
        "/rename &lt;sensor&gt; &lt;new name&gt; — Rename the assigned plant\n"
        "/setlocation &lt;sensor&gt; &lt;location&gt; — Set sensor location\n"
        "/plants — List all plants\n"
        "/sensors — List all active sensors\n"
        "/status [sensor] — Show latest readings (all, or one sensor)\n"
        "/thresholds &lt;plant&gt; — Show thresholds\n"
        "/setthreshold &lt;plant&gt; &lt;param&gt; &lt;min&gt; &lt;max&gt; — Override threshold\n"
        "/silence &lt;sensor&gt; [kind] — Permanently mute alerts; no kind = all of them\n"
        "/unsilence &lt;sensor&gt; [kind] — Re-enable silenced alerts; no kind = all\n"
        "/silences — List currently silenced alerts\n"
        "/removesensor &lt;sensor&gt; — Delete a sensor's data and retire it (asks to confirm)\n"
        "/menu — Button menu instead of typing commands\n"
        "/help — This message\n\n"
        "📷 <i>Send any photo — it's identified automatically.</i>\n\n"
        "<i>Write commands (addsensor, assign, unassign, delete, rename, setlocation, "
        "setthreshold, silence, unsilence, removesensor) and photo uploads are admin-only.</i>"
    )
