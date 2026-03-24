"""Notifier のユニットテスト（モックで実APIを叩かない）."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from src.monitor.notifier import (
    Notifier,
    COLOR_BLUE,
    COLOR_GREEN,
    COLOR_RED,
)


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

FAKE_SIGNAL = "https://discord.com/api/webhooks/signal/xxx"
FAKE_ALERT = "https://discord.com/api/webhooks/alert/xxx"
FAKE_SUMMARY = "https://discord.com/api/webhooks/summary/xxx"


def _make_notifier(configured: bool = True) -> Notifier:
    """テスト用 Notifier を生成する."""
    if configured:
        return Notifier(
            webhook_signal=FAKE_SIGNAL,
            webhook_alert=FAKE_ALERT,
            webhook_summary=FAKE_SUMMARY,
        )
    return Notifier(webhook_signal="", webhook_alert="", webhook_summary="")


def _mock_post_ok() -> MagicMock:
    """requests.post の成功モックを返す."""
    mock_resp = MagicMock()
    mock_resp.status_code = 204
    return MagicMock(return_value=mock_resp)


def _extract_payload(mock_post: MagicMock) -> dict:
    """mock_post から送信された JSON payload を取り出す."""
    return mock_post.call_args[1]["json"]


# ---------------------------------------------------------------------------
# 基本動作
# ---------------------------------------------------------------------------

class TestNotifierBasic:
    """基本動作のテスト."""

    def test_is_configured_true(self) -> None:
        n = _make_notifier(configured=True)
        assert n.is_configured is True

    def test_is_configured_false_all_empty(self) -> None:
        n = _make_notifier(configured=False)
        assert n.is_configured is False

    def test_is_configured_partial(self) -> None:
        """1つでも設定されていれば True."""
        n = Notifier(webhook_signal=FAKE_SIGNAL, webhook_alert="", webhook_summary="")
        assert n.is_configured is True

    @patch("src.monitor.notifier.requests.post")
    def test_post_skips_empty_url(self, mock_post: MagicMock) -> None:
        """URL 空文字の場合は POST せず False."""
        n = _make_notifier(configured=False)
        result = n.notify_signal("AAPL", 0.5, 0.8, 150.0)
        assert result is False
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# _post() エラーハンドリング
# ---------------------------------------------------------------------------

class TestNotifierPost:
    """_post() のテスト."""

    @patch("src.monitor.notifier.requests.post")
    def test_post_success_204(self, mock_post: MagicMock) -> None:
        """204 で True を返す."""
        mock_post.return_value = MagicMock(status_code=204)
        n = _make_notifier()
        assert n._post(FAKE_SIGNAL, {"test": True}) is True

    @patch("src.monitor.notifier.requests.post")
    def test_post_success_200(self, mock_post: MagicMock) -> None:
        """200 でも True を返す."""
        mock_post.return_value = MagicMock(status_code=200)
        n = _make_notifier()
        assert n._post(FAKE_SIGNAL, {"test": True}) is True

    @patch("src.monitor.notifier.requests.post")
    def test_post_http_error_returns_false(self, mock_post: MagicMock) -> None:
        """4xx/5xx で False."""
        mock_post.return_value = MagicMock(status_code=400, text="Bad Request")
        n = _make_notifier()
        assert n._post(FAKE_SIGNAL, {}) is False

    @patch("src.monitor.notifier.requests.post")
    def test_post_network_error_returns_false(self, mock_post: MagicMock) -> None:
        """接続エラーで False（システムは止まらない）."""
        mock_post.side_effect = requests.ConnectionError("network down")
        n = _make_notifier()
        assert n._post(FAKE_SIGNAL, {}) is False

    @patch("src.monitor.notifier.requests.post")
    def test_post_unexpected_error_returns_false(self, mock_post: MagicMock) -> None:
        """予期しない例外でも False."""
        mock_post.side_effect = RuntimeError("boom")
        n = _make_notifier()
        assert n._post(FAKE_SIGNAL, {}) is False


# ---------------------------------------------------------------------------
# シグナル通知
# ---------------------------------------------------------------------------

class TestNotifierSignal:
    """notify_signal() のテスト."""

    @patch("src.monitor.notifier.requests.post", new_callable=_mock_post_ok)
    def test_signal_embed_format(self, mock_post: MagicMock) -> None:
        n = _make_notifier()
        result = n.notify_signal(
            symbol="NVDA",
            sentiment_score=0.75,
            flow_strength=0.82,
            entry_price=125.50,
            direction="LONG",
        )
        assert result is True
        # Webhook URL は mt-signal
        assert mock_post.call_args[0][0] == FAKE_SIGNAL
        payload = _extract_payload(mock_post)
        embed = payload["embeds"][0]
        assert "SIGNAL" in embed["title"]
        assert "NVDA" in embed["title"]
        assert embed["color"] == COLOR_BLUE
        field_values = [f["value"] for f in embed["fields"]]
        assert "+0.75" in field_values
        assert "0.82" in field_values
        assert "$125.50" in field_values


# ---------------------------------------------------------------------------
# エントリー通知
# ---------------------------------------------------------------------------

class TestNotifierEntry:
    """notify_entry() のテスト."""

    @patch("src.monitor.notifier.requests.post", new_callable=_mock_post_ok)
    def test_entry_embed_format(self, mock_post: MagicMock) -> None:
        n = _make_notifier()
        n.notify_entry("AAPL", "LONG", 10, 150.00)
        payload = _extract_payload(mock_post)
        embed = payload["embeds"][0]
        assert "ENTRY" in embed["title"]
        assert "AAPL" in embed["title"]
        assert embed["color"] == COLOR_GREEN
        field_values = [f["value"] for f in embed["fields"]]
        assert "10 shares" in field_values
        assert "$150.00" in field_values


# ---------------------------------------------------------------------------
# 決済通知
# ---------------------------------------------------------------------------

class TestNotifierExit:
    """notify_exit() のテスト."""

    @patch("src.monitor.notifier.requests.post", new_callable=_mock_post_ok)
    def test_exit_profit_green(self, mock_post: MagicMock) -> None:
        """利益のときは緑."""
        n = _make_notifier()
        n.notify_exit("AAPL", 250.00, "TP")
        payload = _extract_payload(mock_post)
        embed = payload["embeds"][0]
        assert embed["color"] == COLOR_GREEN
        field_values = [f["value"] for f in embed["fields"]]
        assert "$+250.00" in field_values
        assert "TP" in field_values
        # mt-alert に送信される
        assert mock_post.call_args[0][0] == FAKE_ALERT

    @patch("src.monitor.notifier.requests.post", new_callable=_mock_post_ok)
    def test_exit_loss_red(self, mock_post: MagicMock) -> None:
        """損失のときは赤."""
        n = _make_notifier()
        n.notify_exit("TSLA", -120.00, "SL")
        payload = _extract_payload(mock_post)
        embed = payload["embeds"][0]
        assert embed["color"] == COLOR_RED
        field_values = [f["value"] for f in embed["fields"]]
        assert "$-120.00" in field_values
        assert "SL" in field_values


# ---------------------------------------------------------------------------
# サーキットブレーカー通知
# ---------------------------------------------------------------------------

class TestNotifierCircuitBreaker:
    """notify_circuit_breaker() のテスト."""

    @patch("src.monitor.notifier.requests.post", new_callable=_mock_post_ok)
    def test_circuit_breaker_red_with_everyone(self, mock_post: MagicMock) -> None:
        n = _make_notifier()
        n.notify_circuit_breaker("日次損失3%超過")
        payload = _extract_payload(mock_post)
        # @everyone が content に含まれる
        assert payload["content"] == "@everyone"
        embed = payload["embeds"][0]
        assert "CIRCUIT BREAKER" in embed["title"]
        assert embed["color"] == COLOR_RED
        assert "日次損失" in embed["description"]
        # mt-alert に送信される
        assert mock_post.call_args[0][0] == FAKE_ALERT


# ---------------------------------------------------------------------------
# 日次サマリー通知
# ---------------------------------------------------------------------------

class TestNotifierDailySummary:
    """notify_daily_summary() のテスト."""

    @patch("src.monitor.notifier.requests.post", new_callable=_mock_post_ok)
    def test_daily_summary_embed(self, mock_post: MagicMock) -> None:
        n = _make_notifier()
        summary = {
            "daily_pnl": 350.50,
            "total_trades": 8,
            "win_rate": 0.625,
            "max_drawdown": 0.015,
            "open_positions": 2,
        }
        n.notify_daily_summary(summary)
        # mt-summary に送信される
        assert mock_post.call_args[0][0] == FAKE_SUMMARY
        payload = _extract_payload(mock_post)
        embed = payload["embeds"][0]
        assert "DAILY SUMMARY" in embed["title"]
        assert embed["color"] == COLOR_GREEN  # PnL > 0 → 緑
        field_values = [f["value"] for f in embed["fields"]]
        assert "$+350.50" in field_values
        assert "8" in field_values
        assert "62%" in field_values
