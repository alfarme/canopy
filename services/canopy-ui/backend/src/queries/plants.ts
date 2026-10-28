/**
 * Plant queries — getPlants (tile list) and getPlant (detail).
 * SQL correctness is the #1 priority.
 */

import { pool } from "../db.js";
import { METRIC_REGISTRY } from "../metrics.js";
import { dominantColor } from "../photo-processing.js";
import { photoUuidFromPath, resolvePhotoPath } from "../photo-paths.js";
import type {
  MetricReading,
  MetricHistory,
  TilePayload,
  TilePhoto,
  PlantDetail,
  PlantDetailPhoto,
} from "../types.js";

// ─── Photo helpers ──────────────────────────────────────────────────────────

const DOMINANT_COLOR_FALLBACK = "#8a8a7a";
const BLURHASH_PLACEHOLDER = "";

async function buildTilePhoto(filePath: string): Promise<TilePhoto> {
  const uuid = photoUuidFromPath(filePath);
  let color = DOMINANT_COLOR_FALLBACK;
  try {
    color = await dominantColor(uuid, resolvePhotoPath(filePath));
  } catch {
    // photo file missing/unreadable — keep the fallback, tile still renders
  }
  return {
    uuid,
    dominantColor: color,
    blurhash: BLURHASH_PLACEHOLDER,
    urls: {
      tile: `/photos/${uuid}/tile`,
      hero: `/photos/${uuid}/hero`,
    },
  };
}

// ─── Metric building ────────────────────────────────────────────────────────

interface ThresholdRow {
  min_soil_moist: number | null;
  max_soil_moist: number | null;
  min_soil_ec: number | null;
  max_soil_ec: number | null;
  min_light_lux: number | null;
  max_light_lux: number | null;
  min_temp: number | null;
  max_temp: number | null;
}

interface BtReadingRow {
  sensor_id: number;
  moisture: number | null;
  temp_c: number | null;
  light_lux: number | null;
  conductivity_us_cm: number | null;
}

function getThresholdValue(
  thresholds: ThresholdRow,
  key: string
): number | null {
  switch (key) {
    case "min_soil_moist":
      return thresholds.min_soil_moist;
    case "max_soil_moist":
      return thresholds.max_soil_moist;
    case "min_soil_ec":
      return thresholds.min_soil_ec;
    case "max_soil_ec":
      return thresholds.max_soil_ec;
    case "min_light_lux":
      return thresholds.min_light_lux;
    case "max_light_lux":
      return thresholds.max_light_lux;
    case "min_temp":
      return thresholds.min_temp;
    case "max_temp":
      return thresholds.max_temp;
    default:
      return null;
  }
}

function getBtReadingValue(
  reading: BtReadingRow,
  col: string
): number | null {
  switch (col) {
    case "moisture":
      return reading.moisture;
    case "temp_c":
      return reading.temp_c;
    case "light_lux":
      return reading.light_lux;
    case "conductivity_us_cm":
      return reading.conductivity_us_cm;
    default:
      return null;
  }
}

function buildMetrics(
  sensorId: number,
  thresholds: ThresholdRow | null,
  btReadings: Map<number, BtReadingRow>,
  sustainedLightSensors: Set<number>,
  sustainedWetSensors: Set<number>,
  sustainedEcSensors: Set<number>
): MetricReading[] {
  if (!thresholds) return [];

  const result: MetricReading[] = [];

  for (const [metricId, def] of Object.entries(METRIC_REGISTRY)) {
    const min = getThresholdValue(thresholds, def.thresholdMin);
    const max = getThresholdValue(thresholds, def.thresholdMax);

    // Both min AND max must be non-null for the metric to appear
    if (min == null || max == null) continue;

    if (!def.btCol) continue;
    let value: number | null = null;
    const reading = btReadings.get(sensorId);
    if (reading) {
      value = getBtReadingValue(reading, def.btCol);
    }

    // A reading must exist for the metric to appear
    if (value == null) continue;

    // Three metrics do not read as out-of-range from a single sample. Each
    // rule is ported from the decision engine so the board and the alerts
    // agree; Canopy re-implemented these comparisons instantaneously and had
    // inherited only the light one, so it contradicted its own alerts.
    //
    //   light        — only after 3 straight days OOR (sustainedLightOOR);
    //                  instantaneous lux is meaningless at night.
    //   moisture>max — only when the soil stays soaked (sustainedWetOOR).
    //                  Watering puts a plant above max for hours by design,
    //                  and flagging that turns correct care into an alert.
    //   conductivity — 24h mean, low side gated on moisture (sustainedEcOOR).
    //                  EC dips after watering and collapses in dry soil.
    //
    // Moisture BELOW min stays instantaneous: dry is dry, there is no benign
    // reason for it, and acting late is the failure mode that matters. Temp
    // is left instantaneous here deliberately — the engine's own frost rule
    // is only a 30-minute window, so a single cold sample is close enough to
    // the truth to be worth showing.
    let outOfRange: boolean;
    if (metricId === "light") {
      outOfRange = sustainedLightSensors.has(sensorId);
    } else if (metricId === "moisture") {
      outOfRange = value < min || (value > max && sustainedWetSensors.has(sensorId));
    } else if (metricId === "conductivity") {
      outOfRange = sustainedEcSensors.has(sensorId);
    } else {
      outOfRange = value < min || value > max;
    }
    result.push({ metricId, value, min, max, unit: def.unit, outOfRange });
  }

  return result;
}

