# Canopy — Technical Reference

A private smart-garden monitoring platform: Xiaomi MiFlora BLE soil sensors + MQTT +
TimescaleDB + Grafana + a Telegram bot + a custom React/Fastify photo dashboard
("Canopy"), deployed via Docker Compose to a single Raspberry Pi. This document
condenses the project's design/build/ops history into one reference sufficient to
understand the system and rebuild it from scratch.

---

## 1. System overview

```
MiFlora BLE sensors (passive advertisements)
        │
        ▼
Theengs Gateway (same Pi as everything else, --net=host --privileged)
        │  MQTT: canopy/ble/<MAC>
        ▼
  ingest service
        │
        ▼
  TimescaleDB (Postgres + hypertables)
        │
   ┌────┼────────────────────┐
   ▼    ▼                    ▼
decision-engine  Grafana              Canopy UI (React/Fastify,
(digests)       (dashboards +         photo dashboard, port 8090)
   │            sensor-health
   ▼             alerting)
Telegram bot
(digests, photo-triggered
 plant identification)
```

All services run as Docker containers on a single Raspberry Pi via `docker compose`,
built natively for arm64 on the Pi itself — no cross-arch emulation, no registry.

GARDENA smart-device cloud integration (soil/valve sensors via the Husqvarna OAuth2 API)
was part of the original design and is described in §2 below for historical/rebuild
reference, but was removed entirely before this repo's Canopy rename — it never shipped
in production and added OAuth2/websocket complexity for no retained benefit. The system
is MiFlora-only.

---

## 2. GARDENA cloud integration (REMOVED — not shipped)

This section is kept verbatim as reference in case GARDENA integration is revisited; none
of it reflects current behavior.

### Auth

- OAuth2 **client_credentials** grant: `POST https://api.authentication.husqvarnagroup.dev/v1/oauth2/token`,
  form-encoded `grant_type=client_credentials&client_id=...&client_secret=...`.
- Response includes `access_token`, `expires_in` (3600s), `scope`, `token_type`. **There is
  no refresh token for this grant type.** The correct strategy is simply to re-authenticate
  whenever a call returns 401 (invalid token) or 404 (expired token) — do not try to
  pre-emptively refresh on a timer alone.
- Every API call requires two headers: `Authorization: Bearer <token>` and
  `X-Api-Key: <app_key>`. The `Authorization-Provider: husqvarna` header some older sample
  code includes is **no longer required** (removed from the API 2022-03-14) — do not add it.
- Limits: max 3 simultaneous access tokens per user+app; 10 invalid auth attempts in 5 min
  triggers a 30-minute lockout.

### REST

- Rate limits: 10 requests/10s, 700 requests/week (429 on exceed). Recommended average is
  ~1 call/15min. `GET /health` is unmetered — use it for uptime/liveness checks, not the
  metered endpoints.
- **SENSOR** service schema (soil probes): `soilHumidity` (%), `soilTemperature` (°C),
  `ambientTemperature` (°C), `lightIntensity` (lux). Exactly these 4 fields, nothing else.
- **COMMON** service schema (all devices): `name`, `batteryLevel` (%), `batteryState`
  (OK/LOW/REPLACE_NOW/OUT_OF_OPERATION/CHARGING/NO_BATTERY/UNKNOWN), `rfLinkLevel` (%, absent
  on Sileno Sense mowers), `serial`, `modelType`, `rfLinkState` (ONLINE/OFFLINE/UNKNOWN).
