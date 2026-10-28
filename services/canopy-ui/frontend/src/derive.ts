/** Client-side derivations over the raw API payload: canonical location
 * groups and alert severity. The DB's `location` column is free text typed
 * on a phone (mixed case, occasional misspellings), so tiles are bucketed
 * by fuzzy-matching against the four known rooms rather than string
 * equality. */

import type { MetricReading, TilePayload } from "./api";

/** Display order is fixed (top → bottom), per the product spec. */
export const LOCATION_GROUPS = ["Terrace", "Upstairs", "Living Room", "Kitchen"] as const;
export const FALLBACK_GROUP = "Elsewhere";

function normalize(s: string): string {
  return s.toLowerCase().replace(/[^a-z]/g, "");
}

/** Small-string Levenshtein — inputs are room names, never long. */
function editDistance(a: string, b: string): number {
  const prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    let diag = prev[0];
    prev[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const next = Math.min(
        prev[j] + 1,
        prev[j - 1] + 1,
        diag + (a[i - 1] === b[j - 1] ? 0 : 1)
      );
      diag = prev[j];
      prev[j] = next;
    }
  }
  return prev[b.length];
}

const CANON = LOCATION_GROUPS.map((g) => ({ group: g, key: normalize(g) }));

/** "living room" / "Upstairs" / "terasse" → canonical group; unmatched or
 * missing locations land in the fallback bucket. Tolerance 2 covers typos
 * without letting "kitchen" absorb arbitrary strings. */
export function canonicalLocation(raw: string | null): string {
  if (!raw) return FALLBACK_GROUP;
  const key = normalize(raw);
  if (!key) return FALLBACK_GROUP;
  let best: { group: string; d: number } | null = null;
  for (const c of CANON) {
    const d = editDistance(key, c.key);
    if (!best || d < best.d) best = { group: c.group, d };
  }
  return best && best.d <= 2 ? best.group : FALLBACK_GROUP;
}

export interface LocationGroup {
  name: string;
  plants: TilePayload[];
}

/** Bucket plants into the fixed group order; empty groups are dropped and
 * anything unmatched trails in "Elsewhere". */
export function groupByLocation(plants: TilePayload[]): LocationGroup[] {
  const buckets = new Map<string, TilePayload[]>();
  for (const p of plants) {
    const g = canonicalLocation(p.location);
    (buckets.get(g) ?? buckets.set(g, []).get(g)!).push(p);
  }
  return [...LOCATION_GROUPS, FALLBACK_GROUP]
    .filter((g) => buckets.has(g))
    .map((g) => ({ name: g, plants: buckets.get(g)! }));
}

/* ---------- alert severity ---------- */

export type SeverityLevel = 1 | 2 | 3;

/** Water is always the most critical metric (spec); temperature next
 * (acute), light/fertility are slow-moving → lowest tier. */
const METRIC_RANK: Record<string, SeverityLevel> = {
  moisture: 3,
  temperature: 2,
  light: 1,
  conductivity: 1,
};

/** How far outside the ideal band, relative to the band width — used to
 * order plants within the same severity tier. */
function deviation(m: MetricReading): number {
  const band = Math.max(m.max - m.min, 1e-9);
  if (m.value < m.min) return (m.min - m.value) / band;
  if (m.value > m.max) return (m.value - m.max) / band;
  return 0;
}

export interface Severity {
  level: SeverityLevel;
  /** deviation of the metric that set the level (sort tiebreak) */
  score: number;
  /** the metric driving the severity */
  metricId: string;
}

/** Severity of a plant's worst out-of-range metric, or null if all in
 * range. Rank wins over deviation: a slightly-dry plant outranks a wildly
 * under-fertilized one. */
export function plantSeverity(p: TilePayload): Severity | null {
  let worst: Severity | null = null;
  for (const m of p.metrics) {
    if (!m.outOfRange) continue;
    const level = METRIC_RANK[m.metricId] ?? 1;
    const score = deviation(m);
    if (!worst || level > worst.level || (level === worst.level && score > worst.score)) {
      worst = { level, score, metricId: m.metricId };
    }
  }
  return worst;
}

/** Alerting plants, most critical first. */
export function alertingPlants(
  plants: TilePayload[]
): { plant: TilePayload; severity: Severity }[] {
  return plants
    .map((plant) => ({ plant, severity: plantSeverity(plant)! }))
    .filter((x) => x.severity !== null)
    .sort(
      (a, b) =>
        b.severity.level - a.severity.level || b.severity.score - a.severity.score
    );
}
