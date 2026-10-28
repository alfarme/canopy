#!/usr/bin/env bash
# Canopy — one-command installer. Run on the machine that will host Canopy
# (a Raspberry Pi or similar Linux box with Bluetooth):
#
#   curl -fsSL https://raw.githubusercontent.com/alfarme/canopy/main/install.sh | bash
#
# What it does, in one pass:
#   1. checks prerequisites (installs git; offers to install Docker),
#   2. clones/updates the repo,
#   3. asks a handful of questions (data directory, optional port changes, and
#      the three Telegram values the stack cannot run without),
#   4. writes .env and the per-container env files, installs the daily backup
#      cron job, then builds and starts the stack via ./deploy.sh.
#
# Optional API keys (plant ID, automatic care thresholds) are deliberately NOT
# asked for here: add them to .env afterwards and re-run ~/canopy/install.sh.
#
# Idempotent and safe to re-run: questions are only asked for values that are
# still empty. Prompts are read from the controlling terminal (/dev/tty), so
# they work under `curl | bash`. With no terminal at all (CI, cron) it never
# prompts: preset the values as environment variables or in .env, or it stops
# after scaffolding .env and tells you what to fill in.
#
# Safe under `bash -x`: tracing is switched off while secrets are handled.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/alfarme/canopy.git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/canopy}"
BRANCH="${BRANCH:-main}"
TTY="${CANOPY_TTY:-/dev/tty}"   # overridable so the prompt logic is testable

log() { printf '==> %s\n' "$1"; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

# ---- xtrace control (bash -x must not print tokens/passwords) -------------
TRACE=0
case $- in *x*) TRACE=1 ;; esac
trace_off() { { set +x; } 2>/dev/null; }
trace_on() { if [ "$TRACE" = 1 ]; then set -x; fi; }

# ---- terminal prompts ---------------------------------------------------
# Open the terminal once as fd 3 and read all answers from it; prompts go to stderr.
HAVE_TTY=0
if ( : <"$TTY" ) 2>/dev/null; then exec 3<"$TTY"; HAVE_TTY=1; fi
have_tty() { [ "$HAVE_TTY" = 1 ]; }

# ask VAR "question" [default] [secret]  — sets VAR; empty reply -> default
ask() {
    local var="$1" prompt="$2" def="${3:-}" secret="${4:-}" reply=""
    if [ -n "$def" ]; then prompt="$prompt [$def]"; fi
    printf '%s: ' "$prompt" >&2
    if [ -n "$secret" ]; then
        read -r -s reply <&3 || true
        printf '\n' >&2
    else
        read -r reply <&3 || true
    fi
    printf -v "$var" '%s' "${reply:-$def}"
}

