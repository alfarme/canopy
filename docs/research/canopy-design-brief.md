# Canopy — design brief (attempt #2, approved direction)

Status: brainstorming complete 2026-07-22. Every decision below was made against
rendered mockups and real processed photos, not written adjectives.

Predecessor: "Object Atlas", rejected on visual grounds; its technical patterns remain reusable.

## 1. Visual concept & mood

**Greenhouse / conservatory.** Warm, alive, lived-in — dappled light, glass-and-plant
warmth, a real space rather than an archive or a SaaS product. Photography is the
protagonist; data supports it.

Rejected moods (shown as rendered alternatives): botanical field journal, quiet
gallery/museum cabinet.

## 2. Themes (user-selectable, 2 token sets)

Everything is built on semantic CSS custom properties; the theme swaps token values only.
Default follows `prefers-color-scheme`; a visible toggle persists the override (localStorage).

- **Light — "Greenhouse warm" (direction A):** sand/paper neutrals (#f7f2e7 / #fbf6ea),
  deep botanical green accent (#4a6741), rust for out-of-range (#b5502e), warm ink text
  (#3a3220).
- **Dark — "Nocturnal conservatory" (direction B):** near-black warm ground (#14140f /
  #1c1c14), parchment text (#f2e9d3), brass accent with a soft glow (#c99a3f, glow via
  box-shadow), ember for out-of-range (#d9714a).

Rejected theme directions (rendered as full tiles): glasshouse light/cool, deep-botanical
hybrid.

## 3. Typography

**Editorial serif + humanist sans.**
- Display/plant names/reading values: **Fraunces** (500; italic 400 for species binomials).
- Labels/UI/data captions: **Work Sans** (400–600; uppercase labels with 0.06–0.08em
  letter-spacing).
- Self-hosted via `@font-face` (no external requests from the self-hosted app).

Scale rhythm from the approved mocks: tile name 26px, detail name 44px, metric value 20px
serif with an 11px sans unit, labels 10–12px uppercase sans.

## 4. Palette & tokens

See §2 for the two theme palettes. Token families to define in `tokens.css`: colour
(surface/ink/accent/warn/track/range-band), spacing (4-based scale), radii (4/6/8px +
pill), borders, shadows (one soft lift + dark-theme glow), photo-treatment parameters
(vignette opacity, wash colour), duration/easing (§8), breakpoints (§9).

## 5. Tile composition (overview grid)

**Controlled asymmetric editorial grid** (approved against a uniform product grid and an
aspect-ratio masonry, both rendered):

- CSS Grid, fixed span patterns — NOT random/JS masonry.
- One deliberately **featured tile** (largest) per view; remaining tiles follow a fixed
  rhythm of 1×1 / 2×1 / 1×2 spans.
- Tile anatomy: treated photo fills the tile; bottom gradient seats the caption — plant
  name (serif) over location · sensor (uppercase sans). Two compact metric bars below the
  caption on larger tiles (moisture + worst-of-the-rest); an out-of-range metric shows a
  rust dot + label.
- Only sensors with an **active plant assignment** get tiles. Unassigned sensors are
  invisible (per product spec).
- Attention state: subtle edge treatment (thin rust rule on the tile's edge), not a
  badge/icon overlay.
- **Photo:dead-space ratio ≥ ~3** (user rule, Phase 1): in any blur-fill composition the
  subject(s) must cover at least ~75% of the tile. Consequences:
  - **Span assignment is photo-aware.** Wide (2×1) and single-subject featured spans are
    reserved for landscape photos; portrait photos get 1×1 / tall (1×2) spans.
  - **Featured (2×2) with portrait corpus = duo tile**: two plants share one blur canvas
    (each a ~0.8-aspect, upper-biased crop, height-fit, side by side — covers ~98%).
    Each half is its own tap target with its own caption. On phone the duo splits into
    two independent portrait halves.

## 6. Photographic treatment

**"Strong grade + soft vignette"** — validated on a real plant photo with the actual production tool (sharp):

- Grade: saturation ×1.6, warm channel curve (`linear([1.22,1.05,0.9], [-18,-10,-4])`),
  gamma 1.12, warm amber wash at ~16% soft-light.
- Vignette: radial, starts at ~58% radius, peaks at **~45% opacity** (user explicitly
  softened it from an 85% version — do not re-darken). Bottom caption gradient ~55% peak.
- **Tile variant**: cover-crop to the tile aspect (portrait bias `center 55%` toward the
  plant body). **Hero variant (detail view): never crop — contain the full photo and fill
  the canvas with a blurred (σ≈28), darkened (×0.55) echo of the same photo.**

Findings baked into these rules (both demonstrated live during brainstorming):
- **rembg/ML background removal FAILS on spiky/feathery foliage** (erased ~93% of the test
  Dracaena) — full cutout is banned; the soft blur-fill/vignette achieves the "abstracted
  specimen" feel safely.
- **sharp's `attention` crop is unreliable** (locked onto bark mulch, cropped out the
  plant on a landscape crop) — never use auto-attention for wide crops; contain+blur-fill
  instead.

## 7. Object-detail composition

**Option B v2 (approved): full-bleed blur-fill canvas.**
- The whole treated photo, contained, seated left-of-centre on its blurred echo.
- Measurement panel (~340px) floats right, **over the blur, never over the plant**:
  all four metrics, each as label / serif value / range-band track / dot / min-max
  numerals; 48h sparkline under soil moisture.
- Caption bottom-left over the photo: name (serif 44px), species (italic), location ·
  sensor · freshness.
- **📷 "Update photo"** pill top-left (§10).
- Phone: photo on top (blur-fill still), panel content stacks below.

## 8. Motion language

Calm and physical; one shared easing; durations 200–450ms. Approved set:
1. **Tile → detail**: shared-element expansion (Motion for React `layoutId`) — the tile
   photo becomes the detail canvas; panel fades/slides in after ~150ms; back reverses.
2. **Filter/reorder**: tiles glide to new grid positions (layout animation).
3. **Reading reveal**: on detail open, each metric dot slides from range-edge to value
   once (~400ms, slight stagger). Never loops.
4. **Hover/press**: 2–3px lift + ~4% photo brighten.
5. `prefers-reduced-motion`: everything becomes instant cuts/crossfades.

Banned: parallax, idle/floating loops, scroll-driven effects, intro sequences.

## 9. Desktop / tablet / mobile behaviour

Primary display is a **12" Android tablet, landscape (~1280px)** — must also work
portrait, plus desktop and phone:
- **≥1100px (primary)**: 4-col asymmetric grid.
- **~700–1100px** (tablet portrait): 2-col; featured tile goes full-width.
- **<700px** (phone): single column; featured tile taller; tile metric bars collapse to a
  one-line status in the caption (full bars live in detail view).
- Container queries for tile-internal layout; the grid reflows with deliberate span rules
  per breakpoint (verified visually — reshapes well on very wide screens too).

## 10. Photo upload (new scope, approved)

- **Where**: detail view, "📷 Update photo" pill (not on grid tiles — keeps overview clean).
- **Flow**: upload → new `identify_photos` row with `plant_id` + file at
  `YYYY-MM-DD/<uuid>.jpg` (same corpus/layout as the Telegram bot) → newest-photo query
  picks it up → treatment runs on-demand at first view (new uuid = new derivative).
- **Infra consequence**: canopy's photo mount must be **read-write** (the bot's layout is
  the write template); derivative cache stays on its own volume.
- Upload dialog later gets the photo-guidelines crib (below).

### Photo guidelines (for the user taking new "appeal" shots)
Whole plant + pot with ~10–15% breathing room; camera level with the plant's midpoint
(not looking down); portrait for tall plants, landscape for wide/trailing; window daylight
from the side/behind the photographer (never backlit); calm-ish background directly behind
foliage; no flash, no extreme close-ups, don't cut the pot off.

## 11. Data-visualization language

No generic charts/gauges/KPI cards. The one shared measurement primitive (used at both
tile and detail scale):
- Track = full sensor scale; darker **band** = ideal threshold range; **dot** = current
  reading (green/brass in range, rust/ember out); threshold numerals at track ends;
  out-of-range states also say it in words ("above ideal range") — never colour-only.
- 48h **sparkline** (from the hourly continuous aggregates) as an annotation under the
  primary metric, sharing the track's width — not a standalone chart.
- d3-scale/d3-interpolate for the math only; all rendering is bespoke inline SVG/DOM.

## 12. Anti-patterns (explicit, from spec + session)

Generic SaaS dashboard / admin template / ecommerce product grid; uniform tile grid;
arbitrary bento sizing; unstable JS masonry; default shadcn styling; glassmorphism;
gradient blobs; gauges; placeholder charts; nested cards; oversized heroes; hover-only
essential info; fixed/min-height dead space; ML background cutouts (§6); auto-attention
wide crops (§6); heavy vignette (>~45%); idle animation; colour-only status.

## 13. Naming

Product name **Canopy**; service directory `services/canopy-ui/`, container `cp-canopy-ui`.
Runner-up candidates considered: verdant, conservatory, greenhouse, atrium, flora,
herbarium, orangerie.
