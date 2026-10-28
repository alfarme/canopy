# canopy — telegram-bot

Sends plant-health alerts to a Telegram chat and handles basic bot commands.

## What it does

- Polls `alert_events WHERE delivered=false` every `POLL_INTERVAL` seconds,
  sends each alert to the configured Telegram chat, then marks it delivered.
- Supports three slash commands:
  - `/assign <sensor> <plant name>` — link a sensor to a plant.
  - `/plants` — list all plants with current sensor assignments.
  - `/status` — show latest sensor readings for all active sensors.

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | — | Target chat/group ID |
| `DATABASE_URL` | ✅* | — | `postgresql://user:pass@host:port/db` (wins over discrete vars) |
| `PGHOST` | ✅* | `localhost` | Postgres host (if no DATABASE_URL) |
| `PGPORT` | | `5432` | Postgres port |
| `PGUSER` | | `canopy` | Postgres user |
| `PGPASSWORD` | | | Postgres password |
| `PGDATABASE` | | `canopy` | Postgres database name |
| `POLL_INTERVAL` | | `30` | Seconds between alert polls |
| `LOG_LEVEL` | | `INFO` | Python logging level |

\* Either `DATABASE_URL` or the discrete `PG*` vars are required.

## Running tests

```bash
cd services/telegram-bot
python -m pytest tests/ -v
```

Tests are fully offline (no network, DB, or Telegram API calls).

## Running locally

```bash
cp .env.example .env
# edit .env with your values
pip install -r requirements.txt
python -m app
```

## Docker

```bash
docker build -t canopy/telegram-bot:latest .
docker run --env-file /path/to/telegram.env canopy/telegram-bot:latest
```
