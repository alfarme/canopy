/**
 * Digest routes — the scheduled Telegram sheets.
 *
 * GET /api/digest/:kind     — the item list as JSON
 * GET /photos/digest/:kind  — the same digest rendered as a contact sheet
 *
 * Two endpoints for one digest so the bot can decide *whether* to post before
 * paying to render: an empty digest must produce no message at all, and asking
 * for the image first would mean rendering a sheet only to discard it. The
 * pair is not transactional — state could change between the two calls — which
 * is fine, because the worst case is a sheet whose contents shifted by one row
 * in the seconds between them.
 *
 * Neither endpoint is cached. A digest is a point-in-time answer, fires a
 * handful of times a month, and caching it would risk serving yesterday's.
 */

import type { FastifyInstance } from "fastify";
import { getDigest, type DigestKind } from "../queries/digest.js";
import { renderDigestSheet, type SheetItem } from "../digest-sheet.js";
import { resolvePhotoPath } from "../photo-paths.js";
import { pool } from "../db.js";
import { stat } from "node:fs/promises";

const KINDS: readonly DigestKind[] = ["water", "sensors", "feed"] as const;

function parseKind(raw: string): DigestKind | null {
  return (KINDS as readonly string[]).includes(raw) ? (raw as DigestKind) : null;
}

/** uuid → on-disk source path, or null when the row or the file is missing.
 *  A missing photo is not an error here: the sheet draws a placeholder. */
async function photoPath(uuid: string | null): Promise<string | null> {
  if (!uuid) return null;
  try {
    const { rows } = await pool.query(
      `SELECT file_path FROM identify_photos
        WHERE file_path LIKE '%/' || $1 || '.%' LIMIT 1`,
      [uuid]
    );
    if (rows.length === 0) return null;
    const resolved = resolvePhotoPath(rows[0].file_path);
    await stat(resolved);
    return resolved;
  } catch {
    return null;
  }
}

/** Per-kind phrasing. Naive "+s" pluralisation produced "3 waitings", and
 *  only two of the three sheets are actually ordered by duration. */
const PHRASE: Record<DigestKind, { count: (n: number) => string; order: string }> = {
  water: { count: (n) => `${n} waiting`, order: "longest first" },
  sensors: {
    count: (n) => `${n} sensor${n === 1 ? "" : "s"}`,
    order: "longest first",
  },
  feed: {
    count: (n) => `${n} plant${n === 1 ? "" : "s"}`,
    order: "furthest out of band first",
  },
};

/** "Sun 17 Aug, 19:00 · 5 waiting · longest first" */
function subtitle(kind: DigestKind, count: number): string {
  const stamp = new Intl.DateTimeFormat("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Europe/Zurich",
  }).format(new Date());
  const p = PHRASE[kind];
  return `${stamp} · ${p.count(count)} · ${p.order}`;
}

export async function digestRoutes(app: FastifyInstance): Promise<void> {
  app.get<{ Params: { kind: string } }>(
    "/api/digest/:kind",
    async (request, reply) => {
      const kind = parseKind(request.params.kind);
      if (!kind) return reply.code(400).send({ error: "Unknown digest kind" });
      return getDigest(kind);
    }
  );

  app.get<{ Params: { kind: string } }>(
    "/photos/digest/:kind",
    async (request, reply) => {
      const kind = parseKind(request.params.kind);
      if (!kind) return reply.code(400).send({ error: "Unknown digest kind" });

      const digest = await getDigest(kind);
      if (digest.items.length === 0) {
        // Nothing to draw. 404 rather than an empty sheet, so a bot that
        // skipped the JSON check still cannot post a blank image.
        return reply.code(404).send({ error: "Digest is empty" });
      }

      try {
        const items: SheetItem[] = await Promise.all(
          digest.items.map(async (it) => ({
            sensorName: it.sensorName,
            plantName: it.plantName,
            photoPath: await photoPath(it.photoUuid),
            note: it.note,
            severity: it.severity,
          }))
        );
        const jpeg = await renderDigestSheet(
          digest.title,
          subtitle(kind, items.length),
          items
        );
        return reply
          .header("Content-Type", "image/jpeg")
          .header("Cache-Control", "no-store")
          .send(jpeg);
      } catch (err) {
        request.log.error({ err, kind }, "digest render failed");
        return reply.code(500).send({ error: "Processing failed" });
      }
    }
  );
}