/**
 * Plant-level attention flag: any out-of-range metric. Light participates
 * only via the sustained 3-day rule (its outOfRange is set from
 * sustainedLightOOR, never from the instantaneous reading), so nighttime
 * lux can no longer flag the whole greenhouse.
 */
function plantNeedsAttention(metrics: MetricReading[]): boolean {
  return metrics.some((m) => m.outOfRange);
}

/**
 * Sensors whose light_lux has been continuously out-of-range for a full
 * 3-day window — the same rule as the decision-engine's
 * light_lux_sustained_oor (2026-07-21): a single in-range daytime reading
 * resets the streak, so nights alone can never fire; only chronic bad
 * placement does. Readings must span (nearly) the whole window so sparse
 * data can't fake a streak.
 */
async function sustainedLightOOR(
  bounds: Array<{ sensorId: number; lo: number; hi: number }>
): Promise<Set<number>> {
  if (bounds.length === 0) return new Set();
  const query = `
    SELECT r.sensor_id
    FROM bt_readings r
    JOIN unnest($1::int[], $2::float8[], $3::float8[]) AS b(sensor_id, lo, hi)
      ON b.sensor_id = r.sensor_id
    WHERE r.time >= now() - interval '72 hours'
      AND r.light_lux IS NOT NULL
    GROUP BY r.sensor_id, b.lo, b.hi
    HAVING bool_and(r.light_lux < b.lo OR r.light_lux > b.hi)
       AND min(r.time) <= now() - interval '71 hours'
  `;
  const { rows } = await pool.query(query, [
    bounds.map((b) => b.sensorId),
    bounds.map((b) => b.lo),
    bounds.map((b) => b.hi),
  ]);
  return new Set(rows.map((r) => r.sensor_id));
}

/**
 * Sensors whose soil is genuinely waterlogged, as opposed to simply watered
 * recently. Ported from the decision-engine's `soil_not_draining` rule
 * (engine.py range_conditions) so the board and the alerts agree; either
 * branch is enough:
 *
 *   1. above max for a continuous 24h — one in-range sample resets it, and
 *      the readings must span nearly the whole window so a sparse hour of
 *      data cannot fake a streak (same guard as sustainedLightOOR);
 *   2. three separate soggy episodes in 7 days — a pot that re-floods over
 *      and over is not draining even if it dries out between waterings.
 *
 * An episode starts on a reading above max whose predecessor was not above
 * max, which is why the count uses lag() rather than counting wet rows.
 */
