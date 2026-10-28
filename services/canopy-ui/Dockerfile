# Canopy — multi-stage build (frontend + backend)
# Build context: services/canopy/

# ── Stage 1: Build ────────────────────────────────────────────────────────────
FROM node:22-slim AS build

WORKDIR /build

# Build frontend
COPY frontend/package.json frontend/package-lock.json* ./frontend/
RUN cd frontend && npm ci
COPY frontend/ ./frontend/
RUN cd frontend && npm run build

# Build backend (tsc only needs devDeps; runtime deps are installed on
# alpine below so sharp gets the musl binding, not this stage's glibc one)
COPY backend/package.json backend/package-lock.json* ./backend/
RUN cd backend && npm ci
COPY backend/ ./backend/
RUN cd backend && npm run build

# ── Stage 1b: Runtime deps on musl ───────────────────────────────────────────
# sharp ships per-libc prebuilt bindings; install prod deps on the SAME base
# as the runtime image (alpine/musl) or sharp fails to load there.
FROM node:22-alpine AS deps
WORKDIR /deps
COPY backend/package.json backend/package-lock.json* ./
RUN npm ci --omit=dev

# ── Stage 1c: Fonts for server-side SVG text ─────────────────────────────────
# sharp renders the alert card's text through librsvg → fontconfig, which
# cannot read woff2. Decompress the app's own self-hosted webfonts to ttf so
# the card uses the same Fraunces/Work Sans the dashboard does (rather than a
# generic fallback, or a second copy of the fonts checked into the repo).
FROM node:22-alpine AS fonts
RUN apk add --no-cache woff2
COPY frontend/public/fonts/ /woff2/
RUN mkdir -p /ttf && cd /woff2 && for f in *.woff2; do woff2_decompress "$f"; done \
    && mv *.ttf /ttf/

# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM node:22-alpine AS runtime

RUN apk add --no-cache fontconfig
COPY --from=fonts /ttf/ /usr/share/fonts/canopy/
RUN fc-cache -f

# node:*-alpine already ships a non-root `node` user at uid/gid 1000 — reuse it
# rather than creating a second uid-1000 user (which fails: 1000 is taken).
# docker-compose overrides the runtime uid with the host user (HOST_UID/HOST_GID)
# so the identify-photos bind mount is writable.

WORKDIR /app

# Copy backend dist + production deps
COPY --from=build /build/backend/dist ./server/
COPY --from=build /build/backend/package.json ./server/
COPY --from=deps /deps/node_modules ./server/node_modules/

# Copy frontend dist
COPY --from=build /build/frontend/dist ./client/

ENV FRONTEND_DIST=/app/client
ENV NODE_ENV=production
ENV PORT=8080

USER node
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/api/health || exit 1

CMD ["node", "/app/server/index.js"]
