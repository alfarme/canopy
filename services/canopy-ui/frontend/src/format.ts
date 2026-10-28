/** Presentation-side metric registry: labels + fixed axis bounds per metric.
 * The API's min/max are the *ideal range*; the measurement primitive also
 * needs stable axis endpoints so the band reads consistently across plants. */

import type { MetricReading } from "./api";
import { canonicalLocation, FALLBACK_GROUP } from "./derive";

export interface MetricPresentation {
  label: string; // panel label
  short: string; // tile mini-bar label
  axisMin: number;
  axisMax: number;
}

export const METRIC_PRESENTATION: Record<string, MetricPresentation> = {
  moisture: { label: "Soil moisture", short: "Moisture", axisMin: 0, axisMax: 100 },
  light: { label: "Light", short: "Light", axisMin: 0, axisMax: 30000 },
  temperature: { label: "Temperature", short: "Temp", axisMin: 5, axisMax: 40 },
  conductivity: { label: "Conductivity", short: "Fertility", axisMin: 0, axisMax: 3000 },
};

const FALLBACK: MetricPresentation = { label: "?", short: "?", axisMin: 0, axisMax: 100 };

export function presentation(m: MetricReading): MetricPresentation {
  const preset = METRIC_PRESENTATION[m.metricId] ?? {
    ...FALLBACK,
    label: m.metricId,
    short: m.metricId,
  };
  // Real thresholds can exceed the preset axis (e.g. Aloe light max 70 klx
  // vs a 30 klx axis) — stretch so the band and dot always fit.
  return {
    ...preset,
    axisMin: Math.min(preset.axisMin, m.min, m.value),
    // Rounded — `m.max * 1.1` is binary-float noise-prone (e.g. 3000 * 1.1
    // = 3300.0000000000005) and axisLabel() below renders it verbatim.
    axisMax: Math.max(preset.axisMax, Math.round(m.max * 1.1), m.value),
  };
}

/** Display value + unit; lux reads as klx like the approved comps. */
export function formatValue(m: MetricReading): { value: string; unit: string } {
  if (m.metricId === "light") {
    return { value: (m.value / 1000).toFixed(1), unit: "klx" };
  }
  if (m.metricId === "temperature") {
    return { value: m.value.toFixed(1), unit: m.unit };
  }
  return { value: String(Math.round(m.value)), unit: m.unit };
}

/** Axis endpoint label: 30000 → "30k". Rounds to 2dp to absorb any
 * lingering binary-float noise (e.g. 55.00000000000001) before display. */
export function axisLabel(n: number): string {
  return n >= 1000 ? `${Math.round((n / 1000) * 100) / 100}k` : String(n);
}

export function oorWord(m: MetricReading): string {
  return m.value > m.max ? "above ideal" : "below ideal";
}

/** Raw ideal-range text, unit-less: "15–60" (light converted to klx to
 * match the displayed value's unit). */
export function rangeLabel(m: MetricReading): string {
  return m.metricId === "light" ? `${m.min / 1000}–${m.max / 1000}` : `${m.min}–${m.max}`;
}

/** Compact out-of-range label with the range folded in: "below 15–60". */
export function oorRangeLabel(m: MetricReading): string {
  return `${m.value > m.max ? "above" : "below"} ${rangeLabel(m)}`;
}

/** "Living room · Sensor 03" — falls back to the composite label. Location
 * is canonicalized (not the raw DB string) so a typo like "terrac" still
 * displays as "Terrace" — the same fuzzy match used to bucket the tile
 * into its section, applied to its caption too. */
export function locationLine(p: {
  location: string | null;
  sensorName: string | null;
  sensorLabel: string;
  battery?: number | null;
}): string {
  let sensor = p.sensorName ?? p.sensorLabel;
  if (/^\d+$/.test(sensor)) sensor = `Sensor ${sensor.padStart(2, "0")}`;
  // Battery rides behind the sensor number: "Sensor 29 (36%)". Omitted when
  // unknown, which is normal — a BT sensor only has a value once the Pi's
  // daily GATT job has managed to connect to it at least once.
  if (p.battery != null) sensor = `${sensor} (${Math.round(p.battery)}%)`;
  const location = p.location ? canonicalLocation(p.location) : null;
  return [location === FALLBACK_GROUP ? null : location, sensor].filter(Boolean).join(" · ");
}

/** "8 min ago" from an ISO timestamp; empty stays empty. */
export function relativeTime(iso: string): string {
  if (!iso) return "no data yet";
  const mins = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60_000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}
