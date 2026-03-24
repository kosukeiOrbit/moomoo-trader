"""Discord Webhook 通知モジュール.

送信失敗時はログに記録してシステムは止めない。
Webhook URL が未設定の場合は通知をスキップする。
チャンネルごとに Webhook を分離:
  - mt-signal:  シグナル発生・エントリー
  - mt-alert:   決済・サーキットブレーカー
  - mt-summary: 日次サマリー
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from config import settings

logger = logging.getLogger(__name__)

# Discord Embed カラー定数
COLOR_BLUE = 0x3498DB
COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_GOLD = 0xF1C40F


class Notifier:
    """Discord Webhook 通知クライアント."""

    def __init__(
        self,
        webhook_signal: str | None = None,
        webhook_alert: str | None = None,
        webhook_summary: str | None = None,
    ) -> None:
        self._webhook_signal = (
            webhook_signal if webhook_signal is not None
            else settings.DISCORD_WEBHOOK_SIGNAL
        )
        self._webhook_alert = (
            webhook_alert if webhook_alert is not None
            else settings.DISCORD_WEBHOOK_ALERT
        )
        self._webhook_summary = (
            webhook_summary if webhook_summary is not None
            else settings.DISCORD_WEBHOOK_SUMMARY
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        """少なくとも1つの Webhook が設定済みか."""
        return bool(self._webhook_signal or self._webhook_alert or self._webhook_summary)

    def _post(self, webhook_url: str, payload: dict[str, Any]) -> bool:
        """Discord Webhook に POST する.

        Args:
            webhook_url: Webhook URL
            payload: JSON ペイロード

        Returns:
            送信成功なら True
        """
        if not webhook_url:
            logger.debug("Webhook URL 未設定: スキップ")
            return False
        try:
            resp = requests.post(webhook_url, json=payload, timeout=10)
            if resp.status_code in (200, 204):
                logger.info("Discord送信成功: %s", webhook_url[:50])
                return True
            logger.error("Discord送信失敗: status=%d body=%s", resp.status_code, resp.text[:200])
            return False
        except requests.RequestException as e:
            logger.error("Discord送信エラー: %s", e)
            return False
        except Exception:
            logger.exception("Discord送信で予期しないエラー")
            return False

    @staticmethod
    def _embed(
        title: str,
        color: int,
        fields: list[dict[str, Any]],
        description: str = "",
    ) -> dict[str, Any]:
        """Discord Embed オブジェクトを構築する."""
        embed: dict[str, Any] = {
            "title": title,
            "color": color,
            "fields": fields,
        }
        if description:
            embed["description"] = description
        return embed

    # ------------------------------------------------------------------
    # シグナル発生通知 → mt-signal
    # ------------------------------------------------------------------

    def notify_signal(
        self,
        symbol: str,
        sentiment_score: float,
        flow_strength: float,
        entry_price: float,
        direction: str = "LONG",
    ) -> bool:
        """シグナル発生を通知する."""
        embed = self._embed(
            title=f"SIGNAL: {direction} {symbol}",
            color=COLOR_BLUE,
            fields=[
                {"name": "Sentiment", "value": f"{sentiment_score:+.2f}", "inline": True},
                {"name": "Flow Strength", "value": f"{flow_strength:.2f}", "inline": True},
                {"name": "Entry Price", "value": f"${entry_price:.2f}", "inline": True},
            ],
        )
        return self._post(self._webhook_signal, {"embeds": [embed]})

    # ------------------------------------------------------------------
    # エントリー通知 → mt-signal
    # ------------------------------------------------------------------

    def notify_entry(
        self,
        symbol: str,
        direction: str,
        size: int,
        price: float,
    ) -> bool:
        """エントリー（約定）通知を送信する."""
        embed = self._embed(
            title=f"ENTRY: {direction} {symbol}",
            color=COLOR_GREEN,
            fields=[
                {"name": "Size", "value": f"{size} shares", "inline": True},
                {"name": "Price", "value": f"${price:.2f}", "inline": True},
                {"name": "Total", "value": f"${size * price:,.2f}", "inline": True},
            ],
        )
        return self._post(self._webhook_signal, {"embeds": [embed]})

    # ------------------------------------------------------------------
    # 決済通知 → mt-alert
    # ------------------------------------------------------------------

    def notify_exit(
        self,
        symbol: str,
        pnl: float,
        reason: str,
    ) -> bool:
        """決済通知を送信する."""
        color = COLOR_GREEN if pnl >= 0 else COLOR_RED
        embed = self._embed(
            title=f"EXIT: {symbol}",
            color=color,
            fields=[
                {"name": "PnL", "value": f"${pnl:+.2f}", "inline": True},
                {"name": "Reason", "value": reason, "inline": True},
            ],
        )
        return self._post(self._webhook_alert, {"embeds": [embed]})

    # ------------------------------------------------------------------
    # サーキットブレーカー通知 → mt-alert (@everyone)
    # ------------------------------------------------------------------

    def notify_circuit_breaker(self, reason: str) -> bool:
        """サーキットブレーカー発動を緊急アラートとして送信する."""
        embed = self._embed(
            title="CIRCUIT BREAKER",
            color=COLOR_RED,
            description=reason,
            fields=[],
        )
        return self._post(self._webhook_alert, {
            "content": "@everyone",
            "embeds": [embed],
        })

    # ------------------------------------------------------------------
    # 日次サマリー通知 → mt-summary
    # ------------------------------------------------------------------

    def notify_daily_summary(self, summary: dict) -> bool:
        """日次サマリーを送信する."""
        daily_pnl = summary.get("daily_pnl", 0)
        color = COLOR_GREEN if daily_pnl >= 0 else COLOR_RED
        embed = self._embed(
            title="DAILY SUMMARY",
            color=color,
            fields=[
                {"name": "PnL", "value": f"${daily_pnl:+.2f}", "inline": True},
                {"name": "Trades", "value": str(summary.get("total_trades", 0)), "inline": True},
                {"name": "Win Rate", "value": f"{summary.get('win_rate', 0):.0%}", "inline": True},
                {"name": "Max DD", "value": f"{summary.get('max_drawdown', 0):.2%}", "inline": True},
                {"name": "Open Pos", "value": str(summary.get("open_positions", 0)), "inline": True},
            ],
        )
        return self._post(self._webhook_summary, {"embeds": [embed]})