async function sustainedWetOOR(
  bounds: Array<{ sensorId: number; hi: number }>
): Promise<Set<number>> {
  if (bounds.length === 0) return new Set();
  const query = `
    WITH b AS (
      SELECT * FROM unnest($1::int[], $2::float8[]) AS t(sensor_id, hi)
    ),
    soaked AS (
      SELECT r.sensor_id
      FROM bt_readings r
      JOIN b ON b.sensor_id = r.sensor_id
      WHERE r.time >= now() - interval '24 hours'
        AND r.moisture IS NOT NULL
      GROUP BY r.sensor_id, b.hi
      HAVING bool_and(r.moisture > b.hi)
         AND min(r.time) <= now() - interval '23 hours'
    ),
    marked AS (
      SELECT r.sensor_id,
             r.moisture > b.hi AS wet,
             lag(r.moisture > b.hi) OVER (
               PARTITION BY r.sensor_id ORDER BY r.time
             ) AS prev_wet
      FROM bt_readings r
      JOIN b ON b.sensor_id = r.sensor_id
      WHERE r.time >= now() - interval '7 days'
        AND r.moisture IS NOT NULL
    ),
    episodes AS (
      SELECT sensor_id, count(*) AS n
      FROM marked
      WHERE wet AND prev_wet IS DISTINCT FROM true
      GROUP BY sensor_id
    )
    SELECT sensor_id FROM soaked
    UNION
    SELECT sensor_id FROM episodes WHERE n >= 3
  `;
  const { rows } = await pool.query(query, [
    bounds.map((b) => b.sensorId),
    bounds.map((b) => b.hi),
  ]);
  return new Set(rows.map((r) => r.sensor_id));
}

/**
 * Below this soil moisture a MiFlora's conductivity reading says more about
 * how dry the soil is than about its nutrients. Mirrors the decision-engine's
 * EC_MIN_MOISTURE (engine.py).
 */
const EC_MIN_MOISTURE = 20;

/**
 * Sensors whose conductivity is genuinely out of range, ported from the
 * decision-engine's feed_needed / salt_buildup branch (engine.py
 * range_conditions). EC is never judged on a single sample because:
 *
 *   - it dips sharply for hours after watering, so an instantaneous low
 *     reading on a well-fed plant is normal;
 *   - it collapses toward zero in dry soil regardless of nutrients, because
 *     the MiFlora measures resistance between two electrodes. Judging "needs
 *     feeding" on a dry pot re-reports thirst as hunger — this fired on 12 of
 *     18 plants when the engine first deployed (2026-07-28).
 *
 * So: a 24h mean over readings spanning at least 12h, and the LOW side is
 * additionally gated on 24h mean moisture clearing the plant's own minimum,
 * floored at EC_MIN_MOISTURE (succulents may sit at 7%, and no EC reading is
 * trustworthy that dry). The high side needs no such gate — dry soil cannot
 * fake excess salt.
 */
async function sustainedEcOOR(
  bounds: Array<{ sensorId: number; lo: number; hi: number; moistFloor: number }>
): Promise<Set<number>> {
  if (bounds.length === 0) return new Set();
  const query = `
    WITH b AS (
      SELECT * FROM unnest($1::int[], $2::float8[], $3::float8[], $4::float8[])
        AS t(sensor_id, lo, hi, moist_floor)
    ),
    ec AS (
      SELECT r.sensor_id, b.lo, b.hi, b.moist_floor,
             avg(r.conductivity_us_cm) AS avg_ec,
             max(r.time) - min(r.time) AS span
      FROM bt_readings r
      JOIN b ON b.sensor_id = r.sensor_id
      WHERE r.time >= now() - interval '24 hours'
        AND r.conductivity_us_cm IS NOT NULL
      GROUP BY r.sensor_id, b.lo, b.hi, b.moist_floor
    ),
    moist AS (
      SELECT r.sensor_id, avg(r.moisture) AS avg_moist
      FROM bt_readings r
      JOIN b ON b.sensor_id = r.sensor_id
      WHERE r.time >= now() - interval '24 hours'
        AND r.moisture IS NOT NULL
      GROUP BY r.sensor_id
    )
    SELECT ec.sensor_id
    FROM ec
    LEFT JOIN moist m ON m.sensor_id = ec.sensor_id
    WHERE ec.span >= interval '12 hours'
      AND (
            ec.avg_ec > ec.hi
         OR (ec.avg_ec < ec.lo
             AND m.avg_moist IS NOT NULL
             AND m.avg_moist >= ec.moist_floor)
          )
  `;
  const { rows } = await pool.query(query, [
    bounds.map((b) => b.sensorId),
    bounds.map((b) => b.lo),
    bounds.map((b) => b.hi),
    bounds.map((b) => b.moistFloor),
  ]);
  return new Set(rows.map((r) => r.sensor_id));
}