# ask_yes "question" default(y|n)
ask_yes() {
    local reply=""
    printf '%s [%s] ' "$1" "$([ "$2" = y ] && echo Y/n || echo y/N)" >&2
    read -r reply <&3 || true
    reply="${reply:-$2}"
    case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# set_env KEY VALUE — replace KEY=... in .env, or append it
set_env() {
    local key="$1" val esc
    val="$2"
    esc="$(printf '%s' "$val" | sed 's/[&|\\]/\\&/g')"
    if grep -q "^${key}=" .env; then
        sed -i "s|^${key}=.*|${key}=${esc}|" .env
    else
        printf '%s=%s\n' "$key" "$val" >> .env
    fi
}

# ---- prerequisites -----------------------------------------------------
SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

if ! command -v git >/dev/null 2>&1; then
    command -v apt-get >/dev/null 2>&1 || die "git is not installed and this system has no apt-get. Install git and re-run."
    log "Installing git"
    $SUDO apt-get update -qq && $SUDO apt-get install -y git
fi

if ! command -v docker >/dev/null 2>&1; then
    have_tty || die "docker is not installed. See https://docs.docker.com/engine/install/ and re-run."
    if ask_yes "Docker is required but not installed. Install it now with Docker's official script?" y; then
        log "Installing Docker (https://get.docker.com)"
        curl -fsSL https://get.docker.com | $SUDO sh
        $SUDO usermod -aG docker "$(id -un)"
        cat >&2 <<EOF

Docker is installed and your user was added to the 'docker' group.
Log out and back in (or reboot) so the group applies, then run the same
install command again — it picks up where it left off.
EOF
        exit 0
    fi
    die "docker is required. See https://docs.docker.com/engine/install/ and re-run."
fi
docker compose version >/dev/null 2>&1 || die "'docker compose' (the plugin, not docker-compose) is required. See https://docs.docker.com/compose/install/linux/ and re-run."
docker info >/dev/null 2>&1 || die "Cannot talk to the Docker daemon. Is it running, and is your user in the 'docker' group? (sudo usermod -aG docker \$USER, then log out and back in)"
if ! systemctl is-active --quiet bluetooth 2>/dev/null; then
    log "WARNING: bluetooth.service is not active — Theengs Gateway needs it for BLE. Continuing; fix with 'sudo systemctl enable --now bluetooth' before expecting sensor data."
fi

# ---- clone or update ----------------------------------------------------
if [ -d "$INSTALL_DIR/.git" ]; then
    log "Updating existing checkout at $INSTALL_DIR"
    git -C "$INSTALL_DIR" fetch origin "$BRANCH"
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
elif [ -e "$INSTALL_DIR" ]; then
    die "$INSTALL_DIR exists and is not a git checkout — remove it or set INSTALL_DIR to a different path."
else
    log "Cloning $REPO_URL -> $INSTALL_DIR"
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# ---- everything below handles secrets: no xtrace ---------------------------
trace_off

FIRST_RUN=0
if [ ! -f .env ]; then
    cp .env.example .env
    FIRST_RUN=1
fi

# Values preset in the environment win over .env (CI / unattended installs).
for var in DATA_DIR GRAFANA_PORT CANOPY_UI_PORT TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID \
           TELEGRAM_ADMIN_USER_ID PLANTNET_API_KEY OPENPLANTBOOK_CLIENT_ID OPENPLANTBOOK_CLIENT_SECRET; do
    if [ -n "${!var:-}" ]; then set_env "$var" "${!var}"; fi
done

load_env() { set -a; source .env; set +a; }
load_env

# ---- interactive setup ------------------------------------------------------
INTERACTIVE="$HAVE_TTY"

if [ -z "${DATA_DIR:-}" ]; then
    DATA_DIR="$HOME/canopy-data"
    if [ "$INTERACTIVE" = 1 ]; then
        printf '\nCanopy keeps its database, backups and photos in one directory.\n' >&2
        while :; do
            ask DATA_DIR "Data directory" "$DATA_DIR"
            DATA_DIR="${DATA_DIR/#\~/$HOME}"
            case "$DATA_DIR" in
                /*[[:space:]]*|*[[:space:]]*) echo "No spaces in the path, please." >&2 ;;
                /*) break ;;
                *) echo "Please give an absolute path." >&2 ;;
            esac
        done
    fi
    set_env DATA_DIR "$DATA_DIR"
fi

if [ "$FIRST_RUN" = 1 ] && [ "$INTERACTIVE" = 1 ]; then
    if ask_yes "Change the web ports (Grafana ${GRAFANA_PORT:-3000}, Canopy UI ${CANOPY_UI_PORT:-8090})?" n; then
        ask GRAFANA_PORT "Grafana port" "${GRAFANA_PORT:-3000}"
        ask CANOPY_UI_PORT "Canopy UI port" "${CANOPY_UI_PORT:-8090}"
        set_env GRAFANA_PORT "$GRAFANA_PORT"
        set_env CANOPY_UI_PORT "$CANOPY_UI_PORT"
    fi
fi

# Telegram: the bot, the digests and Grafana's health alerts all need these.
if [ "$INTERACTIVE" = 1 ] && { [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ] || [ -z "${TELEGRAM_ADMIN_USER_ID:-}" ]; }; then
    cat >&2 <<'EOF'

Telegram is how Canopy talks to you (daily digests, alerts, admin commands).
  1. In Telegram, message @BotFather, send /newbot and copy the bot token.
  2. Message @userinfobot to get your own numeric user id.
  3. Send your new bot any message (or add it to a group) first.
EOF
    while [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; do
        ask TELEGRAM_BOT_TOKEN "Bot token (input hidden)" "" secret
        if ! [[ "$TELEGRAM_BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
            echo "That doesn't look like a bot token (expected 123456:ABC...)." >&2
            TELEGRAM_BOT_TOKEN=""
        fi
    done
    if bot="$(curl -fsS --max-time 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>/dev/null)"; then
        echo "Token accepted: @$(printf '%s' "$bot" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')" >&2
    else
        echo "Could not verify the token with Telegram (offline, or wrong token) — continuing." >&2
    fi
    while [ -z "${TELEGRAM_ADMIN_USER_ID:-}" ]; do
        ask TELEGRAM_ADMIN_USER_ID "Your numeric Telegram user id"
        [[ "$TELEGRAM_ADMIN_USER_ID" =~ ^[0-9]+$ ]] || { echo "Digits only, e.g. 123456789." >&2; TELEGRAM_ADMIN_USER_ID=""; }
    done
    while [ -z "${TELEGRAM_CHAT_ID:-}" ]; do
        ask TELEGRAM_CHAT_ID "Chat id for digests (press Enter to use your user id for a private chat)" "$TELEGRAM_ADMIN_USER_ID"
        [[ "$TELEGRAM_CHAT_ID" =~ ^-?[0-9]+$ ]] || { echo "A number (groups are negative, e.g. -1001234567890)." >&2; TELEGRAM_CHAT_ID=""; }
    done
    set_env TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
    set_env TELEGRAM_ADMIN_USER_ID "$TELEGRAM_ADMIN_USER_ID"
    set_env TELEGRAM_CHAT_ID "$TELEGRAM_CHAT_ID"
fi

load_env
missing=""
for var in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID TELEGRAM_ADMIN_USER_ID; do
    [ -n "${!var:-}" ] || missing="$missing $var"
done
if [ -n "$missing" ]; then
    die "No terminal to ask on. Fill in these vars in $INSTALL_DIR/.env (see the comments there), then run $INSTALL_DIR/install.sh:$missing"
fi

# ---- generate + persist passwords if left blank (stable across reruns) --
gen_password() { head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24; }
if [ -z "${POSTGRES_PASSWORD:-}" ]; then
    POSTGRES_PASSWORD="$(gen_password)"
    set_env POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
    log "Generated POSTGRES_PASSWORD and saved it to .env"
fi
if [ -z "${GF_SECURITY_ADMIN_PASSWORD:-}" ]; then
    GF_SECURITY_ADMIN_PASSWORD="$(gen_password)"
    set_env GF_SECURITY_ADMIN_PASSWORD "$GF_SECURITY_ADMIN_PASSWORD"
    log "Generated GF_SECURITY_ADMIN_PASSWORD and saved it to .env"
fi

# ---- run bind-mounting services as the deploying user -----------------------
# (grafana, telegram-bot, canopy-ui): a non-root user cannot chown to an
# image's baked-in uid, so the containers adopt the owner of $DATA_DIR instead.
# Not in .env.example: this is install mechanics, not something to hand-edit.
# Kept in sync with the current user on every run (self-heals if this script
# is ever run under a different account).
set_env HOST_UID "$(id -u)"
set_env HOST_GID "$(id -g)"

# ---- scaffold $DATA_DIR ---------------------------------------------------
log "Setting up $DATA_DIR"
mkdir -p "$DATA_DIR"/{pgdata,backups,mosquitto/config,mosquitto/data,mosquitto/log,grafana,identify-photos,canopy-cache,deploy-history,env}

if [ ! -f "$DATA_DIR/mosquitto/config/mosquitto.conf" ]; then
    cat > "$DATA_DIR/mosquitto/config/mosquitto.conf" <<'MOSQ'
listener 1883
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
log_dest file /mosquitto/log/mosquitto.log
MOSQ
    log "Wrote default mosquitto.conf (anonymous access — LAN-only broker, not exposed beyond this machine's network)"
fi

# ---- split .env into per-container secret files --------------------------
# Regenerated from .env every run — this is the single source of truth.
# Don't hand-edit files under $DATA_DIR/env/; edit .env and re-run instead.
log "Writing $DATA_DIR/env/*.env from .env"
DB_URL="postgresql://canopy:${POSTGRES_PASSWORD}@timescaledb:5432/canopy"

cat > "$DATA_DIR/env/timescaledb.env" <<EOF
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
EOF

cat > "$DATA_DIR/env/ingest.env" <<EOF
DATABASE_URL=${DB_URL}
MQTT_HOST=mosquitto
MQTT_PORT=1883
LOG_LEVEL=INFO
EOF

cat > "$DATA_DIR/env/decision.env" <<EOF
DATABASE_URL=${DB_URL}
LOG_LEVEL=INFO
EOF

cat > "$DATA_DIR/env/telegram.env" <<EOF
DATABASE_URL=${DB_URL}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
TELEGRAM_ADMIN_USER_ID=${TELEGRAM_ADMIN_USER_ID}
PLANTNET_API_KEY=${PLANTNET_API_KEY:-}
OPENPLANTBOOK_CLIENT_ID=${OPENPLANTBOOK_CLIENT_ID:-}
OPENPLANTBOOK_CLIENT_SECRET=${OPENPLANTBOOK_CLIENT_SECRET:-}
LOG_LEVEL=INFO
EOF

cat > "$DATA_DIR/env/canopy-ui.env" <<EOF
DATABASE_URL=${DB_URL}
EOF

# GF_TS_PASSWORD is the same 'canopy' role Grafana's datasource logs in as —
# deliberately the same value as POSTGRES_PASSWORD, not a separate secret.
# TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID feed the Grafana-side Telegram contact
# point (compose/grafana/provisioning/alerting/contact-points.yaml) — same
# credentials as telegram.env, but Grafana reads its own copy. Without these
# Grafana's alerting provisioning fails to validate the contact point and
# crash-loops on every start (this is fatal for Grafana, unlike telegram-bot
# just failing its own auth check).
cat > "$DATA_DIR/env/grafana.env" <<EOF
GF_SECURITY_ADMIN_PASSWORD=${GF_SECURITY_ADMIN_PASSWORD}
GF_TS_PASSWORD=${POSTGRES_PASSWORD}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
EOF
chmod 600 "$DATA_DIR"/env/*.env .env

trace_on

# ---- daily backup cron job -------------------------------------------------
CRON_LINE="0 3 * * * DATA_DIR=$DATA_DIR $INSTALL_DIR/tools/backup_db.sh >> $DATA_DIR/backups/backup.log 2>&1"
if ! (crontab -l 2>/dev/null | grep -qF "$INSTALL_DIR/tools/backup_db.sh"); then
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab - \
        && log "Installed daily backup cron job (03:00, 14-day retention)" \
        || log "WARNING: could not install the backup cron job — add this line to your crontab yourself: $CRON_LINE"
else
    log "Backup cron job already installed"
fi

# ---- build + deploy ---------------------------------------------------
log "Building and starting the stack (this can take a while on first run)..."
./deploy.sh

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST_IP="${HOST_IP:-localhost}"
cat <<EOF

Canopy is up.
  Canopy UI:  http://${HOST_IP}:${CANOPY_UI_PORT:-8090}
  Grafana:    http://${HOST_IP}:${GRAFANA_PORT:-3000}   (user: admin, password: see $DATA_DIR/env/grafana.env)

Next steps
  1. Check that sensor data is arriving (give it a couple of minutes):
       docker exec cp-mosquitto mosquitto_sub -t 'canopy/ble/#' -v
     Nothing? See docs/technical.md section 10 (first run on new hardware).
  2. In Telegram, message your bot /help, then /addsensor to register a sensor.
EOF

optional=""
for var in PLANTNET_API_KEY OPENPLANTBOOK_CLIENT_ID OPENPLANTBOOK_CLIENT_SECRET; do
    [ -n "${!var:-}" ] || optional="$optional $var"
done
if [ -n "$optional" ]; then
    cat <<EOF
  3. Optional — enable plant ID and automatic care thresholds. Set these in
     $INSTALL_DIR/.env, then run $INSTALL_DIR/install.sh again:
      $optional
     Where to get them: docs/technical.md section 6. Without them Canopy still
     works, using conservative default thresholds.
EOF
fi
