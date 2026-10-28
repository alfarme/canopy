/**
 * Alert card — the picture the Telegram bot posts alongside an alert.
 *
 * This is the alert-board tile, extended for chat: same graded photo, same
 * caption (display name over the uppercase location line), same severity
 * frame, and the same measurement primitive as the board's metric rows —
 * fixed per-metric axis, ideal band drawn inside it, reading as a dot.
 * Anything that changes here should change in the frontend too:
 *   frontend/src/format.ts          — axis presets, value/range formatting
 *   frontend/src/components/MiniBar.tsx, MetricRow.tsx — the track/band/dot
 *   frontend/src/styles/app.css     — the colours mirrored below
 *
 * Portrait (3:4) because chat clients give a tall image far more screen than
 * a wide one, and the bars need horizontal room to read.
 *
 * Rendered fresh per alert (readings change every bucket) — deliberately NOT
 * run through the derivative cache, which assumes immutable inputs.
 */

import sharp from "sharp";
import type { MetricReading, TilePayload } from "./types.js";

const W = 900;
// Tall enough that four three-line metric rows clear each other; the photo
// still gets the top ~55%.
const H = 1350;
const PAD = 64;
const FRAME = 7; // .tile.sev-N::after — inset 3px on a ~300px tile

// Nocturnal-theme tokens (tokens.css / app.css) — the card is always dark: it
// sits in a chat bubble, not on the dashboard's theme-aware surface.
const PAPER = "#17120a";
const INK = "#f2e9d3";
const INK_SOFT = "#b3a888";
const SEV_COLOR: Record<number, string> = { 3: "#d9482f", 2: "#e0913a", 1: "#d9bc3c" };
// .mini .m-track / .m-range / .m-dot / .m-word
// Track/band pulled slightly further apart than the tile's 0.28/0.38: the
// card is a JPEG over a photo, and a 10% opacity step does not survive that.
const TRACK = "rgba(251,246,234,0.22)";
const BAND = "rgba(251,246,234,0.48)";
const DOT_OK = "#8fbf7f";
const DOT_OOR = "#e8825c";
const OOR_INK = "#f0b396";

/** Water first, then temperature — mirrors METRIC_RANK in the frontend's derive.ts. */
const METRIC_RANK: Record<string, number> = {
  moisture: 3,
  temperature: 2,
  light: 1,
  conductivity: 1,
};

/** METRIC_PRESENTATION in format.ts — `label` is the panel form, which is
 * what a card-sized row wants. */
const AXIS: Record<string, { label: string; min: number; max: number }> = {
  moisture: { label: "Soil moisture", min: 0, max: 100 },
  light: { label: "Light", min: 0, max: 30000 },
  temperature: { label: "Temperature", min: 5, max: 40 },
  conductivity: { label: "Conductivity", min: 0, max: 3000 },
};

const esc = (s: string): string =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

const clip = (s: string, n: number): string => (s.length > n ? s.slice(0, n - 1) + "…" : s);

/** presentation() — preset axis, stretched so a band or reading outside it
 * still fits. Rounded max: `m.max * 1.1` is float-noise prone. */
function axisOf(m: MetricReading): { label: string; min: number; max: number } {
  const preset = AXIS[m.metricId] ?? { label: m.metricId, min: 0, max: 100 };
  return {
    label: preset.label,
    min: Math.min(preset.min, m.min, m.value),
    max: Math.max(preset.max, Math.round(m.max * 1.1), m.value),
  };
}

/** formatValue() — lux reads as klx like the board. */
function formatValue(m: MetricReading): string {
  if (m.metricId === "light") return `${(m.value / 1000).toFixed(1)} klx`;
  const unit = m.unit.trim();
  // "%"/"°C" sit flush; word-like units ("lx", "µS/cm") read as typos without
  // a gap.
  const gap = /^[%°]/.test(unit) ? "" : " ";
  if (m.metricId === "temperature") return `${m.value.toFixed(1)}${gap}${unit}`;
  return `${Math.round(m.value)}${gap}${unit}`;
}