/** Bounds input for sustainedEcOOR from assignments + thresholds. */
function collectEcBounds(
  assignments: Array<{ sensor_id: number; plant_id: number }>,
  thresholdsMap: Map<number, ThresholdRow>
): Array<{ sensorId: number; lo: number; hi: number; moistFloor: number }> {
  const bounds: Array<{ sensorId: number; lo: number; hi: number; moistFloor: number }> = [];
  for (const a of assignments) {
    const t = thresholdsMap.get(a.plant_id);
    if (t && t.min_soil_ec != null && t.max_soil_ec != null) {
      bounds.push({
        sensorId: a.sensor_id,
        lo: Number(t.min_soil_ec),
        hi: Number(t.max_soil_ec),
        moistFloor: Math.max(Number(t.min_soil_moist ?? 0), EC_MIN_MOISTURE),
      });
    }
  }
  return bounds;
}

/** Bounds input for sustainedWetOOR from assignments + thresholds. */
function collectMoistureBounds(
  assignments: Array<{ sensor_id: number; plant_id: number }>,
  thresholdsMap: Map<number, ThresholdRow>
): Array<{ sensorId: number; hi: number }> {
  const bounds: Array<{ sensorId: number; hi: number }> = [];
  for (const a of assignments) {
    const t = thresholdsMap.get(a.plant_id);
    if (t && t.max_soil_moist != null) {
      bounds.push({ sensorId: a.sensor_id, hi: Number(t.max_soil_moist) });
    }
  }
  return bounds;
}

/** Bounds input for sustainedLightOOR from assignments + thresholds. */
function collectLightBounds(
  assignments: Array<{ sensor_id: number; plant_id: number }>,
  thresholdsMap: Map<number, ThresholdRow>
): Array<{ sensorId: number; lo: number; hi: number }> {
  const bounds: Array<{ sensorId: number; lo: number; hi: number }> = [];
  for (const a of assignments) {
    const t = thresholdsMap.get(a.plant_id);
    if (t && t.min_light_lux != null && t.max_light_lux != null) {
      bounds.push({
        sensorId: a.sensor_id,
        lo: Number(t.min_light_lux),
        hi: Number(t.max_light_lux),
      });
    }
  }
  return bounds;
}

// ─── getPlants ──────────────────────────────────────────────────────────────

export interface PlantFilters {
  attention?: boolean;
  sort?: "name" | "lastSeen" | "attention";
  order?: "asc" | "desc";
}

