# compose/grafana — Grafana Provisioning

## Directory layout

```
compose/grafana/
├── README.md                                  ← this file
└── provisioning/                              ← mounted read-only into the container
    │                                             at /etc/grafana/provisioning:ro
    ├── datasources/
    │   └── timescaledb.yaml                   ← PostgreSQL/TimescaleDB datasource
    └── dashboards/
        ├── dashboard.yaml                     ← dashboard provider config
        └── json/
            └── canopy.json                    ← "Canopy" dashboard definition
```

## Supplying the database password (GF_TS_PASSWORD)

The datasource YAML references `$GF_TS_PASSWORD` — Grafana resolves this env var
at container startup (Grafana OSS 12.x provisioning env-var substitution, documented
at grafana.com/docs/grafana/latest/administration/provisioning/#use-environment-variables).

**`$DATA_DIR/env/grafana.env` must exist** (untracked, outside git — see .env.example)
and is wired into the compose service:

```yaml
# compose/docker-compose.yml (fragment, do not edit automatically)
services:
  grafana:
    image: grafana/grafana-oss:12.4.3
    env_file: ${DATA_DIR}/env/grafana.env          # supplies GF_TS_PASSWORD
    volumes:
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
```

`grafana.env` content (example — use the real password):

```
GF_TS_PASSWORD=<the actual postgres password for user canopy>
```

## Notes

- `sslmode: disable` — correct for container-to-container communication on the internal
  Docker network (cp-db:5432).
- `timescaledb: true` — enables `time_bucket()` support in Grafana's query builder.
- Dashboard default time range is **last 24 hours**; the time-range picker works because
  all queries use `$__timeFilter(time)`.

## UI edits vs. git — read this before touching canopy.json

`allowUiUpdates: true` (see `dashboards/dashboard.yaml`) lets you edit the dashboard live in
Grafana (panel layout, colors, added panels) and those edits persist in Grafana's own DB
(`/var/lib/grafana`, bind-mounted and durable across container recreates).

**But** file-based provisioning reloads a dashboard from disk whenever `canopy.json`'s
content changes — any deploy that intentionally edits this file (e.g. a panel SQL change)
overwrites whatever's live, discarding UI edits made since the last export. `git` is only the
source of truth for the parts we deliberately encode there; the running dashboard can drift
ahead of it.

**Before editing `canopy.json`,** run `./export-dashboard.sh` on the Pi first (reads
`$DATA_DIR/env/grafana.env` directly, single-host deploy) to pull the current
live dashboard back into git, so your diff lands on top of whatever's actually running
instead of clobbering it. Commit the export separately from your intentional change if the
two are easy to tell apart.
