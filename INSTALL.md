# Manual install

Use this guide if you'd rather not pipe a script into `bash`, or if the installer doesn't fit
your setup (a different layout, an existing Docker host, a non-standard user). It does by
hand exactly what [`install.sh`](install.sh) does, so reading that script is the best
reference if something here is unclear.

For the one-command route, see the Quick start in the [README](README.md).

## 1. Prerequisites

- A 64-bit Linux machine with working Bluetooth (`systemctl is-active bluetooth` prints
  `active`), within range of your sensors.
- `git`, Docker Engine, and the Docker Compose **plugin** (`docker compose version` works —
  not the old `docker-compose`). Your user must be able to run `docker` without `sudo`.

## 2. Get the code

```bash
git clone https://github.com/alfarme/canopy.git ~/canopy
cd ~/canopy
```

## 3. Create your Telegram bot

You need three values:

1. **Bot token** — message **@BotFather** in Telegram, send `/newbot`, and copy the token.
2. **Your user id** — message **@userinfobot**.
3. **Chat id** — where digests go. Your own user id works for a private chat (send the bot a
   message first so it may write to you); for a group, add the bot and use the group's
   negative id.

## 4. Configure `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set:

| Variable | Value |
|---|---|
| `DATA_DIR` | Absolute path without spaces for all persistent data, e.g. `/home/you/canopy-data` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ADMIN_USER_ID` | The three values from step 3 |
| `POSTGRES_PASSWORD`, `GF_SECURITY_ADMIN_PASSWORD` | Any strong passwords (letters and digits are safest). Generate one with `head -c 24 /dev/urandom \| base64 \| tr -dc A-Za-z0-9` |
| `HOST_UID`, `HOST_GID` | **Add these two lines yourself**: the output of `id -u` and `id -g`. Grafana, the Telegram bot and Canopy UI run as this user so they can write to `DATA_DIR`. |

Optional: `GRAFANA_PORT`, `CANOPY_UI_PORT`, `MQTT_HOST`, `BLE_SCAN_TIME`,
`BLE_TIME_BETWEEN_SCANS` (defaults are fine), and the plant-ID keys
`PLANTNET_API_KEY`, `OPENPLANTBOOK_CLIENT_ID`, `OPENPLANTBOOK_CLIENT_SECRET` (see the README).

## 5. Create the data directory

```bash
set -a; source .env; set +a
mkdir -p "$DATA_DIR"/{pgdata,backups,mosquitto/config,mosquitto/data,mosquitto/log,grafana,identify-photos,canopy-cache,deploy-history,env}

cat > "$DATA_DIR/mosquitto/config/mosquitto.conf" <<'MOSQ'
listener 1883
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
log_dest file /mosquitto/log/mosquitto.log
MOSQ
```

The broker allows anonymous clients, so keep port 1883 on your local network.

## 6. Write the per-container env files

The containers read their secrets from files under `$DATA_DIR/env/`. Still in the same shell:

```bash
DB_URL="postgresql://canopy:${POSTGRES_PASSWORD}@timescaledb:5432/canopy"

printf 'POSTGRES_PASSWORD=%s\n' "$POSTGRES_PASSWORD" > "$DATA_DIR/env/timescaledb.env"

printf 'DATABASE_URL=%s\nMQTT_HOST=mosquitto\nMQTT_PORT=1883\nLOG_LEVEL=INFO\n' "$DB_URL" \
  > "$DATA_DIR/env/ingest.env"

printf 'DATABASE_URL=%s\nLOG_LEVEL=INFO\n' "$DB_URL" > "$DATA_DIR/env/decision.env"

printf 'DATABASE_URL=%s\n' "$DB_URL" > "$DATA_DIR/env/canopy-ui.env"

cat > "$DATA_DIR/env/telegram.env" <<EOT
DATABASE_URL=${DB_URL}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
TELEGRAM_ADMIN_USER_ID=${TELEGRAM_ADMIN_USER_ID}
PLANTNET_API_KEY=${PLANTNET_API_KEY:-}
OPENPLANTBOOK_CLIENT_ID=${OPENPLANTBOOK_CLIENT_ID:-}
OPENPLANTBOOK_CLIENT_SECRET=${OPENPLANTBOOK_CLIENT_SECRET:-}
LOG_LEVEL=INFO
EOT

cat > "$DATA_DIR/env/grafana.env" <<EOT
GF_SECURITY_ADMIN_PASSWORD=${GF_SECURITY_ADMIN_PASSWORD}
GF_TS_PASSWORD=${POSTGRES_PASSWORD}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
EOT

chmod 600 "$DATA_DIR"/env/*.env
```

Grafana needs the Telegram token and chat id in its own file as well. Without them it refuses
to start. These files are generated from `.env`: change `.env` and redo this step rather than
editing them.

## 7. Build and start

```bash
./deploy.sh
```

This builds the images on the machine (the first build takes several minutes on a Pi),
starts the stack, and checks that every container is running. Without the script, the
equivalent is:

```bash
set -a; source .env; set +a      # compose reads its variables from the environment
docker compose -f compose/docker-compose.yml up -d --build
docker restart cp-grafana        # pick up bind-mounted provisioning changes
```

## 8. Nightly backups (optional but recommended)

```bash
( crontab -l 2>/dev/null; echo "0 3 * * * DATA_DIR=$DATA_DIR $HOME/canopy/tools/backup_db.sh >> $DATA_DIR/backups/backup.log 2>&1" ) | crontab -
```

Dumps land in `$DATA_DIR/backups`; the last 14 days are kept.

## 9. Check that it works

Canopy UI is at `http://<host>:8090`, Grafana at `http://<host>:3000` (user `admin`, password
from `.env`). Then follow the [first-run checklist](docs/technical.md#10-first-run-on-new-hardware).

## Updating

```bash
cd ~/canopy
git pull --ff-only
# redo step 6 if .env changed, then:
./deploy.sh
```

## Uninstalling

```bash
cd ~/canopy
set -a; source .env; set +a
docker compose -f compose/docker-compose.yml down
crontab -l | grep -vF tools/backup_db.sh | crontab -     # remove the backup job
```

Your data stays in `$DATA_DIR`; delete that directory to remove it for good.
