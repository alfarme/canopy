/**
 * Photo cache pre-warmer.
 *
 * Derivatives are cached forever but generated on first request — on the
 * Pi's ARM CPU that means a cold cache turns the first overview visit into
 * blank tiles while sharp works through every photo. Warm the cache at boot
 * and after each upload instead, sequentially so we never saturate the CPU.
 */

import type { FastifyBaseLogger } from "fastify";
import { pool } from "./db.js";
import { dominantColor, ensureDuo, ensureVariant } from "./photo-processing.js";
import { photoUuidFromPath, resolvePhotoPath } from "./photo-paths.js";

/** All variants for one photo (tile + hero + dominant colour). */
export async function warmPhoto(filePath: string): Promise<void> {
  const uuid = photoUuidFromPath(filePath);
  const src = resolvePhotoPath(filePath);
  await dominantColor(uuid, src);
  await ensureVariant(uuid, "tile", src);
  await ensureVariant(uuid, "hero", src);
}

/** Warm every plant-linked photo, plus the overview's featured duo canvas. */
export async function warmPhotoCache(log: FastifyBaseLogger): Promise<void> {
  const { rows } = await pool.query(
    `SELECT file_path FROM identify_photos WHERE plant_id IS NOT NULL`
  );

  let ok = 0;
  let failed = 0;
  for (const r of rows) {
    try {
      await warmPhoto(r.file_path);
      ok++;
    } catch {
      failed++; // missing/unreadable file — tile falls back gracefully anyway
    }
  }

  // The featured duo canvas: mirror the overview's pick — default sort is
  // plant name asc, first two plants that have a photo share the tile.
  try {
    const { rows: newest } = await pool.query(
      `SELECT DISTINCT ON (p.id) p.display_name, ip.file_path
       FROM plant_assignments pa
       JOIN plants p ON p.id = pa.plant_id
       JOIN identify_photos ip ON ip.plant_id = p.id
       WHERE pa.released_at IS NULL
       ORDER BY p.id, ip.taken_at DESC`
    );
    const pair = newest
      .sort((a, b) => a.display_name.localeCompare(b.display_name))
      .slice(0, 2);
    if (pair.length === 2) {
      await ensureDuo(
        photoUuidFromPath(pair[0].file_path),
        resolvePhotoPath(pair[0].file_path),
        photoUuidFromPath(pair[1].file_path),
        resolvePhotoPath(pair[1].file_path)
      );
    }
  } catch (err) {
    log.warn({ err }, "duo pre-warm failed (will generate on demand)");
  }

  log.info(`photo cache warmed: ${ok} photos ok, ${failed} failed`);
}
