#!/usr/bin/env bash
# MiFlora battery collector — active GATT read, published to the normal BLE topic.
#
# Runs on the Pi (flowerpi), NOT in the compose stack: it needs the Bluetooth
# adapter. See docs/technical.md section 5 for why this exists at all —
# short version, battery sits behind GATT handle 0x38 and the passive scan that
# feeds everything else can never see it.
#
# Deployed to /usr/local/bin/miflora_battery.sh, run every 48 hours by cron
# (`0 12 */2 * *`). History: daily at 12:00 until 2026-07-29, then two-hourly
# for trend resolution, six-hourly from 2026-07-30, then 48-hourly from
# 2026-09-09 — the active GATT connect is the only operation in the whole
# system that costs a sensor any battery, and a discharge curve does not need
# four points a day to stay readable.
#
# Cron cannot express "every 48 hours": `*/2` steps the day-of-month, so a
# 31-day month runs on the 31st and again on the 1st — one 24h gap about seven
# times a year. That extra read is harmless; a systemd timer to avoid it would
# be more machinery than the problem deserves.
#
# CHANGING THIS CADENCE MEANS CHECKING `fp_low_battery`. Its BT branch confirms
# over a 14d window with a 2-sample floor, and both numbers are derived from
# how many reads this cron attempts (roughly half of them fail). The floor is a
# row count, so slowing collection without widening the window makes it
# unreachable and the rule goes silently dark — that has now happened twice.
#
#   Sensor discovery is done by listening to the MQTT stream rather than by
#   querying the database: it needs no credentials on the Pi, and a sensor added
#   via /addsensor is picked up automatically the next run. A sensor that stays
#   silent through the discovery window is simply skipped this cycle.
#
#   Reads are serialized on purpose. The Pi has one adapter (hci0) shared with
#   the Theengs passive scan; connecting one device at a time is what keeps the
#   reading stream undisturbed.
set -uo pipefail

BROKER="${MQTT_HOST:-127.0.0.1}"
PORT="${MQTT_PORT:-1883}"
TOPIC_ROOT="${TOPIC_ROOT:-canopy/ble}"
DISCOVER_SECS="${DISCOVER_SECS:-180}"
RETRIES="${RETRIES:-4}"
READ_TIMEOUT="${READ_TIMEOUT:-20}"
HANDLE="0x38"
MODEL="HHCCJCY01HHCC"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

# read_one <mac> -> publishes and logs; returns 0 on success, 1 otherwise.
read_one() {
    local mac="$1" value="" fw="" out try
    for try in $(seq 1 "$RETRIES"); do
        out=$($SUDO timeout "$READ_TIMEOUT" gatttool -b "$mac" --char-read -a "$HANDLE" 2>&1 | tail -1)
        case "$out" in
            *"Characteristic value"*)
                # "64 38 33 2e 33 2e 35" -> battery 0x64=100%, byte1 undocumented,
                # bytes 2-6 ASCII firmware ("3.3.5").
                read -r -a b <<< "${out##*: }"
                value=$((16#${b[0]}))
                for i in 2 3 4 5 6; do
                    [ -n "${b[$i]:-}" ] && fw+=$(printf "\\x${b[$i]}")
                done
                break
                ;;
        esac
        sleep 2
    done

    if [ -z "$value" ]; then
        # Connects fail transiently and often, uncorrelated with signal
        # strength. Not an error worth alerting on — try again next cycle.
        log "  ${mac}  unreachable after ${RETRIES} tries"
        return 1
    fi
    if [ "$value" -lt 0 ] || [ "$value" -gt 100 ]; then
        log "  ${mac}  implausible battery ${value} — not publishing"
        return 1
    fi

    # `batt` is the Theengs wire key that ingest's FIELD_MAP expects; publishing
    # `battery` would be silently dropped as an unknown field.
    if mosquitto_pub -h "$BROKER" -p "$PORT" \
           -t "${TOPIC_ROOT}/${mac}" \
           -m "{\"id\":\"${mac}\",\"model_id\":\"${MODEL}\",\"batt\":${value}}"; then
        log "  ${mac}  ${value}%  fw ${fw:-?}  (try ${try})"
        return 0
    fi
    log "  ${mac}  ${value}% but publish failed"
    return 1
}

# Single-sensor mode, used by the listener when ingest reports a sensor has
# come back after a long silence.
if [ "${1:-}" = "--mac" ] && [ -n "${2:-}" ]; then
    log "on-demand battery read for ${2}"
    read_one "$(echo "$2" | tr 'a-f' 'A-F')"
    exit $?
fi

log "discovering ${MODEL} sensors for ${DISCOVER_SECS}s on ${TOPIC_ROOT}/#"
macs=$(mosquitto_sub -h "$BROKER" -p "$PORT" -t "${TOPIC_ROOT}/#" -W "$DISCOVER_SECS" 2>/dev/null \
       | grep "$MODEL" \
       | sed -n 's/.*"id": *"\([0-9A-Fa-f:]\{17\}\)".*/\1/p' \
       | tr 'a-f' 'A-F' | sort -u)

count=$(printf '%s\n' "$macs" | grep -c ':' || true)
if [ "$count" -eq 0 ]; then
    log "no sensors discovered — is the gateway running? aborting"
    exit 1
fi
log "discovered ${count} sensor(s)"

ok=0; fail=0
for mac in $macs; do
    if read_one "$mac"; then ok=$((ok + 1)); else fail=$((fail + 1)); fi
done

log "done — ${ok} read, ${fail} failed, ${count} total"
