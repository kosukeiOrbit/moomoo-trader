"""Telegram通知モジュール."""

from __future__ import annotations

import logging

from telegram import Bot

from config import settings

logger = logging.getLogger(__name__)


class Notifier:
    """Telegram通知クライアント."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        self._token = bot_token or settings.TELEGRAM_BOT_TOKEN
        self._chat_id = chat_id or settings.TELEGRAM_CHAT_ID
        self._bot: Bot | None = None

    def _ensure_bot(self) -> Bot:
        if self._bot is None:
            self._bot = Bot(token=self._token)
        return self._bot

    async def send(self, message: str) -> None:
        """メッセージを送信する.

        Args:
            message: 送信するメッセージテキスト
        """
        if not self._token or not self._chat_id:
            logger.debug("Telegram未設定: メッセージをスキップ")
            return
        try:
            bot = self._ensure_bot()
            await bot.send_message(chat_id=self._chat_id, text=message)
            logger.info("Telegram通知送信: %s", message[:50])
        except Exception:
            logger.exception("Telegram通知送信エラー")

    async def notify_entry(self, symbol: str, direction: str, size: int, price: float) -> None:
        """エントリー通知を送信する."""
        msg = f"🟢 ENTRY: {direction} {symbol} {size}株 @ ${price:.2f}"
        await self.send(msg)

    async def notify_exit(self, symbol: str, pnl: float, reason: str) -> None:
        """決済通知を送信する."""
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg = f"{emoji} EXIT: {symbol} PnL=${pnl:+.2f} ({reason})"
        await self.send(msg)

    async def notify_circuit_breaker(self, reason: str) -> None:
        """サーキットブレーカー発動通知を送信する."""
        msg = f"🚨 CIRCUIT BREAKER: {reason}"
        await self.send(msg)

    async def notify_daily_summary(self, summary: dict) -> None:
        """日次サマリー通知を送信する."""
        msg = (
            f"📊 DAILY SUMMARY\n"
            f"PnL: ${summary.get('daily_pnl', 0):+.2f}\n"
            f"Trades: {summary.get('total_trades', 0)}\n"
            f"Win Rate: {summary.get('win_rate', 0):.0%}\n"
            f"Open Positions: {summary.get('open_positions', 0)}"
        )
        await self.send(msg)
