#!/usr/bin/env bash
# Canopy deploy — single Raspberry Pi, native arm64 build, no registry.
# Run directly on the Pi.
#
# Usage:
#   ./deploy.sh                       # build all services, recreate
#   ./deploy.sh ingest telegram-bot   # same, but only these services
#   ./deploy.sh --no-recreate ...     # build only, skip up -d
#
# Rollback is `git checkout <sha> -- .` followed by a rebuild — there is no
# registry to retag against, so there is no --to flag any more.
#
# Before every recreate the currently-running image IDs are snapshotted into
# $DATA_DIR/deploy-history/.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "ERROR: .env not found. Copy .env.example -> .env and set DATA_DIR (see INSTALL.md)." >&2
    exit 1
fi
set -a
# shellcheck disable=SC1091
source .env
set +a
: "${DATA_DIR:?DATA_DIR must be set in .env}"

COMPOSE_FILE="compose/docker-compose.yml"
ALL_SERVICES="ingest decision-engine telegram-bot canopy-ui"
HIST="$DATA_DIR/deploy-history"

# service -> container name, for the pre-deploy snapshot
container_of() {
    case "$1" in
        ingest) echo cp-ingest ;;
        decision-engine) echo cp-decision ;;
        telegram-bot) echo cp-telegram ;;
        canopy-ui) echo cp-canopy-ui ;;
        *) echo "" ;;
    esac
}

RECREATE=1
POS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --no-recreate) RECREATE=0 ;;
        -*) echo "unknown flag: $1" >&2; exit 2 ;;
        *) POS="$POS $1" ;;
    esac
    shift
done
SERVICES="${POS:-$ALL_SERVICES}"

for svc in $SERVICES; do
    [ -d "services/$svc" ] || { echo "ERROR: no services/$svc" >&2; exit 1; }
done

echo "Building: $SERVICES"
docker compose -f "$COMPOSE_FILE" build $SERVICES

if [ "$RECREATE" = "0" ]; then
    echo "Skipping recreate (--no-recreate)."
    exit 0
fi

mkdir -p "$HIST"
TS="$(date +%Y%m%d-%H%M%S)"
snap="$HIST/$TS.txt"
{
    echo "# deploy $TS"
    echo "# pre-deploy running images (service = image_id):"
    for svc in $ALL_SERVICES; do
        c="$(container_of "$svc")"
        printf '%s = %s\n' "$c" "$(docker inspect "$c" --format '{{.Image}}' 2>/dev/null || echo MISSING)"
    done
} > "$snap"
echo "snapshot: $snap"

echo "Recreating..."
docker compose -f "$COMPOSE_FILE" up -d

# Grafana config is bind-mounted, so `up -d` won't restart it when only
# provisioning files changed. Restart it explicitly so synced
# dashboards/datasources/alerting rules take effect.
docker restart cp-grafana >/dev/null 2>&1 && echo "cp-grafana reloaded" || echo "cp-grafana restart skipped"

sleep 5
echo "=== post-deploy status ==="
bad=0
for c in cp-mosquitto cp-db cp-ingest cp-decision cp-telegram cp-canopy-ui cp-grafana cp-theengs; do
    st=$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null || echo missing)
    echo "  $c: $st"
    [ "$st" = "running" ] || bad=1
done
if [ "$bad" = "0" ]; then
    cp "$snap" "$HIST/last-good.txt"
    echo "DEPLOY OK — last-good.txt updated"
else
    echo "WARNING: a service is not running — investigate; rollback is 'git checkout <sha> -- .' + rebuild." >&2
    exit 1
fi
