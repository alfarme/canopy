"""Minimal structured logger — logs to stdout as key=val lines."""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        msg = record.getMessage()
        return f"ts={ts} level={record.levelname} logger={record.name} msg={msg!r}"


def get_logger(name: str) -> logging.Logger:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger
