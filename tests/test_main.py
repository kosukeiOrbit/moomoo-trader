"""main.py のユニットテスト."""

from __future__ import annotations

import sys
from datetime import datetime, time
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

# moomoo SDK モック
if "futu" not in sys.modules:
    sys.modules["futu"] = MagicMock()

from src.main import market_is_open, should_force_exit, ET, JST


# ---------------------------------------------------------------------------
# market_is_open
# ---------------------------------------------------------------------------

class TestMarketIsOpen:
    """market_is_open() のテスト."""

    @patch("src.main.datetime")
    def test_open_during_trading_hours(self, mock_dt: MagicMock) -> None:
        """ET 10:00 月曜日 → True."""
        mock_now = datetime(2026, 3, 23, 10, 0, tzinfo=ET)  # 月曜
        mock_dt.now.return_value = mock_now
        assert market_is_open() is True

    @patch("src.main.datetime")
    def test_closed_before_open(self, mock_dt: MagicMock) -> None:
        """ET 9:00 → False."""
        mock_now = datetime(2026, 3, 23, 9, 0, tzinfo=ET)
        mock_dt.now.return_value = mock_now
        assert market_is_open() is False

    @patch("src.main.datetime")
    def test_closed_after_close(self, mock_dt: MagicMock) -> None:
        """ET 16:30 → False."""
        mock_now = datetime(2026, 3, 23, 16, 30, tzinfo=ET)
        mock_dt.now.return_value = mock_now
        assert market_is_open() is False

    @patch("src.main.datetime")
    def test_closed_on_saturday(self, mock_dt: MagicMock) -> None:
        """土曜 → False."""
        mock_now = datetime(2026, 3, 28, 10, 0, tzinfo=ET)  # 土曜
        mock_dt.now.return_value = mock_now
        assert market_is_open() is False

    @patch("src.main.datetime")
    def test_closed_on_sunday(self, mock_dt: MagicMock) -> None:
        """日曜 → False."""
        mock_now = datetime(2026, 3, 29, 10, 0, tzinfo=ET)  # 日曜
        mock_dt.now.return_value = mock_now
        assert market_is_open() is False

    @patch("src.main.datetime")
    def test_open_at_930(self, mock_dt: MagicMock) -> None:
        """ET 9:30 ちょうど → True."""
        mock_now = datetime(2026, 3, 23, 9, 30, tzinfo=ET)
        mock_dt.now.return_value = mock_now
        assert market_is_open() is True

    @patch("src.main.datetime")
    def test_open_at_1600(self, mock_dt: MagicMock) -> None:
        """ET 16:00 ちょうど → True."""
        mock_now = datetime(2026, 3, 23, 16, 0, tzinfo=ET)
        mock_dt.now.return_value = mock_now
        assert market_is_open() is True


# ---------------------------------------------------------------------------
# should_force_exit
# ---------------------------------------------------------------------------

class TestShouldForceExit:
    """should_force_exit() のテスト (ET-based, DST-aware)."""

    @patch("src.main.datetime")
    def test_force_exit_at_1550_et(self, mock_dt: MagicMock) -> None:
        """ET 15:50 → True."""
        mock_now = datetime(2026, 3, 24, 15, 50, tzinfo=ET)
        mock_dt.now.return_value = mock_now
        assert should_force_exit() is True

    @patch("src.main.datetime")
    def test_no_force_exit_at_1549_et(self, mock_dt: MagicMock) -> None:
        """ET 15:49 → False."""
        mock_now = datetime(2026, 3, 24, 15, 49, tzinfo=ET)
        mock_dt.now.return_value = mock_now
        assert should_force_exit() is False

    @patch("src.main.datetime")
    def test_force_exit_at_1600_et(self, mock_dt: MagicMock) -> None:
        """ET 16:00 → True (after 15:50)."""
        mock_now = datetime(2026, 3, 24, 16, 0, tzinfo=ET)
        mock_dt.now.return_value = mock_now
        assert should_force_exit() is True
