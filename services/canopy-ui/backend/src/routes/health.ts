/** GET /api/health — pings DB with 2s timeout. */

import type { FastifyInstance } from "fastify";
import { pingDb } from "../queries/health.js";

export async function healthRoutes(app: FastifyInstance): Promise<void> {
  app.get("/api/health", async (_request, reply) => {
    const db = await pingDb();
    if (db) {
      return reply.code(200).send({ status: "ok", db: true });
    }
    return reply.code(503).send({ status: "error", db: false });
  });
}
