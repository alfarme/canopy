# Changelog

## 2026-10-27 — Guided installer and first-run docs

- `install.sh` now asks its questions itself (data dir, optional ports, Telegram bot token / user id /
  chat id) via `/dev/tty`, validates them, installs git (and, on consent, Docker), and deploys in a
  single pass; optional API keys are added to `.env` afterwards. Safe under `bash -x`.
- README rewritten (project overview + Quick start); manual from-the-repo install moved to `INSTALL.md`; `docs/technical.md` §10 is now a first-run checklist for
  new hardware.
- `DATA_DIR` has no machine-specific default any more (asked at install, default `~/canopy-data`).

## 2026-10-27 — Initial public history

Fresh single-commit history for `alfarme/canopy`. The system is MiFlora-only:
Theengs Gateway → MQTT → ingest → TimescaleDB, with decision-engine digests,
a Telegram bot, Grafana health alerting and the Canopy photo dashboard
(`services/canopy-ui`), deployed to one Raspberry Pi via `install.sh` / `deploy.sh`.

- bind-mounting services (grafana, telegram-bot, canopy-ui) run as the host user via
  `HOST_UID`/`HOST_GID` (written by `install.sh`; replaces `GRAFANA_UID`/`GRAFANA_GID`)
- design history, rationale and rebuild guide: `docs/technical.md`
