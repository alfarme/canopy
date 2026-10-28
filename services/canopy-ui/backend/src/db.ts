/** PostgreSQL connection pool — shared across the service. */

import pg from "pg";

const { Pool } = pg;

if (!process.env.DATABASE_URL) {
  console.error(
    "FATAL: DATABASE_URL is not set. Cannot start without a database connection."
  );
  process.exit(1);
}

export const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
});

pool.on("error", (err) => {
  console.error("Unexpected pg pool error:", err.message);
});

/** Gracefully close the pool. */
export async function closePool(): Promise<void> {
  await pool.end();
}