/** rangeLabel() — unit-less ideal range, klx for light. */
const rangeLabel = (m: MetricReading): string =>
  m.metricId === "light" ? `${m.min / 1000}–${m.max / 1000}` : `${m.min}–${m.max}`;

/** axisLabel() — 30000 → "30k". */
const axisLabel = (n: number): string =>
  n >= 1000 ? `${Math.round((n / 1000) * 100) / 100}k` : String(n);

/** locationLine() — "Terrace · Sensor 15". The sensor's own name wins over
 * the composite label, and a bare number becomes "Sensor NN"; without that
 * the line often just repeats the plant name ("Terrace · Tomato (15)"). */
function locationLine(p: TilePayload): string {
  let sensor = p.sensorName ?? p.sensorLabel;
  if (/^\d+$/.test(sensor)) sensor = `Sensor ${sensor.padStart(2, "0")}`;
  return [p.location, sensor].filter(Boolean).join(" · ");
}

/** Severity level of the worst out-of-range metric, or 0 if all in range. */
export function severityOf(metrics: MetricReading[]): number {
  let worst = 0;
  for (const m of metrics) {
    if (m.outOfRange) worst = Math.max(worst, METRIC_RANK[m.metricId] ?? 1);
  }
  return worst;
}

/** Out-of-range metrics first (worst rank first), then the rest. */
function orderMetrics(metrics: MetricReading[]): MetricReading[] {
  return [...metrics].sort((a, b) => {
    const oa = Number(a.outOfRange),
      ob = Number(b.outOfRange);
    if (oa !== ob) return ob - oa;
    return (METRIC_RANK[b.metricId] ?? 1) - (METRIC_RANK[a.metricId] ?? 1);
  });
}

/** One measurement row: name + value, track with the ideal band and reading
 * dot, axis endpoints and the ideal/out-of-range word underneath. */
function metricRow(m: MetricReading, y: number): string {
  const a = axisOf(m);
  const tx = PAD;
  const tw = W - PAD * 2;
  const span = Math.max(a.max - a.min, 1e-9);
  const at = (v: number): number =>
    tx + Math.round(Math.min(1, Math.max(0, (v - a.min) / span)) * tw);
  const bandL = at(m.min);
  const bandR = at(m.max);
  const ty = y + 20; // track top
  const ink = m.outOfRange ? OOR_INK : INK;
  const foot = m.outOfRange
    ? `${m.value > m.max ? "above ideal" : "below ideal"} · ${rangeLabel(m)}`
    : `ideal ${rangeLabel(m)}`;

  return `
    <text x="${tx}" y="${y}" class="k" fill="${INK}">${esc(a.label)}</text>
    <text x="${tx + tw}" y="${y}" class="k" text-anchor="end" fill="${ink}">${esc(
      formatValue(m)
    )}</text>
    <rect x="${tx}" y="${ty}" width="${tw}" height="10" rx="5" fill="${TRACK}"/>
    <rect x="${bandL}" y="${ty}" width="${Math.max(
      bandR - bandL,
      2
    )}" height="10" rx="5" fill="${BAND}"/>
    <circle cx="${at(m.value)}" cy="${ty + 5}" r="13" fill="${m.outOfRange ? DOT_OOR : DOT_OK}"
            stroke="rgba(20,14,5,0.4)" stroke-width="4"/>
    <text x="${tx}" y="${ty + 48}" class="a" fill="${INK_SOFT}">${esc(axisLabel(a.min))}</text>
    <text x="${tx + tw / 2}" y="${ty + 48}" class="a" text-anchor="middle" fill="${
      m.outOfRange ? OOR_INK : INK_SOFT
    }">${esc(foot)}</text>
    <text x="${tx + tw}" y="${ty + 48}" class="a" text-anchor="end" fill="${INK_SOFT}">${esc(
      axisLabel(a.max)
    )}</text>`;
}

