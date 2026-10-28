/**
 * Canopy photo treatment pipeline — "strong grade + soft vignette"
 * (design brief §6, validated on real photos).
 *
 * Derivatives are generated on demand at first request and cached forever:
 * photo rows are immutable (new photo = new uuid), so the cache never
 * invalidates. Writes are atomic (tmp file + rename) and deduplicated via an
 * in-flight map so concurrent first requests don't do the work twice.
 *
 * Variants:
 *   tile  900×1200 cover crop (attention position — approved for portrait
 *         crops only; never use attention for wide/landscape crops)
 *   hero  1600×900 — full photo contained, seated left of centre on a
 *         blurred darkened echo of itself (never crop the subject)
 *   duo   1400×860 — two 0.8-aspect upper-biased crops, height-fit, side by
 *         side (photo:dead-space ratio ≥ 3 rule)
 */

import { mkdir, rename, stat, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import sharp from "sharp";

const CACHE_DIR = process.env.PHOTO_CACHE_PATH ?? "/data/photo-cache";

const JPEG_QUALITY = 84;

// ─── Shared recipe pieces ───────────────────────────────────────────────────

const grade = (img: sharp.Sharp): sharp.Sharp =>
  img
    .modulate({ brightness: 0.98, saturation: 1.6, hue: -6 })
    .linear([1.22, 1.05, 0.9], [-18, -10, -4])
    .gamma(1.12);

const warmWash = (w: number, h: number): Buffer =>
  Buffer.from(
    `<svg width="${w}" height="${h}"><rect width="100%" height="100%" fill="#c97a2e" fill-opacity="0.16"/></svg>`
  );

// Radial vignette peaks at 45% opacity (user-softened — do not re-darken)
// plus the bottom caption gradient (~55% peak).
const vignette = (w: number, h: number): Buffer =>
  Buffer.from(
    `<svg width="${w}" height="${h}">
      <defs>
        <radialGradient id="v" cx="50%" cy="46%" r="72%">
          <stop offset="58%" stop-color="#1a1208" stop-opacity="0"/>
          <stop offset="100%" stop-color="#1a1208" stop-opacity="0.45"/>
        </radialGradient>
        <linearGradient id="b" x1="0" y1="0" x2="0" y2="1">
          <stop offset="60%" stop-color="#140e05" stop-opacity="0"/>
          <stop offset="100%" stop-color="#140e05" stop-opacity="0.55"/>
        </linearGradient>
      </defs>
      <rect width="100%" height="100%" fill="url(#v)"/>
      <rect width="100%" height="100%" fill="url(#b)"/>
    </svg>`
  );

// ─── Variant generators (write to outPath) ──────────────────────────────────

async function tileVariant(src: string, outPath: string): Promise<void> {
  const W = 900,
    H = 1200;
  const base = await grade(
    sharp(src)
      .rotate() // apply EXIF orientation (phone portraits store landscape pixels + a rotate tag)
      .resize(W, H, { fit: "cover", position: sharp.strategy.attention })
  ).toBuffer();
  await sharp(base)
    .composite([
      { input: warmWash(W, H), blend: "soft-light" },
      { input: vignette(W, H), blend: "over" },
    ])
    .jpeg({ quality: JPEG_QUALITY })
    .toFile(outPath);
}

async function heroVariant(src: string, outPath: string): Promise<void> {
  const W = 1600,
    H = 900;
  const backdrop = await sharp(src)
    .rotate()
    .resize(W, H, { fit: "cover" })
    .blur(28)
    .modulate({ brightness: 0.55, saturation: 1.3 })
    .toBuffer();
  const subj = await grade(sharp(src).rotate().resize(null, H, { fit: "inside" })).toBuffer();
  const meta = await sharp(subj).metadata();
  const left = Math.max(0, Math.round(W * 0.3 - (meta.width ?? 0) / 2));
  await sharp(backdrop)
    .composite([
      { input: subj, left, top: 0 },
      { input: warmWash(W, H), blend: "soft-light" },
      { input: vignette(W, H), blend: "over" },
    ])
    .jpeg({ quality: JPEG_QUALITY })
    .toFile(outPath);
}

const DUO_W = 1400,
  DUO_H = 860,
  DUO_ASPECT = 0.8;

// Each half occupies a fixed slot, so two halves plus the gap are exactly
// DUO_W wide no matter what the source photos look like. This is load-bearing:
// duoSubject used to be height-fit only, letting the width fall out of the
// source aspect, which is fine for the portrait photos the duo canvas was
// designed around but overflows badly for a landscape one. A 1280x720 source
// scaled to 860 high is 1529px — wider than the whole canvas — and sharp
// rejects a composite input larger than its base with "Image to composite must
// have same dimensions or smaller", 500ing the endpoint and leaving the tile a
// flat dominantColor grey. For a 0.8-aspect portrait these constants reproduce
// the previous geometry exactly (688x860 is 0.8), so only odd aspects change.
const DUO_GAP = 24,
  DUO_HALF_W = (DUO_W - DUO_GAP) / 2; // 688

/** One duo half: 0.8-aspect, upper-biased (35%) crop, graded, fixed-size. */
async function duoSubject(src: string): Promise<Buffer> {
  const m = await sharp(src).metadata();
  // metadata() reports raw sensor width/height regardless of the EXIF
  // orientation tag; swap them when that tag says the display dimensions
  // are rotated 90°, so the crop math below matches the .rotate()'d pixels.
  const swapped = (m.orientation ?? 1) >= 5;
  const w = (swapped ? m.height : m.width) ?? 0,
    h = (swapped ? m.width : m.height) ?? 0;
  const ch = Math.round(w / DUO_ASPECT);
  const top = Math.max(0, Math.round((h - ch) * 0.35));
  // The extract picks the region (0.8 aspect when the source is tall enough,
  // clamped to the frame when it is not); the cover-resize then guarantees the
  // slot dimensions. When the extract already is 0.8 this is a pure scale.
  return grade(
    sharp(src)
      .rotate()
      .extract({ left: 0, top, width: w, height: Math.min(ch, h - top) })
      .resize(DUO_HALF_W, DUO_H, { fit: "cover", position: "attention" })
  ).toBuffer();
}

async function duoVariant(srcA: string, srcB: string, outPath: string): Promise<void> {
  const backdrop = await sharp(srcA)
    .rotate()
    .resize(DUO_W, DUO_H, { fit: "cover" })
    .blur(28)
    .modulate({ brightness: 0.55, saturation: 1.3 })
    .toBuffer();
  const [sa, sb] = await Promise.all([duoSubject(srcA), duoSubject(srcB)]);
  // Both halves are exactly DUO_HALF_W x DUO_H, so the slots are fixed and
  // the right edge lands on DUO_W precisely — no measure-then-place needed.
  await sharp(backdrop)
    .composite([
      { input: sa, left: 0, top: 0 },
      { input: sb, left: DUO_HALF_W + DUO_GAP, top: 0 },
      { input: warmWash(DUO_W, DUO_H), blend: "soft-light" },
      { input: vignette(DUO_W, DUO_H), blend: "over" },
    ])
    .jpeg({ quality: JPEG_QUALITY })
    .toFile(outPath);
}

// ─── Cache orchestration ────────────────────────────────────────────────────

export type Variant = "tile" | "hero";

const inFlight = new Map<string, Promise<string>>();

async function cached(
  key: string,
  generate: (outPath: string) => Promise<void>
): Promise<string> {
  const finalPath = path.join(CACHE_DIR, key);
  try {
    await stat(finalPath);
    return finalPath;
  } catch {
    /* not cached yet */
  }

  const running = inFlight.get(key);
  if (running) return running;

  const job = (async () => {
    await mkdir(CACHE_DIR, { recursive: true });
    const tmpPath = path.join(CACHE_DIR, `.tmp-${process.pid}-${key}`);
    await generate(tmpPath);
    await rename(tmpPath, finalPath);
    return finalPath;
  })().finally(() => inFlight.delete(key));

  inFlight.set(key, job);
  return job;
}

/** Path to the cached tile/hero derivative, generating it on first request. */
export function ensureVariant(uuid: string, variant: Variant, srcPath: string): Promise<string> {
  const gen = variant === "tile" ? tileVariant : heroVariant;
  return cached(`${variant}-${uuid}.jpg`, (out) => gen(srcPath, out));
}

/** Path to the cached duo canvas for an ordered pair of photos. */
export function ensureDuo(
  uuidA: string,
  srcA: string,
  uuidB: string,
  srcB: string
): Promise<string> {
  return cached(`duo-${uuidA}-${uuidB}.jpg`, (out) => duoVariant(srcA, srcB, out));
}

/** Dominant colour (#rrggbb) via 1×1 resize; cached next to the derivatives. */
export async function dominantColor(uuid: string, srcPath: string): Promise<string> {
  const colorPath = path.join(CACHE_DIR, `${uuid}.color`);
  try {
    return (await readFile(colorPath, "utf8")).trim();
  } catch {
    /* not cached yet */
  }
  const { data } = await sharp(srcPath)
    .rotate()
    .resize(1, 1, { fit: "cover" })
    .removeAlpha()
    .raw()
    .toBuffer({ resolveWithObject: true });
  const hex = `#${[data[0], data[1], data[2]]
    .map((c) => c.toString(16).padStart(2, "0"))
    .join("")}`;
  await mkdir(CACHE_DIR, { recursive: true });
  await writeFile(colorPath, hex, "utf8");
  return hex;
}
