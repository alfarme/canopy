#!/usr/bin/env bash
# On-demand MiFlora battery reads.
#
# Subscribes to canopy/cmd/battery and reads the battery of whichever MAC
# is published there. Ingest publishes to that topic when a sensor reappears
# after RETURN_AFTER_HOURS of silence: dropping out and coming back is the
# classic dying-cell pattern, and by then the stored battery value can be a
# whole day old, so the return is exactly when a fresh reading is worth taking.
#
# Runs on the Pi as a systemd service alongside the daily cron job. Both call
# the same reader (miflora_battery.sh --mac), so there is one implementation of
# the GATT read and one wire format.
set -uo pipefail

BROKER="${MQTT_HOST:-127.0.0.1}"
PORT="${MQTT_PORT:-1883}"
CMD_TOPIC="${CMD_TOPIC:-canopy/cmd/battery}"
READER="${READER:-/usr/local/bin/miflora_battery.sh}"
# Ignore repeat requests for the same sensor inside this window. Ingest fires
# once per return, but a flapping sensor could return repeatedly, and each read
# costs a GATT connect on the one adapter the passive scan is also using.
COOLDOWN_SECS="${COOLDOWN_SECS:-3600}"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

declare -A last_read

log "listening on ${CMD_TOPIC} (broker ${BROKER}:${PORT})"
mosquitto_sub -h "$BROKER" -p "$PORT" -t "$CMD_TOPIC" | while read -r mac; do
    mac=$(echo "$mac" | tr -d '\r' | tr 'a-f' 'A-F')
    if ! [[ "$mac" =~ ^[0-9A-F:]{17}$ ]]; then
        # The topic is a command channel; anything not a MAC is not ours.
        log "ignoring malformed payload: ${mac:0:40}"
        continue
    fi

    now=$(date +%s)
    prev=${last_read[$mac]:-0}
    if [ $((now - prev)) -lt "$COOLDOWN_SECS" ]; then
        log "${mac} requested again within cooldown — skipping"
        continue
    fi
    last_read[$mac]=$now

    log "battery read requested for ${mac}"
    "$READER" --mac "$mac" || log "${mac} read failed — daily job will retry"
done
