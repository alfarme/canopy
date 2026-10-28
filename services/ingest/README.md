# ingest — BLE sensor ingest service

Subscribes to `canopy/ble/#` (Theengs Gateway output), decodes
HHCCJCY01 JSON payloads, merges per-MAC partial readings into one
`bt_readings` row per sensor per time bucket, and writes to TimescaleDB.
Auto-registers unknown MAC addresses as new sensors and emits an
`alert_events(kind='new_sensor')` row for the telegram-bot.

## How it works

The HHCCJCY01 broadcasts ONE metric per rotating BLE advertisement
(`tempc`, `moi`, `lux`, or `fer`). Theengs Gateway publishes each
decoded advertisement as a partial JSON payload. Ingest accumulates
partials in memory, keyed by MAC + bucket, and flushes the merged row
when the bucket rolls over or on shutdown.

Field mapping (Theengs → `bt_readings`):

| Theengs field | DB column            | Type  |
|---------------|----------------------|-------|
| `tempc`       | `temp_c`             | float |
| `moi`         | `moisture`           | float |
| `lux`         | `light_lux`          | int   |
| `fer`         | `conductivity_us_cm` | int   |
| `rssi`        | `rssi`               | int   |
| `batt`        | `battery`            | int   |

`batt` never arrives on the passive MiFlora path (the HHCCJCY01HHCC decoder
emits only temperature/moisture/fertility/luminance; battery lives behind GATT
handle `0x38`). It is mapped for an active-connect collector publishing to the
same topic — see `docs/technical.md` §5.

## Environment variables

| Variable        | Default       | Description                              |
|-----------------|---------------|------------------------------------------|
| `DATABASE_URL`  | —             | Full PostgreSQL URL (overrides PG* vars) |
| `PGHOST`        | `localhost`   | PostgreSQL host                          |
| `PGPORT`        | `5432`        | PostgreSQL port                          |
| `PGUSER`        | `postgres`    | PostgreSQL user                          |
| `PGPASSWORD`    | —             | PostgreSQL password                      |
| `PGDATABASE`    | `canopy`      | PostgreSQL database name                 |
| `MQTT_HOST`     | `localhost`   | MQTT broker host                         |
| `MQTT_PORT`     | `1883`        | MQTT broker port                         |
| `MQTT_USER`     | —             | MQTT username (optional)                 |
| `MQTT_PASSWORD` | —             | MQTT password (optional)                 |
| `BUCKET_MINUTES`| `30`          | Bucket size for merging partials (min)   |
| `LOG_LEVEL`     | `INFO`        | Logging level (DEBUG/INFO/WARNING/ERROR) |

## Running tests

```bash
cd services/ingest
pip install -r requirements.txt
pytest tests/ -v
```

## Running the service

```bash
# Docker
docker build -t canopy-ingest .
docker run --env-file .env canopy-ingest

# Local
pip install -r requirements.txt
python -m app
```