export async function getPlants(filters: PlantFilters): Promise<TilePayload[]> {
  const { attention, sort = "name", order = "asc" } = filters;

  // 1. Fetch all active assignments with sensor + plant + label info
  const assignmentQuery = `
    SELECT
      pa.id AS assignment_id,
      pa.plant_id,
      p.display_name AS plant_name,
      p.species,
      s.id AS sensor_id,
      s.last_seen,
      s.location AS sensor_location,
      s.name AS sensor_name,
      sl.label AS sensor_label,
      bat.battery
    FROM plant_assignments pa
    JOIN sensors s ON s.id = pa.sensor_id
    JOIN plants p ON p.id = pa.plant_id
    LEFT JOIN sensor_labels sl ON sl.sensor_id = s.id
    -- Newest *non-null* battery. It has to be non-null rather than "the
    -- latest row", because battery is sampled once a day by the Pi's GATT
    -- job while readings arrive every few minutes — the most recent row's
    -- battery column is almost always empty.
    LEFT JOIN LATERAL (
      SELECT time, battery FROM bt_readings
        WHERE sensor_id = s.id AND battery IS NOT NULL
        ORDER BY time DESC LIMIT 1
    ) bat ON true
    WHERE pa.released_at IS NULL
  `;
  const { rows: assignments } = await pool.query(assignmentQuery);

  if (assignments.length === 0) return [];

  // 2. Collect sensor + plant IDs
  const sensorIds: number[] = [];
  const plantIds: number[] = [];

  for (const a of assignments) {
    sensorIds.push(a.sensor_id);
    plantIds.push(a.plant_id);
  }

  // 3. Fetch latest BT readings
  const btReadings = new Map<number, BtReadingRow>();
  if (sensorIds.length > 0) {
    const btQuery = `
      SELECT DISTINCT ON (sensor_id)
        sensor_id, moisture, temp_c, light_lux, conductivity_us_cm
      FROM bt_readings
      WHERE sensor_id = ANY($1)
      ORDER BY sensor_id, time DESC
    `;
    const { rows } = await pool.query(btQuery, [sensorIds]);
    for (const r of rows) {
      btReadings.set(r.sensor_id, r);
    }
  }

  // 4. Fetch thresholds for all plant_ids
  const thresholdsMap = new Map<number, ThresholdRow>();
  if (plantIds.length > 0) {
    const threshQuery = `
      SELECT plant_id, min_soil_moist, max_soil_moist, min_soil_ec, max_soil_ec,
             min_light_lux, max_light_lux, min_temp, max_temp
      FROM thresholds
      WHERE plant_id = ANY($1)
    `;
    const { rows } = await pool.query(threshQuery, [plantIds]);
    for (const r of rows) {
      thresholdsMap.set(r.plant_id, r);
    }
  }

  // 5. Fetch newest photo per plant (most recent taken_at)
  const photosMap = new Map<number, string>(); // plant_id -> file_path
  if (plantIds.length > 0) {
    const photoQuery = `
      SELECT DISTINCT ON (plant_id)
        plant_id, file_path
      FROM identify_photos
      WHERE plant_id = ANY($1)
      ORDER BY plant_id, taken_at DESC
    `;
    const { rows } = await pool.query(photoQuery, [plantIds]);
    for (const r of rows) {
      photosMap.set(r.plant_id, r.file_path);
    }
  }

  // 6. Sustained checks (one query each), then tile payloads
  const [sustainedLight, sustainedWet, sustainedEc] = await Promise.all([
    sustainedLightOOR(collectLightBounds(assignments, thresholdsMap)),
    sustainedWetOOR(collectMoistureBounds(assignments, thresholdsMap)),
    sustainedEcOOR(collectEcBounds(assignments, thresholdsMap)),
  ]);

  const tiles: TilePayload[] = [];
  for (const a of assignments) {
    const thresholds = thresholdsMap.get(a.plant_id) ?? null;
    const metrics = buildMetrics(
      a.sensor_id,
      thresholds,
      btReadings,
      sustainedLight,
      sustainedWet,
      sustainedEc
    );
    const needsAttention = plantNeedsAttention(metrics);

    const filePath = photosMap.get(a.plant_id) ?? null;
    const photo = filePath ? await buildTilePhoto(filePath) : null;

    tiles.push({
      id: String(a.assignment_id),
      plantId: a.plant_id,
      plantName: a.plant_name,
      species: a.species ?? null,
      sensorLabel: a.sensor_label ?? a.sensor_id.toString(),
      sensorName: a.sensor_name ?? null,
      battery: a.battery ?? null,
      location: a.sensor_location ?? null,
      lastSeen: a.last_seen ? new Date(a.last_seen).toISOString() : "",
      photo,
      metrics,
      needsAttention,
    });
  }

  // 7. Filter
  let result = tiles;
  if (attention) {
    result = result.filter((t) => t.needsAttention);
  }

  // 8. Sort
  const dir = order === "desc" ? -1 : 1;
  result.sort((a, b) => {
    switch (sort) {
      case "name":
        return dir * a.plantName.localeCompare(b.plantName);
      case "lastSeen":
        return dir * (a.lastSeen.localeCompare(b.lastSeen));
      case "attention":
        // needsAttention true first (desc) or last (asc)
        return dir * (Number(b.needsAttention) - Number(a.needsAttention));
      default:
        return 0;
    }
  });

  return result;
}

// ─── History (48h hourly aggregates for detail sparklines) ──────────────────

// date_trunc instead of time_bucket: works on plain Postgres too (test DBs
// have no timescaledb extension; in production it is a hypertable either way).
async function getHistory(sensorId: number): Promise<MetricHistory[]> {
  const byMetric = new Map<string, { t: string; v: number }[]>();

  const { rows } = await pool.query(
    `SELECT date_trunc('hour', time) AS bucket,
            avg(moisture) AS moisture,
            avg(temp_c) AS temp_c,
            avg(light_lux) AS light_lux,
            avg(conductivity_us_cm) AS conductivity_us_cm
     FROM bt_readings
     WHERE sensor_id = $1 AND time > now() - interval '48 hours'
     GROUP BY bucket ORDER BY bucket`,
    [sensorId]
  );
  for (const [metricId, def] of Object.entries(METRIC_REGISTRY)) {
    const col = def.btCol;
    if (!col) continue;
    const points = rows
      .filter((r) => r[col] != null)
      .map((r) => ({
        t: new Date(r.bucket).toISOString(),
        v: Number(r[col]),
      }));
    if (points.length > 0) byMetric.set(metricId, points);
  }

  return [...byMetric.entries()].map(([metricId, points]) => ({
    metricId,
    points,
  }));
}

