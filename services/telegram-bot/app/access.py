"""
Access control — gate write commands to the admin user.
"""
from __future__ import annotations

import os
from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes


def get_admin_user_id() -> int | None:
    """Return the configured admin user ID, or None if not set."""
    raw = os.environ.get("TELEGRAM_ADMIN_USER_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def is_admin(user_id: int) -> bool:
    """Check if a given Telegram user ID matches the admin."""
    admin_id = get_admin_user_id()
    if admin_id is None:
        # If no admin configured, deny all write commands for safety
        return False
    return user_id == admin_id


def admin_only(handler):
    """Decorator that restricts a command handler to the admin user."""
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if user is None or not is_admin(user.id):
            await update.message.reply_text("Not authorized")
            return
        return await handler(update, context, *args, **kwargs)
    return wrapper
