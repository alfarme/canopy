# decision-engine

Periodic service that reads sensor data from the DB, evaluates thresholds with
hysteresis, and writes deduplicated `alert_events` rows consumed by telegram-bot.

## What it does

Every `INTERVAL_SECONDS` (default 300 s):

1. Load active sensors, plant assignments, and thresholds from Postgres.
2. Fetch the most recent bt_readings for each sensor.
3. Run pure-function evaluation (`app/engine.py`):
   - **water_needed** — moisture < `min_soil_moist`; clears only when moisture
     rises above `min_soil_moist + hysteresis_moist` (prevents flapping).
   - **stale** — no reading in > `STALE_HOURS` hours.
   - **stuck** — last N moisture readings are all identical (sensor frozen).
   - **out_of_range** — temp / lux / EC outside threshold min/max.
   - **low_battery** — battery % < `BATTERY_LOW_PCT`.
4. Insert `alert_events` rows only when a condition *transitions to active*
   (deduplication via in-memory `active_alerts` set across cycles).

## Environment variables

| Variable          | Default                    | Description                              |
|-------------------|----------------------------|------------------------------------------|
| `DATABASE_URL`    | —                          | Full DSN; wins over discrete vars        |
| `PGHOST`          | `localhost`                | Postgres host                            |
| `PGPORT`          | `5432`                     | Postgres port                            |
| `PGUSER`          | `postgres`                 | Postgres user                            |
| `PGPASSWORD`      | *(empty)*                  | Postgres password                        |
| `PGDATABASE`      | `canopy`                   | Postgres database name                   |
| `INTERVAL_SECONDS`| `300`                      | Evaluation period (seconds)              |
| `STALE_HOURS`     | `24`                       | Hours without reading → stale alert      |
| `STUCK_WINDOW`    | `5`                        | Recent readings window for stuck check   |
| `BATTERY_LOW_PCT` | `15`                       | Battery % threshold for low_battery      |
| `LOG_LEVEL`       | `INFO`                     | Logging level (DEBUG/INFO/WARNING/ERROR) |

## Running tests

```bash
cd services/decision-engine
pip install psycopg[binary]==3.2.9 pytest
pytest tests/ -v
```

## Running locally

```bash
cd services/decision-engine
cp .env.example .env
# edit .env with real DB credentials
export $(grep -v '^#' .env | xargs)
python -m app
```
