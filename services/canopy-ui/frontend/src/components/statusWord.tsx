/** One-line status for compact tiles: worst out-of-range metric or all-clear. */

import type { MetricReading } from "../api";
import { oorRangeLabel, presentation } from "../format";

export function StatusWord({ metrics }: { metrics: MetricReading[] }) {
  const oor = metrics.filter((m) => m.outOfRange);
  if (oor.length === 0) {
    return <>{metrics.length > 0 ? "all readings in range" : "no readings yet"}</>;
  }
  // Light OOR is informational (nightly), so any other metric outranks it.
  const worst = oor.find((m) => m.metricId !== "light") ?? oor[0];
  return (
    <span className="oor-word">
      {presentation(worst).short.toLowerCase()} {oorRangeLabel(worst)}
    </span>
  );
}

/** Moisture first plus the worst (or first) other metric — the two minis a tile shows. */
export function tileMetrics(metrics: MetricReading[]): MetricReading[] {
  if (metrics.length === 0) return [];
  const [first, ...rest] = metrics;
  const worst = rest.find((m) => m.outOfRange) ?? rest[0];
  return worst ? [first, worst] : [first];
}
