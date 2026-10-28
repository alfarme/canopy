"""
Structured logging setup — one line per event, key=val format.
"""
from __future__ import annotations
import logging
import os
import sys


def setup_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        stream=sys.stdout,
        level=level,
        format="time=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    # httpx/httpcore log full request URLs at INFO — and the Telegram Bot API
    # embeds the bot token IN the URL path (.../bot<TOKEN>/method). Silence
    # these regardless of LOG_LEVEL so the token never lands in container
    # logs; PlantNet/OpenPlantbook calls don't carry secrets in the URL, but
    # the same suppression is harmless and consistent for them too.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
