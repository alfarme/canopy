/**
 * Digest queries — the item lists behind the scheduled Telegram sheets.
 *
 * These read LIVE sensor state rather than draining `alert_events`. That is
 * deliberate: an event log tells you a condition fired at some point, while a
 * digest has to answer "what is true right now, and for how long has it been
 * true" — which is what the sheets show and what the ranking needs. It also
 * means a digest cannot be thrown off by a backlog of undelivered rows.
 *
 * Every query honours `alert_silences`, so silencing a sensor in Telegram
 * keeps it out of the digests too.
 */

import { pool } from "../db.js";

export type DigestKind = "water" | "sensors" | "feed";

export interface DigestItem {
  sensorName: string;      // the user's number, e.g. "14" — never sensors.id
  plantName: string | null;
  photoUuid: string | null;
  /** Short right-hand line under the caption, e.g. "2.2 days". */
  note: string;
  /** 1..3, drives the frame colour. */
  severity: number;
}

/** Newest photo for a plant, as a correlated scalar subquery fragment. */
const PHOTO_SQL = `
  (SELECT ip.file_path FROM identify_photos ip
    WHERE ip.plant_id = p.id ORDER BY ip.taken_at DESC LIMIT 1)
`;

/**
 * Plants that need water, longest-waiting first.
 *
 * Three filters matter more than the threshold comparison itself, and each one
 * was put here because it produced a wrong list without it:
 *
 *   1. FRESHNESS. The reading must be under 24h old. A dead sensor's last
 *      reading is frozen below its threshold forever, so without this the
 *      ranking is topped by whichever sensor died longest ago — Anthurium (8)
 *      sat at "19.6 days dry" while actually being 17 days offline.
 *   2. EVER-WET. `last_wet IS NOT NULL` — a sensor that has never once crossed
 *      its own threshold in 90 days is not in a dry pot, it is broken.
 *      Cannabis (14) reads 0.00 permanently and would otherwise hold the top
 *      slot every single day.
 *   3. SILENCES. A sensor silenced for `water_needed` is excluded.
 *
 * Ranked by how long the plant has been waiting, not by how dry it is: depth
 * is noisy sample to sample, duration is monotone and is what the user asked
 * to prioritise.
 */
async function waterItems(limit: number): Promise<DigestItem[]> {
  const { rows } = await pool.query(
    `
    SELECT s.name AS sensor_name,
           regexp_replace(p.display_name, ' \\(#[^)]+\\)$', '') AS plant_name,
           ${PHOTO_SQL} AS photo_path,
           EXTRACT(epoch FROM (now() - w.last_wet)) / 86400.0 AS days_dry,
           r.moisture,
           COALESCE(t.min_soil_moist, 20) AS min_moist
    FROM plant_assignments a
    JOIN sensors s  ON s.id = a.sensor_id AND s.kind = 'bt' AND s.active
    JOIN plants  p  ON p.id = a.plant_id
    LEFT JOIN thresholds t ON t.plant_id = p.id
    LEFT JOIN LATERAL (
      SELECT b.moisture, b.time FROM bt_readings b
       WHERE b.sensor_id = s.id AND b.moisture IS NOT NULL
       ORDER BY b.time DESC LIMIT 1
    ) r ON true
    LEFT JOIN LATERAL (
      SELECT max(b.time) AS last_wet FROM bt_readings b
       WHERE b.sensor_id = s.id
         AND b.time > now() - interval '90 days'
         AND b.moisture >= COALESCE(t.min_soil_moist, 20)
    ) w ON true
    WHERE a.released_at IS NULL
      AND r.time > now() - interval '24 hours'
      AND r.moisture < COALESCE(t.min_soil_moist, 20)
      AND w.last_wet IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM alert_silences sil
         WHERE sil.sensor_id = s.id AND sil.kind = 'water_needed'
      )
    ORDER BY days_dry DESC
    LIMIT $1
    `,
    [limit]
  );
  return rows.map((r) => {
    const deficit = Number(r.min_moist) - Number(r.moisture);
    return {
      sensorName: r.sensor_name,
      plantName: r.plant_name,
      photoUuid: uuidFromPath(r.photo_path),
      note: `${Number(r.days_dry).toFixed(1)} days`,
      severity: deficit >= 10 ? 3 : deficit >= 5 ? 2 : 1,
    };
  });
}

