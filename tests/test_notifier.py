"""Notifier のユニットテスト（モックで実APIを叩かない）."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.monitor.notifier import Notifier


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _make_notifier(configured: bool = True) -> Notifier:
    """テスト用 Notifier を生成する."""
    if configured:
        return Notifier(bot_token="test-token", chat_id="12345")
    return Notifier(bot_token="", chat_id="")


# ---------------------------------------------------------------------------
# 基本動作
# ---------------------------------------------------------------------------

class TestNotifierBasic:
    """基本動作のテスト."""

    def test_is_configured_true(self) -> None:
        n = _make_notifier(configured=True)
        assert n.is_configured is True

    def test_is_configured_false_no_token(self) -> None:
        n = Notifier(bot_token="", chat_id="123")
        assert n.is_configured is False

    def test_is_configured_false_no_chat_id(self) -> None:
        n = Notifier(bot_token="tok", chat_id="")
        assert n.is_configured is False

    @pytest.mark.asyncio
    async def test_send_skips_when_not_configured(self) -> None:
        """未設定時は送信せず False を返す."""
        n = _make_notifier(configured=False)
        result = await n.send("test")
        assert result is False


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------

class TestNotifierSend:
    """send() のテスト."""

    @pytest.mark.asyncio
    async def test_send_success(self) -> None:
        """正常送信で True を返す."""
        n = _make_notifier()
        mock_bot = AsyncMock()
        n._bot = mock_bot

        result = await n.send("hello")
        assert result is True
        mock_bot.send_message.assert_awaited_once_with(
            chat_id="12345", text="hello",
        )

    @pytest.mark.asyncio
    async def test_send_telegram_error_returns_false(self) -> None:
        """TelegramError で False を返し、システムは止まらない."""
        from telegram.error import TelegramError

        n = _make_notifier()
        mock_bot = AsyncMock()
        mock_bot.send_message.side_effect = TelegramError("network error")
        n._bot = mock_bot

        result = await n.send("hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_unexpected_error_returns_false(self) -> None:
        """予期しない例外でも False を返す."""
        n = _make_notifier()
        mock_bot = AsyncMock()
        mock_bot.send_message.side_effect = RuntimeError("boom")
        n._bot = mock_bot

        result = await n.send("hello")
        assert result is False


# ---------------------------------------------------------------------------
# シグナル通知
# ---------------------------------------------------------------------------

class TestNotifierSignal:
    """notify_signal() のテスト."""

    @pytest.mark.asyncio
    async def test_signal_message_format(self) -> None:
        n = _make_notifier()
        mock_bot = AsyncMock()
        n._bot = mock_bot

        await n.notify_signal(
            symbol="NVDA",
            sentiment_score=0.75,
            flow_strength=0.82,
            entry_price=125.50,
            direction="LONG",
        )
        msg = mock_bot.send_message.call_args[1]["text"]
        assert "SIGNAL" in msg
        assert "NVDA" in msg
        assert "+0.75" in msg
        assert "0.82" in msg
        assert "125.50" in msg


# ---------------------------------------------------------------------------
# エントリー通知
# ---------------------------------------------------------------------------

class TestNotifierEntry:
    """notify_entry() のテスト."""

    @pytest.mark.asyncio
    async def test_entry_message_format(self) -> None:
        n = _make_notifier()
        mock_bot = AsyncMock()
        n._bot = mock_bot

        await n.notify_entry("AAPL", "LONG", 10, 150.00)
        msg = mock_bot.send_message.call_args[1]["text"]
        assert "ENTRY" in msg
        assert "AAPL" in msg
        assert "10" in msg
        assert "150.00" in msg


# ---------------------------------------------------------------------------
# 決済通知
# ---------------------------------------------------------------------------

class TestNotifierExit:
    """notify_exit() のテスト."""

    @pytest.mark.asyncio
    async def test_exit_profit_message(self) -> None:
        n = _make_notifier()
        mock_bot = AsyncMock()
        n._bot = mock_bot

        await n.notify_exit("AAPL", 250.00, "TP")
        msg = mock_bot.send_message.call_args[1]["text"]
        assert "EXIT" in msg
        assert "+250.00" in msg
        assert "TP" in msg

    @pytest.mark.asyncio
    async def test_exit_loss_message(self) -> None:
        n = _make_notifier()
        mock_bot = AsyncMock()
        n._bot = mock_bot

        await n.notify_exit("TSLA", -120.00, "SL")
        msg = mock_bot.send_message.call_args[1]["text"]
        assert "-120.00" in msg
        assert "SL" in msg


# ---------------------------------------------------------------------------
# サーキットブレーカー通知
# ---------------------------------------------------------------------------

class TestNotifierCircuitBreaker:
    """notify_circuit_breaker() のテスト."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_message(self) -> None:
        n = _make_notifier()
        mock_bot = AsyncMock()
        n._bot = mock_bot

        await n.notify_circuit_breaker("日次損失3%超過")
        msg = mock_bot.send_message.call_args[1]["text"]
        assert "CIRCUIT BREAKER" in msg
        assert "日次損失" in msg


# ---------------------------------------------------------------------------
# 日次サマリー通知
# ---------------------------------------------------------------------------

class TestNotifierDailySummary:
    """notify_daily_summary() のテスト."""

    @pytest.mark.asyncio
    async def test_daily_summary_message(self) -> None:
        n = _make_notifier()
        mock_bot = AsyncMock()
        n._bot = mock_bot

        summary = {
            "daily_pnl": 350.50,
            "total_trades": 8,
            "win_rate": 0.625,
            "max_drawdown": 0.015,
            "open_positions": 2,
        }
        await n.notify_daily_summary(summary)
        msg = mock_bot.send_message.call_args[1]["text"]
        assert "DAILY SUMMARY" in msg
        assert "+350.50" in msg
        assert "8" in msg
        assert "62%" in msg  # 0.625 → 62%
