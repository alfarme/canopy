/**
 * Canopy — Fastify backend entry point.
 * Registers API routes, static frontend serving with SPA fallback, and photo routes.
 */

import { fileURLToPath } from "node:url";
import path from "node:path";
import Fastify from "fastify";
import fastifyStatic from "@fastify/static";
import { closePool } from "./db.js";
import { healthRoutes } from "./routes/health.js";
import { plantsRoutes } from "./routes/plants.js";
import { photosRoutes } from "./routes/photos.js";
import { plantEditRoutes } from "./routes/plant-edit.js";
import { digestRoutes } from "./routes/digest.js";
import { warmPhotoCache } from "./photo-warmer.js";

const PORT = Number(process.env.PORT) || 8080;
const HOST = "0.0.0.0";

// Resolve frontend dist path — default: ../client relative to this file's dist location,
// matching the Docker layout (/app/server + /app/client).
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_DIST =
  process.env.FRONTEND_DIST ?? path.resolve(__dirname, "../client");

const app = Fastify({ logger: true });

// ─── Register routes ────────────────────────────────────────────────────────
await app.register(healthRoutes);
await app.register(plantsRoutes);
await app.register(photosRoutes);
await app.register(plantEditRoutes);
await app.register(digestRoutes);

// ─── Static file serving (frontend SPA) ─────────────────────────────────────
await app.register(fastifyStatic, {
  root: FRONTEND_DIST,
  prefix: "/",
  // decorateReply must stay enabled: the SPA fallback below calls reply.sendFile.
  wildcard: false,
});

// SPA fallback: any non-/api, non-/photos route returns index.html
app.setNotFoundHandler(async (request, reply) => {
  const url = request.url;
  if (url.startsWith("/api/") || url.startsWith("/photos/")) {
    return reply.code(404).send({ error: "Not found" });
  }
  return reply.sendFile("index.html", FRONTEND_DIST);
});

// ─── Graceful shutdown ──────────────────────────────────────────────────────
async function shutdown(signal: string): Promise<void> {
  app.log.info(`Received ${signal}, shutting down...`);
  await app.close();
  await closePool();
  process.exit(0);
}

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));

// ─── Start ──────────────────────────────────────────────────────────────────
try {
  await app.listen({ port: PORT, host: HOST });
  app.log.info(`Canopy backend listening on ${HOST}:${PORT}`);
  // Fire-and-forget: pre-generate all photo derivatives so the first
  // overview visit never shows blank tiles (cold-cache generation is slow
  // on a Pi's ARM CPU).
  warmPhotoCache(app.log).catch((err) =>
    app.log.warn({ err }, "photo cache pre-warm failed")
  );
} catch (err) {
  app.log.error(err);
  process.exit(1);
}