/**
 * Sensors that are not reporting, or reporting nonsense — the weekly sheet.
 *
 * Two distinct faults, because they need different actions and look nothing
 * alike in the data (see docs/technical.md, "Two zeros mean two different
 * faults"):
 *
 *   SILENT       — no reading in 24h. Usually a flat cell.
 *   MOISTURE DEAD— reporting fine, but moisture pinned at exactly 0 across a
 *                  long run while conductivity still responds. Moisture is
 *                  capacitive and EC resistive; one channel is gone and the
 *                  board needs replacing. Requires EC to be working, so a
 *                  probe lying out of its pot (both channels zero) is not
 *                  mistaken for a hardware fault.
 *
 * Unassigned sensors are included — a spare going flat in a drawer is still
 * worth knowing about — but sensors silenced for `stale` are not.
 */
async function sensorItems(limit: number): Promise<DigestItem[]> {
  const { rows } = await pool.query(
    `
    WITH agg AS (
      SELECT s.id, s.name, p.id AS plant_id, p.display_name,
             max(b.time) AS last_seen,
             count(b.time) FILTER (
               WHERE b.time > now() - interval '14 days' AND b.moisture IS NOT NULL
             ) AS n_moist,
             max(b.moisture) FILTER (WHERE b.time > now() - interval '14 days') AS max_moist,
             max(b.conductivity_us_cm) FILTER (WHERE b.time > now() - interval '14 days') AS max_ec
      FROM sensors s
      LEFT JOIN plant_assignments a ON a.sensor_id = s.id AND a.released_at IS NULL
      LEFT JOIN plants p ON p.id = a.plant_id
      LEFT JOIN bt_readings b ON b.sensor_id = s.id
      WHERE s.kind = 'bt' AND s.active AND s.name IS NOT NULL
        AND NOT EXISTS (
          SELECT 1 FROM alert_silences sil
           WHERE sil.sensor_id = s.id AND sil.kind = 'stale'
        )
      GROUP BY s.id, s.name, p.id, p.display_name
    )
    SELECT agg.name AS sensor_name,
           regexp_replace(agg.display_name, ' \\(#[^)]+\\)$', '') AS plant_name,
           (SELECT ip.file_path FROM identify_photos ip
             WHERE ip.plant_id = agg.plant_id ORDER BY ip.taken_at DESC LIMIT 1) AS photo_path,
           EXTRACT(epoch FROM (now() - agg.last_seen)) / 86400.0 AS days_dark,
           (agg.last_seen IS NULL OR agg.last_seen < now() - interval '24 hours') AS silent
    FROM agg
    WHERE agg.last_seen IS NULL
       OR agg.last_seen < now() - interval '24 hours'
       OR (agg.n_moist > 20 AND agg.max_moist = 0 AND agg.max_ec > 0)
    ORDER BY days_dark DESC NULLS FIRST
    LIMIT $1
    `,
    [limit]
  );
  return rows.map((r) => {
    const days = r.days_dark == null ? null : Number(r.days_dark);
    const note = r.silent
      ? days == null
        ? "never reported"
        : `dark ${days.toFixed(0)} days`
      : "moisture channel dead";
    return {
      sensorName: r.sensor_name,
      plantName: r.plant_name,
      photoUuid: uuidFromPath(r.photo_path),
      note,
      // A sensor dark for over a week outranks one that dropped out yesterday.
      severity: days == null || days >= 7 ? 3 : days >= 2 ? 2 : 1,
    };
  });
}

/**
 * Slow soil chemistry — the monthly sheet: needs feeding, or salt building up.
 *
 * Identical rule to Canopy's own board and the decision engine's
 * feed_needed/salt_buildup: a 24h mean over readings spanning at least 12h,
 * with the LOW side gated on 24h mean moisture clearing the plant's minimum
 * (floored at 20%), because EC collapses in dry soil and would otherwise
 * report thirst as hunger.
 */
