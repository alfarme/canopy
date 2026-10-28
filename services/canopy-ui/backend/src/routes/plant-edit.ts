/**
 * PATCH /api/plants/:id — edit an assigned plant's name, room, sensor
 * assignment, and thresholds. `:id` is the plant_assignments id, same as
 * GET /api/plants/:id. All edits in one request run in a single transaction.
 *
 * GET /api/sensors — the bt sensor roster (id, label, current assignment)
 * used to populate the "reassign sensor" picker.
 */

import type { FastifyInstance } from "fastify";
import { pool } from "../db.js";
import { LOCATION_GROUPS } from "../locations.js";

interface ThresholdsBody {
  minSoilMoist?: number | null;
  maxSoilMoist?: number | null;
  minSoilEc?: number | null;
  maxSoilEc?: number | null;
  minLightLux?: number | null;
  maxLightLux?: number | null;
  minTemp?: number | null;
  maxTemp?: number | null;
  hysteresisMoist?: number | null;
}

interface EditBody {
  displayName?: string;
  location?: string;
  sensorId?: number;
  thresholds?: ThresholdsBody;
}

export async function plantEditRoutes(app: FastifyInstance): Promise<void> {
  // GET /api/sensors — bt sensors + whichever plant (if any) each is
  // currently assigned to, for the edit modal's sensor picker.
  app.get("/api/sensors", async (_request, reply) => {
    const { rows } = await pool.query(
      `SELECT
         s.id,
         COALESCE(s.name, s.mac_or_device_id) AS name,
         sl.label,
         pa.plant_id AS assigned_plant_id,
         p.display_name AS assigned_plant_name
       FROM sensors s
       LEFT JOIN sensor_labels sl ON sl.sensor_id = s.id
       LEFT JOIN plant_assignments pa ON pa.sensor_id = s.id AND pa.released_at IS NULL
       LEFT JOIN plants p ON p.id = pa.plant_id
       WHERE s.kind = 'bt' AND s.active = true
       ORDER BY s.id`
    );
    return reply.send({
      sensors: rows.map((r) => ({
        id: r.id,
        name: r.name,
        label: r.label ?? r.name,
        assignedPlantId: r.assigned_plant_id,
        assignedPlantName: r.assigned_plant_name,
      })),
    });
  });

  app.patch<{ Params: { id: string }; Body: EditBody }>(
    "/api/plants/:id",
    async (request, reply) => {
      const { id } = request.params;
      const body = request.body ?? {};

      const client = await pool.connect();
      try {
        await client.query("BEGIN");

        const { rows: assignmentRows } = await client.query(
          `SELECT pa.plant_id, pa.sensor_id
           FROM plant_assignments pa
           WHERE pa.id = $1 AND pa.released_at IS NULL`,
          [id]
        );
        if (assignmentRows.length === 0) {
          await client.query("ROLLBACK");
          return reply.code(404).send({ error: "Not found" });
        }
        const plantId: number = assignmentRows[0].plant_id;
        const currentSensorId: number = assignmentRows[0].sensor_id;
        let sensorId = currentSensorId;

        if (typeof body.displayName === "string") {
          const name = body.displayName.trim();
          if (!name) {
            await client.query("ROLLBACK");
            return reply.code(400).send({ error: "Name cannot be empty" });
          }
          await client.query(
            `UPDATE plants SET display_name = $1, updated_at = now() WHERE id = $2`,
            [name, plantId]
          );
        }

        if (typeof body.sensorId === "number" && body.sensorId !== currentSensorId) {
          const { rows: targetSensor } = await client.query(
            `SELECT id FROM sensors WHERE id = $1 AND kind = 'bt' AND active = true`,
            [body.sensorId]
          );
          if (targetSensor.length === 0) {
            await client.query("ROLLBACK");
            return reply.code(400).send({ error: "Sensor not found" });
          }
          const { rows: conflict } = await client.query(
            `SELECT plant_id FROM plant_assignments
             WHERE sensor_id = $1 AND released_at IS NULL`,
            [body.sensorId]
          );
          if (conflict.length > 0) {
            await client.query("ROLLBACK");
            return reply
              .code(409)
              .send({ error: "That sensor is already assigned to another plant" });
          }
          await client.query(
            `UPDATE plant_assignments SET released_at = now()
             WHERE sensor_id = $1 AND plant_id = $2 AND released_at IS NULL`,
            [currentSensorId, plantId]
          );
          await client.query(
            `INSERT INTO plant_assignments (sensor_id, plant_id) VALUES ($1, $2)`,
            [body.sensorId, plantId]
          );
          sensorId = body.sensorId;
        }

        if (typeof body.location === "string") {
          if (!(LOCATION_GROUPS as readonly string[]).includes(body.location)) {
            await client.query("ROLLBACK");
            return reply
              .code(400)
              .send({ error: `Location must be one of: ${LOCATION_GROUPS.join(", ")}` });
          }
          await client.query(`UPDATE sensors SET location = $1 WHERE id = $2`, [
            body.location,
            sensorId,
          ]);
        }

        if (body.thresholds) {
          const t = body.thresholds;
          await client.query(
            `INSERT INTO thresholds (
               plant_id, min_soil_moist, max_soil_moist, min_soil_ec, max_soil_ec,
               min_light_lux, max_light_lux, min_temp, max_temp, hysteresis_moist, updated_at
             ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,COALESCE($10,5),now())
             ON CONFLICT (plant_id) DO UPDATE SET
               min_soil_moist = EXCLUDED.min_soil_moist,
               max_soil_moist = EXCLUDED.max_soil_moist,
               min_soil_ec = EXCLUDED.min_soil_ec,
               max_soil_ec = EXCLUDED.max_soil_ec,
               min_light_lux = EXCLUDED.min_light_lux,
               max_light_lux = EXCLUDED.max_light_lux,
               min_temp = EXCLUDED.min_temp,
               max_temp = EXCLUDED.max_temp,
               hysteresis_moist = COALESCE(EXCLUDED.hysteresis_moist, thresholds.hysteresis_moist),
               updated_at = now()`,
            [
              plantId,
              t.minSoilMoist ?? null,
              t.maxSoilMoist ?? null,
              t.minSoilEc ?? null,
              t.maxSoilEc ?? null,
              t.minLightLux ?? null,
              t.maxLightLux ?? null,
              t.minTemp ?? null,
              t.maxTemp ?? null,
              t.hysteresisMoist ?? null,
            ]
          );
          await client.query(
            `UPDATE plants SET threshold_source = 'manual', updated_at = now() WHERE id = $1`,
            [plantId]
          );
        }

        await client.query("COMMIT");
        return reply.send({ ok: true });
      } catch (err) {
        await client.query("ROLLBACK");
        request.log.error({ err }, "plant edit failed");
        return reply.code(500).send({ error: "Update failed" });
      } finally {
        client.release();
      }
    }
  );
}
