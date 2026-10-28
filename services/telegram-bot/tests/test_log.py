"""
Tests for logging setup — specifically that we never let secrets leak.
"""
import logging

from app.log import setup_logging


class TestHttpxLoggingSuppressed:
    """The Telegram Bot API embeds the bot token IN the request URL
    (.../bot<TOKEN>/method). httpx/httpcore log full URLs at INFO, so
    without suppression the token lands in plaintext container logs.
    """

    def test_httpx_logger_is_warning_or_above(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        setup_logging()
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING

    def test_httpx_logger_is_warning_even_with_debug_app_level(self, monkeypatch):
        # Even if someone sets LOG_LEVEL=DEBUG for app troubleshooting, the
        # token must not leak — suppression must not depend on LOG_LEVEL.
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        setup_logging()
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING

    def test_httpcore_logger_is_warning_or_above(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        setup_logging()
        assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
