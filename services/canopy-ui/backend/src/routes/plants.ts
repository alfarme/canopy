/** GET /api/plants and GET /api/plants/:id */

import type { FastifyInstance } from "fastify";
import { getPlants, getPlant } from "../queries/plants.js";
import type { PlantFilters } from "../queries/plants.js";

export async function plantsRoutes(app: FastifyInstance): Promise<void> {
  // GET /api/plants?attention=true&sort=name|lastSeen|attention&order=asc|desc
  app.get<{
    Querystring: {
      attention?: string;
      sort?: string;
      order?: string;
    };
  }>("/api/plants", async (request, reply) => {
    const { attention, sort, order } = request.query;

    const filters: PlantFilters = {};

    if (attention === "true") {
      filters.attention = true;
    }

    if (sort === "name" || sort === "lastSeen" || sort === "attention") {
      filters.sort = sort;
    }

    if (order === "asc" || order === "desc") {
      filters.order = order;
    }

    const plants = await getPlants(filters);
    return reply.send({ plants });
  });

  // GET /api/plants/:id
  app.get<{ Params: { id: string } }>(
    "/api/plants/:id",
    async (request, reply) => {
      const { id } = request.params;
      const plant = await getPlant(id);
      if (!plant) {
        return reply.code(404).send({ error: "Not found" });
      }
      return reply.send(plant);
    }
  );
}
