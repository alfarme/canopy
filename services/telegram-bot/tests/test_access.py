"""
Tests for the access control module.
"""
import os
import pytest
from unittest.mock import patch
from app.access import get_admin_user_id, is_admin


class TestGetAdminUserId:
    def test_valid_id(self):
        with patch.dict(os.environ, {"TELEGRAM_ADMIN_USER_ID": "123456789"}):
            assert get_admin_user_id() == 123456789

    def test_missing_env(self):
        with patch.dict(os.environ, {}, clear=True):
            assert get_admin_user_id() is None

    def test_empty_string(self):
        with patch.dict(os.environ, {"TELEGRAM_ADMIN_USER_ID": ""}):
            assert get_admin_user_id() is None

    def test_non_numeric(self):
        with patch.dict(os.environ, {"TELEGRAM_ADMIN_USER_ID": "not-a-number"}):
            assert get_admin_user_id() is None

    def test_whitespace(self):
        with patch.dict(os.environ, {"TELEGRAM_ADMIN_USER_ID": "  123456789  "}):
            assert get_admin_user_id() == 123456789


class TestIsAdmin:
    def test_matching_id(self):
        with patch.dict(os.environ, {"TELEGRAM_ADMIN_USER_ID": "12345"}):
            assert is_admin(12345) is True

    def test_non_matching_id(self):
        with patch.dict(os.environ, {"TELEGRAM_ADMIN_USER_ID": "12345"}):
            assert is_admin(99999) is False

    def test_no_admin_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            assert is_admin(12345) is False
