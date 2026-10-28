/**
 * Photo routes.
 *
 * GET  /photos/:uuid/:variant      — original | tile | hero (derivatives
 *                                    generated on demand, cached forever)
 * GET  /photos/duo/:uuidA/:uuidB   — shared duo canvas for the featured tile
 * POST /api/plants/:id/photo       — upload a new photo for a plant (multipart);
 *                                    mirrors the Telegram bot's storage layout:
 *                                    PHOTO_BASE_PATH/YYYY-MM-DD/<uuid-hex>.jpg
 *                                    + identify_photos row with plant_id.
 *
 * Photo path resolution:
 * 1. Look up identify_photos row by uuid (basename of file_path without ext).
 * 2. If file_path is absolute and starts with PHOTO_BASE_PATH, use it directly.
 * 3. Otherwise, fall back to PHOTO_BASE_PATH/<parent-basename>/<basename>
 *    which maps to the YYYY-MM-DD/<uuid>.jpg layout on the photo bind mount.
 */

import { createReadStream } from "node:fs";
import { mkdir, stat, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import path from "node:path";
import type { FastifyInstance, FastifyReply } from "fastify";
import fastifyMultipart from "@fastify/multipart";
import { pool } from "../db.js";
import { ensureDuo, ensureVariant } from "../photo-processing.js";
import { PHOTO_BASE_PATH, resolvePhotoPath } from "../photo-paths.js";
import { warmPhoto } from "../photo-warmer.js";
import { renderAlertCard } from "../alert-card.js";
import { getPlants } from "../queries/plants.js";

const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

// uuids come from user-controlled URLs and end up in filenames — allow only
// the bot's uuid4-hex form (plus dashes for robustness).
const UUID_RE = /^[0-9a-f-]{8,40}$/i;

/** file_path of an identify_photos row addressed by its uuid, or null. */
async function lookupPhoto(uuid: string): Promise<string | null> {
  if (!UUID_RE.test(uuid)) return null;
  const { rows } = await pool.query(
    `SELECT file_path FROM identify_photos
     WHERE file_path LIKE '%/' || $1 || '.%'
     LIMIT 1`,
    [uuid]
  );
  return rows.length > 0 ? rows[0].file_path : null;
}

/** Resolve uuid → on-disk source path, or null if row/file missing. */
async function sourcePath(uuid: string): Promise<string | null> {
  let filePath: string | null;
  try {
    filePath = await lookupPhoto(uuid);
  } catch {
    return null;
  }
  if (!filePath) return null;
  const resolved = resolvePhotoPath(filePath);
  try {
    await stat(resolved);
  } catch {
    return null;
  }
  return resolved;
}

async function sendJpeg(reply: FastifyReply, diskPath: string) {
  // Explicit Content-Length: without it the stream goes out chunked, which
  // some client paths (AV http scanners, proxies) truncate on parallel image
  // loads — seen as "stream closed prematurely" + blank tiles.
  const { size } = await stat(diskPath);
  reply.header("Content-Type", "image/jpeg");
  reply.header("Content-Length", size);
  reply.header("Cache-Control", "public, max-age=31536000, immutable");
  return reply.send(createReadStream(diskPath));
}

export async function photosRoutes(app: FastifyInstance): Promise<void> {
  await app.register(fastifyMultipart, {
    limits: { fileSize: MAX_UPLOAD_BYTES, files: 1 },
  });

  // Duo canvas for the featured tile. Registered before the generic
  // /photos/:uuid/:variant route so "duo" is never parsed as a uuid.
  app.get<{ Params: { uuidA: string; uuidB: string } }>(
    "/photos/duo/:uuidA/:uuidB",
    async (request, reply) => {
      const { uuidA, uuidB } = request.params;
      const [srcA, srcB] = await Promise.all([sourcePath(uuidA), sourcePath(uuidB)]);
      if (!srcA || !srcB) {
        return reply.code(404).send({ error: "Not found" });
      }
      try {
        const diskPath = await ensureDuo(uuidA, srcA, uuidB, srcB);
        return sendJpeg(reply, diskPath);
      } catch (err) {
        request.log.error({ err, uuidA, uuidB }, "duo generation failed");
        return reply.code(500).send({ error: "Processing failed" });
      }
    }
  );

  // Alert card for the Telegram bot: composed per request from live readings,
  // so it is NOT cached (and must be registered before /photos/:uuid/:variant,
  // which would otherwise swallow "alert-card" as a uuid).
  app.get<{ Params: { plantId: string } }>(
    "/photos/alert-card/:plantId",
    async (request, reply) => {
      const plantId = Number(request.params.plantId);
      if (!Number.isInteger(plantId)) {
        return reply.code(400).send({ error: "Bad plant id" });
      }
      const plant = (await getPlants({})).find((p) => p.plantId === plantId);
      if (!plant) {
        return reply.code(404).send({ error: "Not found" });
      }
      try {
        const src = plant.photo ? await sourcePath(plant.photo.uuid) : null;
        const jpeg = await renderAlertCard(plant, src);
        reply.header("Content-Type", "image/jpeg");
        reply.header("Content-Length", jpeg.length);
        reply.header("Cache-Control", "no-store");
        return reply.send(jpeg);
      } catch (err) {
        request.log.error({ err, plantId }, "alert card generation failed");
        return reply.code(500).send({ error: "Processing failed" });
      }
    }
  );

  app.get<{ Params: { uuid: string; variant: string } }>(
    "/photos/:uuid/:variant",
    async (request, reply) => {
      const { uuid, variant } = request.params;
      if (variant !== "original" && variant !== "tile" && variant !== "hero") {
        return reply.code(404).send({ error: "Unknown variant" });
      }

      const src = await sourcePath(uuid);
      if (!src) {
        return reply.code(404).send({ error: "Not found" });
      }

      if (variant === "original") {
        return sendJpeg(reply, src);
      }
      try {
        const diskPath = await ensureVariant(uuid, variant, src);
        return sendJpeg(reply, diskPath);
      } catch (err) {
        request.log.error({ err, uuid, variant }, "variant generation failed");
        return reply.code(500).send({ error: "Processing failed" });
      }
    }
  );

  // Photo upload from the detail view ("📷 Update photo").
  app.post<{ Params: { id: string } }>(
    "/api/plants/:id/photo",
    async (request, reply) => {
      const plantId = Number(request.params.id);
      if (!Number.isInteger(plantId) || plantId <= 0) {
        return reply.code(400).send({ error: "Invalid plant id" });
      }

      const plant = await pool.query(
        `SELECT id, display_name FROM plants WHERE id = $1`,
        [plantId]
      );
      if (plant.rows.length === 0) {
        return reply.code(404).send({ error: "Plant not found" });
      }

      const file = await request.file();
      if (!file) {
        return reply.code(400).send({ error: "No file uploaded" });
      }
      if (!/^image\/jpe?g$/.test(file.mimetype)) {
        return reply.code(415).send({ error: "Only JPEG uploads are supported" });
      }

      const buf = await file.toBuffer(); // throws if fileSize limit exceeded

      // Same layout the Telegram bot writes: one subdir per UTC day.
      const day = new Date().toISOString().slice(0, 10);
      const uuid = randomUUID().replace(/-/g, "");
      const dayDir = path.join(PHOTO_BASE_PATH, day);
      const diskPath = path.join(dayDir, `${uuid}.jpg`);
      await mkdir(dayDir, { recursive: true });
      await writeFile(diskPath, buf);

      const { rows } = await pool.query(
        `INSERT INTO identify_photos (file_path, plant_id, plant_name)
         VALUES ($1, $2, $3) RETURNING id`,
        [diskPath, plantId, plant.rows[0].display_name]
      );

      // Fire-and-forget: derivatives are ready before anyone views them.
      warmPhoto(diskPath).catch((err) =>
        request.log.warn({ err, uuid }, "post-upload pre-warm failed")
      );

      return reply
        .code(201)
        .send({ id: rows[0].id, uuid, photoUrl: `/photos/${uuid}/original` });
    }
  );
}