- **VALVE control**: `PUT /v1/command/{service_id}` (note: `service_id` ≠ `device_id` — find
  it via the device's `relationships.services.data`).
  - `VALVE_CONTROL` commands: `START_SECONDS_TO_OVERRIDE` (seconds must be a positive
    multiple of 60, range 1–36000), `STOP_UNTIL_NEXT_TASK`, `PAUSE`, `UNPAUSE`.
  - `VALVE_SET_CONTROL` only supports `STOP_UNTIL_NEXT_TASK` (closes all valves on the set).
  - At most **2 valves may be open simultaneously** — a 3rd start attempt returns
    `CONCURRENT_LIMIT_REACHED`.
  - A 202 response only confirms the Gateway *received* the command; you must poll device
    state afterward to confirm it actually executed.
  - VALVE monitoring fields: `activity` (CLOSED/MANUAL_WATERING/SCHEDULED_WATERING), `state`
    (OK/WARNING/ERROR/UNAVAILABLE), `lastErrorCode` (large enum incl. `VALVE_BROKEN`,
    `FROST_PREVENTS_STARTING`, `LOW_BATTERY_PREVENTS_STARTING`), `duration`.

### WebSocket (live event stream)

- `POST /v1/websocket` with a JSON:API body containing `locationId` returns a pre-signed WSS
  URL. That URL is only valid to *connect* for ~30 seconds after issuance.
- Connection max lifetime is ~2 hours (server closes with code 1001); it also auto-closes
  after ~10 minutes of idle (no events).
- Recommended keep-alive: ping every ~2.5 minutes (`ping_interval=150s` in the reference
  client; GARDENA's own OpenAPI docs suggest 5 min).
- Reconnect protocol: re-fetch location → reset all internal/local state → request a fresh
  WS URL → open → process incoming events as JSON:API PATCH documents → on any terminal
  close, go back to the "reset state" step (don't just reopen with stale local state).
- Event payload document types: `SENSOR`, `VALVE`, `VALVE_SET`, `POWER_SOCKET`, `MOWER`,
  `COMMON`.

### Known unverified areas (flagged in original research, not independently confirmed)
Token-endpoint per-second rate limit; concurrent-WebSocket-connections-per-app limit; exact
behavior of a `seconds` parameter on `PAUSE`; WebSocket message rate limit.

---

## 3. MiFlora BLE sensor integration

### Hardware & protocol

Xiaomi/HHCC `HHCCJCY01` "Flower Care" soil sensors. Two access modes:

- **Active GATT connect** (handle-based reads) — required only to read **battery level**
  (handle `0x38`, byte 0 = battery %). The canonical `miflora` Python library reads this at
  most once per 24h to conserve the sensor's coin cell. Full sensor data lives at handle
  `0x35` (16 bytes, little-endian: bytes 0–1 = temp ×10, bytes 3–6 = lux uint32, byte 7 =
  soil moisture %, bytes 8–9 = conductivity µS/cm), but this is **not** how the primary
  pipeline reads it — see below.
- **Passive BLE advertisements** (firmware 3.x) — the sensor broadcasts using Xiaomi service
  UUID `0xFE95`; each single advertisement carries only *one* metric (temp/moisture/
  lux/fertility, selected by a condition byte), rotating through all four over time.
  Listening passively costs the sensor **zero additional battery** beyond what it broadcasts
  anyway, unlike active polling.

This active-vs-passive split is why the **primary sensor pipeline uses passive scanning**
(via Theengs Gateway) for temp/moisture/lux/fertility, and battery is collected separately
on its own, much slower cadence via a dedicated active-connect step (see §5, battery
cadence — the shipped value is 48 hours).

### Collector: Theengs Gateway (not bt-mqtt-gateway, not a custom script)

Theengs Gateway was chosen over the alternatives evaluated during design:
- `bt-mqtt-gateway` — rejected: effectively unmaintained, active-connect-only via the
  unmaintained `bluepy` library, unreliable on modern Python/Raspberry Pi OS Bookworm, no
  auto-discovery.
- A custom `python-bleak` script — rejected as the primary path (full protocol control but
  full maintenance burden); kept in mind only as a DIY fallback.
- **Theengs Gateway** — actively maintained, native passive-scan support, first-class
  HHCCJCY01 decoder, simple pip/Docker/systemd install, built-in Home Assistant MQTT
  discovery (not used here, but harmless).

Runs as an ordinary `docker compose` service on the **same single Raspberry Pi** as every
other container (an earlier design used a separate dedicated Pi for BLE range; re-evaluate
placement if BLE range becomes a real issue on your deployment). Real-world indoor BLE range
is ~5–10m through walls (not the theoretical 30m open-air figure); a fleet spread across
multiple rooms may need a second collector if a sensor's RSSI consistently sits below −90 dBm.

**Required container flags** — `--net=host --privileged` AND a **bind-mounted D-Bus socket**
(`-v /var/run/dbus:/var/run/dbus`). This last mount is easy to miss and causes a completely
silent failure: the gateway connects to MQTT and "starts scanning" successfully in its logs,
but never decodes a single advertisement, because Bleak (the BLE library Theengs uses)
talks to `bluetoothd` over D-Bus and the container has no D-Bus socket to talk over without
this mount. Symptom: `docker ps` looks healthy, logs show "Starting BLE scan" / "Connected to
MQTT broker", but zero MQTT payloads ever appear even with a sensor at 20cm. Diagnose by
running `BleakScanner.discover()` directly inside the container — it throws
`FileNotFoundError` on `/var/run/dbus/system_bus_socket` if the mount is missing.

**Host networking vs. Mosquitto**: `network_mode: host` means Theengs cannot use compose
bridge-network service-name DNS to reach Mosquitto. Mosquitto stays on the default bridge
network with `1883:1883` published to the host, and Theengs is pointed at
`MQTT_HOST=127.0.0.1` (a `.env` var, overridable to the Pi's LAN IP as an escape hatch if
the loopback-hairpin path doesn't work on some future kernel/nftables config) — Docker's
DNAT for published ports applies to traffic from the host's own network namespace, which is
what a `network_mode: host` container shares.

Shipped Pi collector env vars (see `compose/docker-compose.yml`, verified against
`gateway.theengs.io/install/docker/` for the pinned `theengs/gateway:v1.7.5.1` image):
`MQTT_HOST`, `MQTT_PUBLISH_TOPIC` (not `PUBLISH_TOPIC` — an earlier design draft used the
wrong name), `SCANNING_MODE=passive`, `BLE_SCAN_TIME` (not `SCAN_DURATION`) and
`BLE_TIME_BETWEEN_SCANS` (not `TIME_BETWEEN`) — the image documents no `MQTT_PORT` env var,
so the stack relies on Mosquitto's default 1883. Defaults: `BLE_SCAN_TIME=10`,
`BLE_TIME_BETWEEN_SCANS=50` (a 10s-scan/50s-pause duty cycle).

MQTT topic: `canopy/ble/<MAC>` (Theengs appends the MAC to `MQTT_PUBLISH_TOPIC`). Payload
JSON fields: `name`, `id`, `rssi`, `brand`, `model`, `model_id`, `tempc`, `moi`, `lux`, `fer`.

---

## 4. Database — TimescaleDB

- Image: `timescale/timescaledb:2.28.2-pg17` (slim variant; sufficient for this workload —
  no PostGIS/WAL-G needed). Always pin the exact tag, never `latest-pg17`, for
  reproducibility.
- PG15-based TimescaleDB tags are deprecated (support ends with the 2.28.x series) — do not
  use them for new deployments.
- **Current (2.18+) compression API naming is "columnstore," not "compress"** — the old
  names (`compress_chunk()`, `add_compression_policy()`, `timescaledb.compress`, etc.) still
  work as aliases in 2.28.x but are scheduled for eventual removal in 3.0. New SQL should use
  `convert_to_columnstore()`, `add_columnstore_policy()`,
  `timescaledb.enable_columnstore = true`. The underlying "Hypercore" table-access-method
  experiment (2.18) was fully removed in 2.22 — don't reference it as current.
- Continuous aggregates: standard `CREATE MATERIALIZED VIEW ... WITH (timescaledb.continuous)`
  + `add_continuous_aggregate_policy(...)`; a 1-hour rollup with hourly refresh is the
  pattern used for the Canopy dashboard's 48h sparklines.
- Backup: **`tools/backup_db.sh` via a daily host crontab entry** (`pg_dump -Fc` inside the
  `cp-db` container, output landing on the host through the `/backups` bind mount), not
  WAL-G (overkill for this data volume) and not bare filesystem snapshots as the *sole*
  backup — a snapshot taken mid-write of a live Postgres data directory can capture an
  inconsistent mix of WAL and data-file state; a filesystem/volume snapshot is a secondary
  safety net only, never the primary backup path. Keep the last 14 daily dumps (the script
  prunes automatically); it also verifies each dump file is non-empty before pruning.
  `CREATE EXTENSION IF NOT EXISTS timescaledb;` must exist before `pg_restore` runs; always
  do a full dump/restore (never schema-only + data-only separately — hypertable metadata
  ordering matters).
- Deployment paths: always use explicit `$DATA_DIR/...` bind mounts for Postgres data (see
  `.env.example`), never Docker named volumes (named volumes are harder to inspect/back up
  directly from the host filesystem).
- `restart: unless-stopped` (not `always`) on every service, so a deliberate `docker stop`
  during maintenance is respected across a host reboot.

---

## 5. Decision engine — alerting

The alerting design went through several iterations; only the final, shipped behavior
matters operationally:

- **Battery-check cadence: 48 hours**, with the low-battery rule (`cp_low_battery`) widened
  to a 14-day / 2-sample window to match. This was reached via a sequence of tightenings
  (daily → 2h → 6h → 48h) as the operational cost of frequent active-GATT battery reads
  became clear; "monthly" was floated independently in three early design/research
  documents as a starting proposal but was **never shipped** — do not treat it as current
  behavior.
- **Light out-of-range alerting: sustained-only, 3-day window.** Light was originally
  excluded entirely from "needs attention" (too noisy — day/night cycles constantly cross
  any fixed threshold), then, later, changed to only fire when *every* reading across a
  full rolling 3-day window is out of range — mirroring the existing
  `light_lux_sustained_oor` pattern already used for other sustained conditions. A single
  in-range daytime reading resets the streak. Treat "light excluded from needs-attention" as
  a superseded interim rule, not current behavior.
- **`out_of_range` was split into 7 distinct alert kinds** (per-metric/per-direction, e.g.
  moisture-low, moisture-high, temperature-low, etc.) rather than one generic
  "out_of_range" bucket, to make Grafana panels and digest messages specific.
- **Alert delivery**: two different mechanisms for two different alert families —
  - Plant-state conditions (moisture/temperature/light/fertility out-of-range, low battery,
    etc.) are delivered as **scheduled Telegram digests** (a batched summary message sent on
    a schedule), not realtime per-condition pings. This replaced an earlier realtime,
    fire-immediately-per-condition design, which produced alert-burst spam whenever the
    decision-engine restarted (see below).
  - Sensor/infra-health conditions (device offline, stale data, etc.) are delivered via
    **Grafana Alerting**, separately from the Telegram digest path.
- Cooldown/dedup state (`within_cooldown()`) is derived from the persisted `alert_events`
  table at startup, **not** kept only in an in-memory dict — an in-memory-only cooldown was
  the root cause of a restart-triggered alert-spam bug (every restart forgot all cooldowns
  and re-fired everything at once).
- Alert severity ranking for UI sorting ("water always wins"): moisture (3) > temperature
  (2) > light/fertility (1).

---

## 6. Plant identification & care thresholds

### Species identification: Pl@ntNet (not Plant.id, not a self-hosted model)

Chosen over Plant.id/Kindwise (no real ongoing free tier — 100 one-time test credits, then
paid) and a self-hosted ViT model (kept only as a hypothetical offline fallback, never
built). Pl@ntNet gives 500 free identifications/day (resets 00:00 UTC), accepts 1–5 images
per request tagged with an `organ` (flower/leaf/fruit/bark/habit/auto), and returns a
`scientificNameWithoutAuthor` binomial directly usable as an OpenPlantbook lookup key.
Non-plant photos return a clean HTTP 404 (no hallucinated species). A 30-species live
accuracy test measured **96.7% top-1 genus-level accuracy**, ~0.5s average latency, and
confirmed the 404-on-non-plant behavior — production-ready for this project's volume.
Requires a "Powered by Pl@ntNet" attribution line in the bot UI.

### Care thresholds: OpenPlantbook

OAuth2 client_credentials against `open.plantbook.io`. **Correct env var names are
`OPENPLANTBOOK_CLIENT_ID` / `OPENPLANTBOOK_CLIENT_SECRET`** — an earlier draft used a single
`OPENPLANTBOOK_TOKEN` name, which does not match the actual two-credential client_credentials
flow; don't reintroduce it. All external-service secrets (OpenPlantbook, PlantNet, Telegram
bot token) are supplied via `env_file`, never inlined under a compose service's
`environment:` block, to keep them out of `compose/docker-compose.yml` (which is tracked in
git).

- Search: `GET /api/v1/plant/search?alias=NAME&limit=N` (case-insensitive, matches display
  name, alias, and multilingual common names).
- Detail: `GET /api/v1/plant/detail/{pid}/` (`?include=care` for text guidance) — returns
  min/max soil moisture %, soil EC (µS/cm), light (lux), and temperature (°C), all in units
  directly compatible with the MiFlora sensor readings. A `0` value in `min/max_soil_ec`
  means "unset," not a real threshold — filter those out before applying.
  Take the top hit's `pid` from a `search` call — **never guess a `pid` by lowercasing the
  scientific name** (subspecies/cultivars/hybrid binomials don't always map cleanly, and
  OpenPlantbook 301-redirects `/plant/detail/<pid>` to a trailing-slash URL that some HTTP
  clients won't auto-follow without configuration).
- Unresolved plants fall back to conservative indoor-default thresholds (moisture 15–60%, EC
  100–2000 µS/cm, light 500–20000 lux, temp 10–35°C) and are flagged with
  `threshold_source = 'default'` so the UI can indicate the value is a guess.

### Telegram bot photo-ID flow — CURRENT behavior

**There is no `/identify` or `/id` command anymore.** Both were removed entirely; any photo
an admin sends to the bot **automatically triggers** PlantNet identification via a single
`MessageHandler(filters.PHOTO, handle_photo)`. Do not describe the old explicit-command
design as current — it was the original design but was superseded.

Other durable fixes/gotchas from building this flow (still relevant even though the command
itself changed): PlantNet responses carry no per-result reference image, so the bot uses a
Wikipedia thumbnail lookup for a visual confirmation; Telegram **group privacy mode** must be
disabled via BotFather or the bot silently never sees plain-text group replies (a platform
behavior, not a bug); a multipart `data` payload must be a `Mapping`, not a list of tuples,
or httpx silently skips multipart encoding; `conversation_timeout` requires the
`python-telegram-bot[job-queue]` extra installed or it is silently a no-op; httpx/httpcore
log full request URLs (including the bot token embedded in Telegram's URL path) at INFO —
force those loggers to WARNING unconditionally to avoid leaking the token into container
logs.

---

## 7. Canopy — photo dashboard

A custom React/Fastify app (not Grafana) for a warm, editorial "greenhouse/conservatory"
photo-forward plant browser, served on port 8090. "Canopy" is the product name (unchanged
by the project rename); its service directory is `services/canopy-ui` (renamed from
`services/canopy` to avoid colliding with the project's own new name, "Canopy"). Predecessor
design "Object Atlas" was rejected on visual grounds before Canopy's design was approved
and is out of scope here.

Key shipped decisions:
- Photo treatment: "strong grade + soft vignette" via `sharp`. Tile crops use `cover` with a
  portrait-biased center; **the detail/hero view never crops** — it contains the full photo
  and fills the remaining canvas with a blurred, darkened echo of the same image.
  `sharp`'s `attention`-based auto-crop and ML background removal (`rembg`) were both tried
  and rejected: `attention` locked onto background clutter (e.g. bark mulch) instead of the
  plant on wide crops, and `rembg` erased ~93% of a spiky/feathery test plant. Neither is
  used anywhere in the pipeline.
- All photo uploads must call sharp's `.rotate()` — an early version didn't, so phone photos
  taken in portrait orientation (EXIF orientation tag 6) rendered 90° off; crop math for the
  side-by-side "duo tile" layout also needs a width/height swap when EXIF orientation ≥ 5.
- Location grouping uses client-side fuzzy (Levenshtein ≤2) matching of the free-text
  `location` column into a fixed display order (Terrace → Upstairs → Living Room → Kitchen →
  Elsewhere), not a rigid enum column.
- `/plants` and `/sensors` listing order uses `ORDER BY LENGTH(name), name` to sort
  numeric-string names correctly — a regex/cast-based alternative (`~ '^[0-9]+$'::int`) was
  considered but rejected because it silently behaves differently between real Postgres and
  the test suite's SQLite shim.
- Photo upload is read-write from Canopy directly onto the same corpus/layout the Telegram
  bot writes to (`YYYY-MM-DD/<uuid>.jpg`); a scroll-position bug after route transitions
  requires pinning scroll position for ~1.2s post-navigation because the route transition
  transiently collapses document height.
- Sustained light-OOR display logic mirrors the decision-engine's `light_lux_sustained_oor`
  rule (§5) — a full 3-day all-readings-OOR window, not the earlier interim "exclude light
  entirely" rule.

---

## 8. Deployment & operations (single Raspberry Pi)

- **`install.sh` is the one-shot bring-up path** for a fresh Pi:
  `curl -fsSL https://raw.githubusercontent.com/alfarme/canopy/main/install.sh | bash`.
  It's idempotent and re-runnable by design: the first run clones the repo and scaffolds
  `.env` from `.env.example`, then stops and asks you to fill in the required Telegram
  values; the second (and every later) run clones/pulls, validates `.env`, generates
  `POSTGRES_PASSWORD`/`GF_SECURITY_ADMIN_PASSWORD` if left blank (persisting them back into
  `.env` so reruns are stable), splits `.env` into the per-container files under
  `$DATA_DIR/env/*.env`, sets up `$DATA_DIR`, installs the daily backup cron job, and runs
  `./deploy.sh`. Because it runs under `curl | bash`, stdin is the script itself — it never
  prompts interactively; anything missing is a stop-and-ask-you-to-edit-`.env`-and-rerun,
  not a terminal read.
- **`.env` is regenerated into `$DATA_DIR/env/*.env` on every `install.sh` run** — it is the
  single source of truth; don't hand-edit the generated per-container files, they get
  overwritten. `HOST_UID`/`HOST_GID` get appended to `.env` automatically (not part of
  the user-facing template) so Grafana's container runs as the deploying host user rather
  than its image-default uid 472, which otherwise can't write to a bind-mounted
  `$DATA_DIR/grafana` owned by a non-root Pi user — a non-root user can't `chown` a
  directory to an arbitrary uid to fix that after the fact, so it has to be solved this way,
  on the compose side, not by `chown`ing in `install.sh`.
- **Grafana's own Telegram alerting contact point needs `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID` in `$DATA_DIR/env/grafana.env` too** — the same credentials as
  `telegram.env`, but Grafana reads its own copy (`compose/grafana/provisioning/alerting/contact-points.yaml`).
  Missing either one is fatal for Grafana specifically (unlike telegram-bot, which just fails
  its own auth check and crash-loops on its own): Grafana's alerting provisioning refuses to
  finish starting at all if the contact point fails to validate.
- Images are built natively for arm64 directly on the Pi via `docker compose build` — no
  cross-arch buildx/QEMU emulation, no registry push/pull. This is a deliberate
  simplification: a single Pi building its own images needs no build host or registry —
  `docker compose build && docker compose up -d` is sufficient.
- **All persistent state lives under `$DATA_DIR`** (set in `.env`, copied from
  `.env.example`): `pgdata`, `backups`, `mosquitto/{config,data,log}`, `grafana`,
  `identify-photos`, `canopy-cache`, `deploy-history/`, `env/*.env`. Secrets (DB passwords,
  API keys, bot tokens) live in `$DATA_DIR/env/*.env`, untracked by git — never inlined
  under a compose service's `environment:` block.
- **`deploy.sh` builds the changed services, recreates the stack, snapshots pre-deploy image
  IDs to `$DATA_DIR/deploy-history/`, then health-checks every container** (`cp-mosquitto
  cp-db cp-ingest cp-decision cp-telegram cp-canopy-ui cp-grafana cp-theengs`), restarting
  Grafana explicitly afterward (its config is bind-mounted, so `up -d` alone won't pick up
  provisioning changes). There is no `--to <tag>` rollback mode any more — with no registry,
  rollback is `git checkout <sha> -- .` followed by a rebuild.
- Theengs Gateway needs `network_mode: host` for BLE (see §3), which is why it talks to
  Mosquitto over `127.0.0.1` against a published port rather than compose bridge-network DNS
  — the one real topology wrinkle in an otherwise ordinary single-host compose stack.
- **Bare-metal tools unchanged in kind**: `tools/miflora_battery.sh` / `_listener.sh` /
  `.service` still run on the Pi's host (cron+systemd, need `gatttool`/`bluetoothctl`), not
  containerized — they talk to Mosquitto on `127.0.0.1` (no longer a hardcoded LAN IP) and
  use the renamed `canopy/ble`, `canopy/cmd/battery` topics.
- `restart: unless-stopped` everywhere (see §4).

---

## 9. Setup instructions (from scratch)

**The short version**: on the target machine run
`curl -fsSL https://raw.githubusercontent.com/alfarme/canopy/main/install.sh | bash` and answer
the prompts (data directory, optional port changes, Telegram bot token / user id / chat id).
`install.sh` handles everything from step 3 onward below; the optional API keys (step 2) are
added to `.env` afterwards, followed by `~/canopy/install.sh`. The long version, for
understanding what it's doing or for doing it by hand:

1. **Provision the host**: a Raspberry Pi (or similar SBC) with Docker + Docker Compose,
   Bluetooth working (`systemctl is-active bluetooth`), and the deploying user in the
   `docker` group. `install.sh` installs `git` via apt and, with your consent, Docker via
   Docker's official script (then asks you to log out and back in for the group to apply).
   It only warns about an inactive Bluetooth service.
2. **Register external API credentials**:
   - OpenPlantbook: register at `open.plantbook.io`, generate `OPENPLANTBOOK_CLIENT_ID` /
     `OPENPLANTBOOK_CLIENT_SECRET`.
   - Pl@ntNet: register a free account at `my.plantnet.org` for an API key (500 req/day).
   - Telegram: create a bot via BotFather; **disable Group Privacy mode** if the bot needs to
     read plain-text messages in a group chat; get your own numeric user id from
     @userinfobot for `TELEGRAM_ADMIN_USER_ID`.
   Put all of these into the single root `.env` (`install.sh` scaffolds it from
   `.env.example` on first run) — never inline them under a compose service's
   `environment:` block, and never hand-edit the generated `$DATA_DIR/env/*.env` files
   directly (see §8).
3. **Copy `.env.example` to `.env`** at the repo root and fill it in (`install.sh` does the
   copy for you on first run).
4. **Deploy TimescaleDB**: `timescale/timescaledb:2.28.2-pg17` (or a newer pinned pg17/pg18
   tag), with the schema migrations applied on first boot via `compose/initdb/`.
5. **Bring up Mosquitto and Theengs Gateway** as ordinary compose services (see
   `compose/docker-compose.yml`) — Theengs needs `network_mode: host`, `privileged: true`,
   and the D-Bus bind mount (§3); do not skip the D-Bus mount.
6. **`docker compose build && docker compose up -d`** from the repo root (or `./deploy.sh`,
   which wraps the same build/up/health-check sequence, or `install.sh`, which wraps
   `deploy.sh`).
7. **Deploy Grafana** with provisioned dashboards/datasources from
   `compose/grafana/provisioning/`, plus Grafana Alerting rules for sensor/infra health.
8. **Set up daily backups**: a host crontab entry running `tools/backup_db.sh` (see the
   script header for the exact line, or let `install.sh` install it), retaining 14 days.
9. **Seed plant/sensor data**: assign each MiFlora MAC to a plant, resolve care thresholds
   via OpenPlantbook (falling back to the conservative defaults in §6 for unresolved
   species), and confirm each Telegram admin can trigger identification by simply sending a
   photo (no command needed — see §6).

---

## 10. First run on new hardware

Canopy has been exercised end to end in pieces but not on every kind of host, so verify
the BLE-to-database path once on a fresh machine. In order:

1. **Containers up**: `docker ps` shows `cp-mosquitto`, `cp-db`, `cp-theengs`, `cp-ingest`,
   `cp-decision`, `cp-telegram`, `cp-canopy-ui` and `cp-grafana` all `Up` (not restarting).
   A restart loop on `cp-grafana` almost always means `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`
   are missing from `$DATA_DIR/env/grafana.env`; on `cp-telegram`, a wrong bot token.
2. **Bluetooth is up**: `systemctl is-active bluetooth` prints `active`, and `hciconfig` (or
   `bluetoothctl list`) shows an adapter. No adapter, no data.
3. **Theengs sees advertisements**: `docker logs cp-theengs` should show decoded devices
   within a minute or two. Silence while the container looks healthy usually means the
   D-Bus mount or `privileged`/host-network settings are missing (§3).
4. **MQTT carries them**: `docker exec cp-mosquitto mosquitto_sub -t 'canopy/ble/#' -v`
   prints JSON per sensor. Theengs reaches Mosquitto over the host loopback
   (`MQTT_HOST=127.0.0.1`, §3); if Theengs logs connection errors, set `MQTT_HOST` in `.env`
   to the machine's LAN IP and re-run `install.sh`. This loopback topology has not been
   confirmed on every kernel/firewall setup.
5. **Ingest writes rows**: `docker logs cp-ingest` is free of errors, and
   `docker exec cp-db psql -U canopy -d canopy -c "select count(*) from bt_readings"` grows.
   Rows are written when a bucket closes (every `BUCKET_MINUTES`, default 30), so allow
   up to that long for the first one.
6. **Sensors announce themselves**: each new MAC triggers a "new sensor" alert in Telegram;
   register it with `/addsensor MAC NUMBER` and `/assign`.
7. **Weak signal**: a sensor with very low RSSI (around -85 dBm or worse) is unreliable;
   move the host or the sensor closer. A successful data reading does not guarantee the
   separate battery read (§5) succeeds on a weak link.

---

## 11. Explicitly out of scope for this document

- **Automated irrigation** (never merged): would add an `irrigation_zones`
  schema, a valve command executor, and planned sun/weather gating (Open-Meteo) for
  automated watering. This work predates the GARDENA removal and is not part of this
  repository.
- **Deploy generalization** (removing hardcoded paths from `deploy.sh` toward a fully
  `.env`-driven, `curl | bash`-installable deploy mechanism): done — see §8 and `install.sh`.
- **Easier MiFlora sensor discovery for fresh setups** is still proposed future work, not
  yet designed or implemented.
