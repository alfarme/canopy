# Canopy

Photography-led plant dashboard: one arm64 container serving a React SPA and
Fastify API. Served on port 8090.

- **Design contract:** `docs/research/canopy-design-brief.md` (repo root) — themes,
  grid, photo treatment recipes, data-viz rules.
- **Backend** (`backend/`): plants API (readings vs thresholds, 48h hourly history),
  photo variants generated on demand by sharp (tile/hero/duo, warm grade + vignette)
  into a permanent cache with boot/upload pre-warming, JPEG upload endpoint that
  mirrors the Telegram bot's storage layout.
- **Frontend** (`frontend/`): Vite + React 19, TanStack Query, d3-scale + bespoke
  SVG measurement primitive, Motion shared-element tile→detail morph, pinch-in on
  the hero to go back, self-hosted Fraunces/Work Sans, light/dark via CSS tokens.

## Env

| Var | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | — | Postgres/TimescaleDB connection |
| `PORT` | `8080` | listen port (published as 8090) |
| `PHOTO_BASE_PATH` | `/data/photos` | identify-photos tree (RW: uploads write here) |
| `PHOTO_CACHE_PATH` | `/data/photo-cache` | derivative cache (persistent, safe to wipe) |
| `FRONTEND_DIST` | `/app/client` | built SPA served by the backend |

## Dev

```
cd backend  && npm ci && npm run build   # tsc → dist/
cd frontend && npm ci && npm run dev     # Vite on :5173, proxies /api + /photos → :8080
```

Local test recipe: `postgres:16-alpine` + `compose/initdb/*.sql` (timescaledb
extension errors are harmless — only readings hypertables are affected), point
`PHOTO_BASE_PATH` at any `YYYY-MM-DD/<uuid>.jpg` tree.

Deploy: part of the main stack — `./deploy.sh` from the repo root.
Container runs as uid 999 to match the shared `identify-photos` ownership.
