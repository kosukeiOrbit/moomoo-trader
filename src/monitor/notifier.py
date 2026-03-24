"""Telegram通知モジュール.

送信失敗時はログに記録してシステムは止めない。
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID が未設定の場合は全通知をスキップする。
"""

from __future__ import annotations

import logging

from telegram import Bot
from telegram.error import TelegramError

from config import settings

logger = logging.getLogger(__name__)


class Notifier:
    """Telegram 通知クライアント."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        self._token = bot_token if bot_token is not None else settings.TELEGRAM_BOT_TOKEN
        self._chat_id = chat_id if chat_id is not None else settings.TELEGRAM_CHAT_ID
        self._bot: Bot | None = None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        """Telegram が設定済みかどうか."""
        return bool(self._token) and bool(self._chat_id)

    def _ensure_bot(self) -> Bot:
        if self._bot is None:
            self._bot = Bot(token=self._token)
        return self._bot

    async def send(self, message: str) -> bool:
        """メッセージを送信する.

        Args:
            message: 送信テキスト

        Returns:
            送信成功なら True
        """
        if not self.is_configured:
            logger.debug("Telegram未設定: メッセージをスキップ")
            return False
        try:
            bot = self._ensure_bot()
            await bot.send_message(chat_id=self._chat_id, text=message)
            logger.info("Telegram送信: %s", message[:80])
            return True
        except TelegramError as e:
            logger.error("Telegram送信エラー: %s", e)
            return False
        except Exception:
            logger.exception("Telegram送信で予期しないエラー")
            return False

    # ------------------------------------------------------------------
    # シグナル発生通知
    # ------------------------------------------------------------------

    async def notify_signal(
        self,
        symbol: str,
        sentiment_score: float,
        flow_strength: float,
        entry_price: float,
        direction: str = "LONG",
    ) -> bool:
        """シグナル発生を通知する.

        Args:
            symbol: 銘柄シンボル
            sentiment_score: センチメントスコア
            flow_strength: 大口フロー強度
            entry_price: エントリー価格
            direction: "LONG" or "SHORT"
        """
        msg = (
            f"📡 SIGNAL: {direction} {symbol}\n"
            f"Sentiment: {sentiment_score:+.2f}\n"
            f"Flow Strength: {flow_strength:.2f}\n"
            f"Entry Price: ${entry_price:.2f}"
        )
        return await self.send(msg)

    # ------------------------------------------------------------------
    # エントリー通知
    # ------------------------------------------------------------------

    async def notify_entry(
        self,
        symbol: str,
        direction: str,
        size: int,
        price: float,
    ) -> bool:
        """エントリー（約定）通知を送信する."""
        msg = (
            f"🟢 ENTRY: {direction} {symbol}\n"
            f"Size: {size}株 @ ${price:.2f}\n"
            f"Total: ${size * price:,.2f}"
        )
        return await self.send(msg)

    # ------------------------------------------------------------------
    # 決済通知
    # ------------------------------------------------------------------

    async def notify_exit(
        self,
        symbol: str,
        pnl: float,
        reason: str,
    ) -> bool:
        """決済通知を送信する.

        Args:
            symbol: 銘柄シンボル
            pnl: 損益
            reason: 決済理由（SL / TP / センチメント反転 等）
        """
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg = (
            f"{emoji} EXIT: {symbol}\n"
            f"PnL: ${pnl:+.2f}\n"
            f"Reason: {reason}"
        )
        return await self.send(msg)

    # ------------------------------------------------------------------
    # サーキットブレーカー通知
    # ------------------------------------------------------------------

    async def notify_circuit_breaker(self, reason: str) -> bool:
        """サーキットブレーカー発動を緊急アラートとして送信する."""
        msg = f"🚨 CIRCUIT BREAKER 🚨\n{reason}"
        return await self.send(msg)

    # ------------------------------------------------------------------
    # 日次サマリー通知
    # ------------------------------------------------------------------

    async def notify_daily_summary(self, summary: dict) -> bool:
        """日次サマリーを送信する.

        Args:
            summary: PnLTracker.get_daily_summary() の戻り値
        """
        msg = (
            f"📊 DAILY SUMMARY\n"
            f"PnL: ${summary.get('daily_pnl', 0):+.2f}\n"
            f"Trades: {summary.get('total_trades', 0)}\n"
            f"Win Rate: {summary.get('win_rate', 0):.0%}\n"
            f"Max DD: {summary.get('max_drawdown', 0):.2%}\n"
            f"Open Positions: {summary.get('open_positions', 0)}"
        )
        return await self.send(msg)
