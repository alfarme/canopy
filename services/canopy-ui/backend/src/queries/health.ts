/** Health check query — ping with 2s timeout. */

import { pool } from "../db.js";

export async function pingDb(): Promise<boolean> {
  try {
    const client = await pool.connect();
    try {
      await client.query("SET statement_timeout = 2000");
      await client.query("SELECT 1");
      return true;
    } finally {
      client.release();
    }
  } catch {
    return false;
  }
}