async function feedItems(limit: number): Promise<DigestItem[]> {
  const { rows } = await pool.query(
    `
    WITH bounds AS (
      SELECT a.sensor_id, s.name, p.id AS plant_id, p.display_name,
             t.min_soil_ec::float8 AS lo, t.max_soil_ec::float8 AS hi,
             GREATEST(COALESCE(t.min_soil_moist, 0), 20)::float8 AS moist_floor
      FROM plant_assignments a
      JOIN sensors s ON s.id = a.sensor_id AND s.kind = 'bt' AND s.active
      JOIN plants  p ON p.id = a.plant_id
      JOIN thresholds t ON t.plant_id = p.id
      WHERE a.released_at IS NULL
        AND t.min_soil_ec IS NOT NULL AND t.max_soil_ec IS NOT NULL
        AND NOT EXISTS (
          SELECT 1 FROM alert_silences sil
           WHERE sil.sensor_id = a.sensor_id
             AND sil.kind IN ('feed_needed', 'salt_buildup')
        )
    ),
    ec AS (
      SELECT b.*, avg(r.conductivity_us_cm) AS avg_ec,
             max(r.time) - min(r.time) AS span
      FROM bounds b
      JOIN bt_readings r ON r.sensor_id = b.sensor_id
      WHERE r.time >= now() - interval '24 hours'
        AND r.conductivity_us_cm IS NOT NULL
      GROUP BY b.sensor_id, b.name, b.plant_id, b.display_name, b.lo, b.hi, b.moist_floor
    ),
    moist AS (
      SELECT r.sensor_id, avg(r.moisture) AS avg_moist
      FROM bounds b
      JOIN bt_readings r ON r.sensor_id = b.sensor_id
      WHERE r.time >= now() - interval '24 hours' AND r.moisture IS NOT NULL
      GROUP BY r.sensor_id
    )
    SELECT ec.name AS sensor_name,
           regexp_replace(ec.display_name, ' \\(#[^)]+\\)$', '') AS plant_name,
           (SELECT ip.file_path FROM identify_photos ip
             WHERE ip.plant_id = ec.plant_id ORDER BY ip.taken_at DESC LIMIT 1) AS photo_path,
           round(ec.avg_ec) AS avg_ec, ec.lo, ec.hi,
           (ec.avg_ec > ec.hi) AS is_salt
    FROM ec LEFT JOIN moist m ON m.sensor_id = ec.sensor_id
    WHERE ec.span >= interval '12 hours'
      AND (
            ec.avg_ec > ec.hi
         OR (ec.avg_ec < ec.lo AND m.avg_moist IS NOT NULL AND m.avg_moist >= ec.moist_floor)
          )
    -- Worst offenders first: how far outside the band, as a fraction of it.
    ORDER BY CASE WHEN ec.avg_ec > ec.hi
                  THEN (ec.avg_ec - ec.hi) / NULLIF(ec.hi, 0)
                  ELSE (ec.lo - ec.avg_ec) / NULLIF(ec.lo, 0) END DESC
    LIMIT $1
    `,
    [limit]
  );
  return rows.map((r) => ({
    sensorName: r.sensor_name,
    plantName: r.plant_name,
    photoUuid: uuidFromPath(r.photo_path),
    note: r.is_salt
      ? `salt ${r.avg_ec} (max ${Math.round(Number(r.hi))})`
      : `feed ${r.avg_ec} (min ${Math.round(Number(r.lo))})`,
    severity: r.is_salt ? 3 : 2,
  }));
}

/** identify_photos stores a full path; the photo pipeline keys off the uuid. */
function uuidFromPath(p: string | null): string | null {
  if (!p) return null;
  const base = p.split("/").pop() ?? "";
  const uuid = base.replace(/\.[^.]+$/, "");
  return /^[0-9a-f]{32}$/i.test(uuid) ? uuid : null;
}

export interface Digest {
  kind: DigestKind;
  title: string;
  items: DigestItem[];
}

const TITLES: Record<DigestKind, string> = {
  water: "Water needed",
  sensors: "Sensors down",
  feed: "Fertiliser & salt",
};

/** Item cap per sheet. Water is the user's "top 5"; the others are rarer and
 *  a truncated hardware list is worse than a slightly taller image. */
const LIMITS: Record<DigestKind, number> = { water: 5, sensors: 12, feed: 9 };

export async function getDigest(kind: DigestKind): Promise<Digest> {
  const limit = LIMITS[kind];
  const items =
    kind === "water"
      ? await waterItems(limit)
      : kind === "sensors"
        ? await sensorItems(limit)
        : await feedItems(limit);
  return { kind, title: TITLES[kind], items };
}
