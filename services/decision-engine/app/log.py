"""Structured logging helper (stdout, key=val format)."""
import logging
import os
import sys


def get_logger(name: str) -> logging.Logger:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                fmt="time=%(asctime)s level=%(levelname)s service=decision-engine logger=%(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, level, logging.INFO))
    return logger
