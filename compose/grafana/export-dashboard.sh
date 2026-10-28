#!/usr/bin/env bash
# Pull the live "Canopy" dashboard JSON back from Grafana on the Pi into
# git, so UI edits (panel layout, colors, added panels, etc.) become the new
# source of truth instead of being silently discarded on the next deploy that
# touches compose/grafana/provisioning/dashboards/json/canopy.json.
#
# Grafana's file-provisioning reloads a dashboard from disk whenever the JSON
# content changes (allowUiUpdates: true only stops the "not editable" banner,
# it does NOT protect UI edits from a file-driven reload). Run this BEFORE
# editing canopy.json (e.g. for a panel-SQL change), then `git diff` the
# result: Grafana always re-serializes panels with full default fields filled
# in (options, overrides, etc.), so expect a one-time reformat noise diff even
# with no real user edits — review for actual content changes, not formatting.
#
# Run directly on the Pi (single-host deploy).
set -euo pipefail

DATA_DIR="${DATA_DIR:?set DATA_DIR (see .env)}"
ENV_FILE="$DATA_DIR/env/grafana.env"
OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/provisioning/dashboards/json/canopy.json"
UID_="canopy"

PW=$(grep GF_SECURITY_ADMIN_PASSWORD "$ENV_FILE" | cut -d= -f2-)
curl -s -u "admin:$PW" "http://localhost:${GRAFANA_PORT:-3000}/api/dashboards/uid/$UID_" | python3 -c "
import json, sys
dashboard = json.load(sys.stdin)['dashboard']
dashboard.pop('id', None)
dashboard.pop('version', None)
json.dump(dashboard, sys.stdout, indent=2, sort_keys=True)
print()
" > "$OUT"

echo "Exported live dashboard -> $OUT"
echo "Review before committing (expect reformat noise; look for real content changes):"
echo "  git diff -- $OUT"