function overlaySvg(plant: TilePayload, sev: number): Buffer {
  const accent = SEV_COLOR[sev];
  const shown = orderMetrics(plant.metrics).slice(0, 4);
  // Grow the block upward from a fixed bottom margin: a plant with fewer
  // metrics then just shows more photo instead of a gap under the last bar.
  const ROW = 118;
  const top = H - PAD - 26 - (shown.length - 1) * ROW;
  const rows = shown.map((m, i) => metricRow(m, top + i * ROW)).join("");
  // .tile .caption — name over the uppercase location line.
  const sub = locationLine(plant);
  const nameY = top - 116;

  return Buffer.from(`<svg width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg">
    <style>
      .n { font-family: "Fraunces", serif; font-size: 64px; font-weight: 500; }
      .s { font-family: "Work Sans", sans-serif; font-size: 24px; letter-spacing: 2px; }
      .k { font-family: "Work Sans", sans-serif; font-size: 30px; font-weight: 500; }
      .a { font-family: "Work Sans", sans-serif; font-size: 21px; }
    </style>
    <defs>
      <linearGradient id="scrim" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#100b04" stop-opacity="0"/>
        <stop offset="32%" stop-color="#100b04" stop-opacity="0.80"/>
        <stop offset="56%" stop-color="#100b04" stop-opacity="0.95"/>
        <stop offset="100%" stop-color="#100b04" stop-opacity="0.97"/>
      </linearGradient>
    </defs>
    <rect y="${nameY - 200}" width="100%" height="${H - nameY + 200}" fill="url(#scrim)"/>
    <text x="${PAD}" y="${nameY}" class="n" fill="${INK}">${esc(clip(plant.plantName, 24))}</text>
    <text x="${PAD}" y="${nameY + 42}" class="s" fill="${INK_SOFT}">${esc(
      clip(sub.toUpperCase(), 42)
    )}</text>
    ${rows}
    ${
      accent
        ? `<rect x="${FRAME / 2}" y="${FRAME / 2}" width="${W - FRAME}" height="${
            H - FRAME
          }" fill="none" stroke="${accent}" stroke-width="${FRAME}"/>`
        : ""
    }
  </svg>`);
}

/** Photo: same grade/vignette recipe as the tile variant, cropped 3:4. */
async function photoLayer(srcPath: string): Promise<Buffer> {
  const base = await sharp(srcPath)
    .rotate() // EXIF orientation
    .resize(W, H, { fit: "cover", position: sharp.strategy.attention })
    .modulate({ brightness: 0.98, saturation: 1.6, hue: -6 })
    .linear([1.22, 1.05, 0.9], [-18, -10, -4])
    .gamma(1.12)
    .toBuffer();
  const vignette = Buffer.from(
    `<svg width="${W}" height="${H}">
      <defs>
        <radialGradient id="v" cx="50%" cy="34%" r="72%">
          <stop offset="58%" stop-color="#1a1208" stop-opacity="0"/>
          <stop offset="100%" stop-color="#1a1208" stop-opacity="0.45"/>
        </radialGradient>
      </defs>
      <rect width="100%" height="100%" fill="url(#v)"/>
    </svg>`
  );
  return sharp(base).composite([{ input: vignette, blend: "over" }]).toBuffer();
}

/**
 * Compose the card. `srcPath` may be null (plant without a photo) — the card
 * then renders on flat paper, so an alert always gets one.
 */
export async function renderAlertCard(
  plant: TilePayload,
  srcPath: string | null
): Promise<Buffer> {
  const sev = severityOf(plant.metrics);
  const layers: sharp.OverlayOptions[] = [];
  if (srcPath) {
    try {
      layers.push({ input: await photoLayer(srcPath), left: 0, top: 0 });
    } catch {
      /* unreadable photo — card still renders, just without it */
    }
  }
  layers.push({ input: overlaySvg(plant, sev), left: 0, top: 0 });

  return sharp({
    create: { width: W, height: H, channels: 3, background: PAPER },
  })
    .composite(layers)
    .jpeg({ quality: 86 })
    .toBuffer();
}
