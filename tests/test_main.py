"""main.py のユニットテスト."""

from __future__ import annotations

import sys
from datetime import datetime, time
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

# moomoo SDK モック
if "moomoo" not in sys.modules:
    sys.modules["moomoo"] = MagicMock()

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
    """should_force_exit() のテスト."""

    @patch("src.main.datetime")
    def test_force_exit_at_0550(self, mock_dt: MagicMock) -> None:
        """JST 05:50 → True."""
        mock_now = datetime(2026, 3, 24, 5, 50, tzinfo=JST)
        mock_dt.now.return_value = mock_now
        assert should_force_exit() is True

    @patch("src.main.datetime")
    def test_no_force_exit_at_0549(self, mock_dt: MagicMock) -> None:
        """JST 05:49 → False."""
        mock_now = datetime(2026, 3, 24, 5, 49, tzinfo=JST)
        mock_dt.now.return_value = mock_now
        assert should_force_exit() is False

    @patch("src.main.datetime")
    def test_force_exit_after_0550(self, mock_dt: MagicMock) -> None:
        """JST 06:00 → True（05:50以降）."""
        mock_now = datetime(2026, 3, 24, 6, 0, tzinfo=JST)
        mock_dt.now.return_value = mock_now
        assert should_force_exit() is True
