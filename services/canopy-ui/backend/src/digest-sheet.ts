/**
 * Digest contact sheet — a grid of small plant tiles for the scheduled
 * Telegram digests.
 *
 * Deliberately NOT the alert card. The card is one plant at 900x1350 with a
 * metric row per reading; a digest is a glance at several plants at once, so
 * the sliders are dropped entirely and each tile carries only the photo, the
 * plant name, the user's sensor number, and one short note (how long it has
 * been waiting / dark / out of band). Urgency is carried by the frame colour
 * alone. Palette and severity colours are shared with alert-card.ts so the
 * two read as one family.
 */

import sharp from "sharp";

const PAPER = "#17120a";
const INK = "#f2e9d3";
const INK_SOFT = "#b3a888";
const SEV_COLOR: Record<number, string> = { 3: "#d9482f", 2: "#e0913a", 1: "#d9bc3c" };

const W = 900;
const PAD = 48;
const GAP = 24;
const COLS = 3;
const TILE = Math.round((W - PAD * 2 - GAP * (COLS - 1)) / COLS); // 252
const CAP = 78; // caption block: name+number line, then the note line
const ROW = TILE + CAP;
const HEAD = 112;
const FRAME = 5;
const JPEG_QUALITY = 86;

const esc = (s: string): string =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

const clip = (s: string, n: number): string =>
  s.length > n ? s.slice(0, n - 1) + "…" : s;

export interface SheetItem {
  sensorName: string;
  plantName: string | null;
  /** Absolute path to the source photo, or null to draw a placeholder. */
  photoPath: string | null;
  note: string;
  severity: number;
}

const tilePos = (i: number) => ({
  x: PAD + (i % COLS) * (TILE + GAP),
  y: HEAD + Math.floor(i / COLS) * (ROW + GAP),
});

/**
 * Stand-in for a tile with no photo — an unassigned sensor, or a plant nobody
 * has photographed. A muted panel carrying the sensor number keeps the grid
 * rhythm; leaving a hole would misread as "one fewer problem".
 */
function placeholder(label: string): Buffer {
  return Buffer.from(
    `<svg xmlns="http://www.w3.org/2000/svg" width="${TILE}" height="${TILE}">
       <rect width="${TILE}" height="${TILE}" rx="10" fill="#241d12"/>
       <text x="${TILE / 2}" y="${TILE / 2 + 4}" text-anchor="middle" fill="#5c5343"
             font-family="DejaVu Sans, sans-serif" font-size="56"
             font-weight="700">${esc(label)}</text>
       <text x="${TILE / 2}" y="${TILE / 2 + 38}" text-anchor="middle" fill="#4a4235"
             font-family="DejaVu Sans, sans-serif" font-size="17">no photo</text>
     </svg>`
  );
}

const roundMask = Buffer.from(
  `<svg xmlns="http://www.w3.org/2000/svg" width="${TILE}" height="${TILE}">
     <rect width="${TILE}" height="${TILE}" rx="10" fill="#fff"/></svg>`
);

/** Rendered as three layers, because sharp composites opaquely: paper+header,
 *  then the photos, then frames and captions ON TOP. A frame stroked into the
 *  background layer sits at the tile edge and is buried by the photo. */
export async function renderDigestSheet(
  title: string,
  subtitle: string,
  items: SheetItem[]
): Promise<Buffer> {
  const rows = Math.max(1, Math.ceil(items.length / COLS));
  const H = HEAD + rows * ROW + (rows - 1) * GAP + PAD - 20;

  const bg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}">
<rect width="${W}" height="${H}" fill="${PAPER}"/>
<text x="${PAD}" y="58" fill="${INK}" font-family="DejaVu Sans, sans-serif"
      font-size="34" font-weight="700" letter-spacing="1">${esc(title)}</text>
<text x="${PAD}" y="90" fill="${INK_SOFT}" font-family="DejaVu Sans, sans-serif"
      font-size="20">${esc(subtitle)}</text>
<line x1="${PAD}" y1="${HEAD - 18}" x2="${W - PAD}" y2="${HEAD - 18}"
      stroke="rgba(251,246,234,0.18)" stroke-width="1"/>
</svg>`;

  let fg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}">`;
  for (const [i, it] of items.entries()) {
    const { x, y } = tilePos(i);
    const c = SEV_COLOR[it.severity] ?? SEV_COLOR[1];
    const name = clip(it.plantName ?? "unassigned", 17);
    // xml:space="preserve" with the tspans on one line: librsvg collapses the
    // space between them otherwise and the caption reads "Cannabis(14)".
    fg += `
<rect x="${x + FRAME / 2}" y="${y + FRAME / 2}" width="${TILE - FRAME}" height="${TILE - FRAME}"
      rx="10" fill="none" stroke="${c}" stroke-width="${FRAME}"/>
<circle cx="${x + 9}" cy="${y + TILE + 26}" r="5" fill="${c}"/>
<text x="${x + 24}" y="${y + TILE + 33}" font-family="DejaVu Sans, sans-serif" font-size="21" xml:space="preserve"><tspan fill="${INK}" font-weight="600">${esc(name)}</tspan><tspan fill="${INK_SOFT}" font-weight="400"> (${esc(it.sensorName)})</tspan></text>
<text x="${x + 24}" y="${y + TILE + 60}" fill="${c}" font-family="DejaVu Sans, sans-serif"
      font-size="19" font-weight="600">${esc(clip(it.note, 22))}</text>`;
  }
  fg += `</svg>`;

  const photos = await Promise.all(
    items.map(async (it, i) => {
      const { x, y } = tilePos(i);
      const input = it.photoPath ?? placeholder(it.sensorName);
      // `position: "attention"` matters at 252px: a centre crop wastes the
      // frame on ceiling and floor, saliency pulls it onto the foliage.
      const buf = await sharp(input)
        .rotate() // honour EXIF
        .resize(TILE, TILE, { fit: "cover", position: "attention" })
        .composite([{ input: roundMask, blend: "dest-in" }])
        .png()
        .toBuffer();
      return { input: buf, left: x, top: y };
    })
  );

  return sharp(Buffer.from(bg))
    .composite([...photos, { input: Buffer.from(fg), left: 0, top: 0 }])
    .jpeg({ quality: JPEG_QUALITY })
    .toBuffer();
}
