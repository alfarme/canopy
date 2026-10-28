#!/usr/bin/env bash
# Daily Postgres backup. Install as a host crontab entry, e.g.:
#   0 3 * * * DATA_DIR=$HOME/canopy-data $HOME/canopy/tools/backup_db.sh >> $HOME/canopy-data/backups/backup.log 2>&1
#
# Always a full dump/restore (never schema-only + data-only separately —
# hypertable metadata ordering matters, see docs/technical.md). Runs pg_dump
# inside the cp-db container so it needs no local psql client on the host;
# /backups is already bind-mounted to $DATA_DIR/backups (see
# compose/docker-compose.yml), so the dump lands directly on the host.
set -euo pipefail

: "${DATA_DIR:?set DATA_DIR}"
RETAIN_DAYS=14
OUT="$DATA_DIR/backups/canopy-$(date +%F).dump"

docker exec cp-db pg_dump -U canopy -Fc canopy -f "/backups/$(basename "$OUT")"

# Verify the dump is non-empty before pruning anything older.
if [ ! -s "$OUT" ]; then
    echo "ERROR: backup $OUT is empty or missing — not pruning old backups" >&2
    exit 1
fi
echo "backup ok: $OUT ($(du -h "$OUT" | cut -f1))"

find "$DATA_DIR/backups" -maxdepth 1 -name 'canopy-*.dump' -mtime "+${RETAIN_DAYS}" -print -delete