// ─── getPlant (detail) ──────────────────────────────────────────────────────

export async function getPlant(id: string): Promise<PlantDetail | null> {
  // 1. Fetch the assignment
  const assignmentQuery = `
    SELECT
      pa.id AS assignment_id,
      pa.plant_id,
      p.display_name AS plant_name,
      p.species,
      s.id AS sensor_id,
      s.last_seen,
      s.location AS sensor_location,
      s.name AS sensor_name,
      sl.label AS sensor_label
    FROM plant_assignments pa
    JOIN sensors s ON s.id = pa.sensor_id
    JOIN plants p ON p.id = pa.plant_id
    LEFT JOIN sensor_labels sl ON sl.sensor_id = s.id
    WHERE pa.id = $1 AND pa.released_at IS NULL
  `;
  const { rows: assignments } = await pool.query(assignmentQuery, [id]);

  if (assignments.length === 0) return null;
  const a = assignments[0];

  // 2. Get latest readings
  const btReadings = new Map<number, BtReadingRow>();

  const btQuery = `
    SELECT DISTINCT ON (sensor_id)
      sensor_id, moisture, temp_c, light_lux, conductivity_us_cm
    FROM bt_readings
    WHERE sensor_id = $1
    ORDER BY sensor_id, time DESC
  `;
  const { rows } = await pool.query(btQuery, [a.sensor_id]);
  if (rows.length > 0) btReadings.set(a.sensor_id, rows[0]);

  // 3. Thresholds
  const threshQuery = `
    SELECT min_soil_moist, max_soil_moist, min_soil_ec, max_soil_ec,
           min_light_lux, max_light_lux, min_temp, max_temp
    FROM thresholds
    WHERE plant_id = $1
  `;
  const { rows: threshRows } = await pool.query(threshQuery, [a.plant_id]);
  const thresholds = threshRows.length > 0 ? threshRows[0] : null;

  const thresholdsForBounds = new Map<number, ThresholdRow>();
  if (thresholds) thresholdsForBounds.set(a.plant_id, thresholds);
  const [sustainedLight, sustainedWet, sustainedEc] = await Promise.all([
    sustainedLightOOR(collectLightBounds([a], thresholdsForBounds)),
    sustainedWetOOR(collectMoistureBounds([a], thresholdsForBounds)),
    sustainedEcOOR(collectEcBounds([a], thresholdsForBounds)),
  ]);

  const metrics = buildMetrics(
    a.sensor_id,
    thresholds,
    btReadings,
    sustainedLight,
    sustainedWet,
    sustainedEc
  );
  const needsAttention = plantNeedsAttention(metrics);

  // 4. ALL photos for this plant (newest first) — full gallery
  const photosQuery = `
    SELECT file_path, score, taken_at
    FROM identify_photos
    WHERE plant_id = $1
    ORDER BY taken_at DESC
  `;
  const { rows: photoRows } = await pool.query(photosQuery, [a.plant_id]);

  const photos: PlantDetailPhoto[] = await Promise.all(
    photoRows.map((r) => buildTilePhoto(r.file_path))
  );

  // Tile photo = newest (first in gallery)
  const photo: TilePhoto | null = photos.length > 0 ? photos[0] : null;

  // identifyConfidence = score of newest identify_photos row
  const identifyConfidence =
    photoRows.length > 0 && photoRows[0].score != null
      ? Number(photoRows[0].score)
      : null;

  return {
    id: String(a.assignment_id),
    plantId: a.plant_id,
    plantName: a.plant_name,
    species: a.species ?? null,
    sensorLabel: a.sensor_label ?? a.sensor_id.toString(),
    sensorName: a.sensor_name ?? null,
    battery: a.battery ?? null,
    location: a.sensor_location ?? null,
    lastSeen: a.last_seen ? new Date(a.last_seen).toISOString() : "",
    photo,
    metrics,
    needsAttention,
    photos,
    identifyConfidence,
    history: await getHistory(a.sensor_id),
    sensorId: a.sensor_id,
    rawThresholds: thresholds,
  };
}
