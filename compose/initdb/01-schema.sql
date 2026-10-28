-- Canopy schema — see docs/technical.md
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE sensors (
    id               SERIAL PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN ('bt')),
    mac_or_device_id TEXT UNIQUE NOT NULL,
    name             TEXT,
    location         TEXT,
    collector        TEXT,
    first_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen        TIMESTAMPTZ,
    active           BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE plants (
    id               SERIAL PRIMARY KEY,
    display_name     TEXT NOT NULL,
    species          TEXT,
    opb_pid          TEXT,
    gbif_id          TEXT,
    threshold_source TEXT NOT NULL DEFAULT 'default',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE plant_assignments (
    id          SERIAL PRIMARY KEY,
    sensor_id   INTEGER NOT NULL REFERENCES sensors(id),
    plant_id    INTEGER NOT NULL REFERENCES plants(id),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX one_active_assignment_per_sensor
    ON plant_assignments (sensor_id) WHERE released_at IS NULL;

CREATE TABLE thresholds (
    plant_id           INTEGER PRIMARY KEY REFERENCES plants(id),
    min_soil_moist     INTEGER,
    max_soil_moist     INTEGER,
    min_soil_ec        INTEGER,
    max_soil_ec        INTEGER,
    min_light_lux      INTEGER,
    max_light_lux      INTEGER,
    min_temp           INTEGER,
    max_temp           INTEGER,
    hysteresis_moist   INTEGER NOT NULL DEFAULT 5,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE bt_readings (
    time               TIMESTAMPTZ NOT NULL,
    sensor_id          INTEGER NOT NULL REFERENCES sensors(id),
    moisture           REAL,
    temp_c             REAL,
    light_lux          INTEGER,
    conductivity_us_cm INTEGER,
    battery            INTEGER,
    rssi               INTEGER
);
SELECT create_hypertable('bt_readings', by_range('time'));
-- Required by ingest's INSERT ... ON CONFLICT (time, sensor_id) upsert.
-- A hypertable's unique index must include the partitioning column (time).
CREATE UNIQUE INDEX bt_readings_time_sensor_uidx ON bt_readings (time, sensor_id);
ALTER TABLE bt_readings SET (
    timescaledb.enable_columnstore = true,
    timescaledb.segmentby = 'sensor_id',
    timescaledb.orderby = 'time DESC');
CALL add_columnstore_policy('bt_readings', after => INTERVAL '7 days');

-- 1h continuous aggregates for dashboards
CREATE MATERIALIZED VIEW bt_readings_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', time) AS hour, sensor_id,
       avg(moisture) AS avg_moisture, min(moisture) AS min_moisture, max(moisture) AS max_moisture,
       avg(temp_c) AS avg_temp, avg(light_lux) AS avg_lux,
       avg(conductivity_us_cm) AS avg_ec, min(battery) AS min_battery,
       avg(rssi) AS avg_rssi, count(*) AS samples
FROM bt_readings GROUP BY hour, sensor_id;
SELECT add_continuous_aggregate_policy('bt_readings_hourly',
    start_offset => INTERVAL '3 hours', end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour');

-- alert events emitted by decision-engine, consumed by telegram-bot
CREATE TABLE alert_events (
    id         SERIAL PRIMARY KEY,
    time       TIMESTAMPTZ NOT NULL DEFAULT now(),
    sensor_id  INTEGER REFERENCES sensors(id),
    plant_id   INTEGER REFERENCES plants(id),
    kind       TEXT NOT NULL,  -- water_needed|stale|stuck|low_battery|new_sensor
                               -- range kinds: frost_risk|heat_stress|feed_needed|
                               --   salt_buildup|soil_not_draining|light_low|light_high
                               -- out_of_range is legacy: it preceded the split above and
                               -- still appears in old rows and in alert_silences
    message    TEXT NOT NULL,
    delivered  BOOLEAN NOT NULL DEFAULT false
);

-- permanent per-(sensor, kind) alert mutes, managed via the telegram bot's
-- /silence and /unsilence commands
CREATE TABLE alert_silences (
    sensor_id    INTEGER NOT NULL REFERENCES sensors(id),
    kind         TEXT NOT NULL,
    silenced_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (sensor_id, kind)
);

-- Photos taken during /identify, kept for a future training/dataset feature.
-- Rows are inserted at identification time (sensor_id/plant_name still NULL)
-- and updated once the user completes the assign flow; left NULL if they
-- cancel. sensor_name/plant_name are snapshots (not just FKs) so the dataset
-- stays meaningful even if a sensor is later renamed or removed.
CREATE TABLE identify_photos (
    id           SERIAL PRIMARY KEY,
    taken_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    file_path    TEXT NOT NULL,
    species      TEXT,
    score        REAL,
    sensor_id    INTEGER REFERENCES sensors(id),
    sensor_name  TEXT,
    plant_name   TEXT,
    plant_id     INTEGER REFERENCES plants(id)
);

-- View providing human-friendly sensor labels: "Plant Name (sensor-name)" if
-- assigned, else just the sensor name or MAC.
CREATE OR REPLACE VIEW sensor_labels AS
SELECT s.id AS sensor_id,
       COALESCE(p.display_name || ' (' || s.name || ')', s.name, s.mac_or_device_id) AS label
FROM sensors s
LEFT JOIN plant_assignments pa ON pa.sensor_id = s.id AND pa.released_at IS NULL
LEFT JOIN plants p ON p.id = pa.plant_id;
