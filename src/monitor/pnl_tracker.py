"""リアルタイムP&L記録モジュール."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    """トレード記録."""
    order_id: str
    symbol: str
    direction: str
    size: int
    entry_price: float
    exit_price: float | None = None
    pnl: float = 0.0
    opened_at: datetime = field(default_factory=datetime.now)
    closed_at: datetime | None = None


class PnLTracker:
    """P&Lトラッカー: リアルタイムに損益を記録・集計する."""

    def __init__(self) -> None:
        self._open_trades: dict[str, TradeRecord] = {}
        self._closed_trades: list[TradeRecord] = []
        self._daily_pnl: float = 0.0
        self._peak_balance: float = 0.0

    def register(
        self,
        order_id: str,
        symbol: str,
        direction: str,
        size: int,
        entry_price: float,
    ) -> None:
        """新規トレードを記録する.

        Args:
            order_id: 注文ID
            symbol: 銘柄シンボル
            direction: "LONG" or "SHORT"
            size: 株数
            entry_price: エントリー価格
        """
        self._open_trades[order_id] = TradeRecord(
            order_id=order_id,
            symbol=symbol,
            direction=direction,
            size=size,
            entry_price=entry_price,
        )
        logger.info("トレード記録: %s %s %s %d株 @ %.2f", order_id, direction, symbol, size, entry_price)

    def close_trade(self, order_id: str, exit_price: float) -> float:
        """トレードを決済記録する.

        Args:
            order_id: 注文ID
            exit_price: 決済価格

        Returns:
            損益
        """
        trade = self._open_trades.pop(order_id, None)
        if trade is None:
            logger.warning("オープントレードが見つかりません: %s", order_id)
            return 0.0

        if trade.direction == "LONG":
            pnl = (exit_price - trade.entry_price) * trade.size
        else:
            pnl = (trade.entry_price - exit_price) * trade.size

        trade.exit_price = exit_price
        trade.pnl = pnl
        trade.closed_at = datetime.now()
        self._closed_trades.append(trade)
        self._daily_pnl += pnl

        logger.info("トレード決済: %s PnL=%.2f 日次合計=%.2f", order_id, pnl, self._daily_pnl)
        return pnl

    @property
    def daily_pnl(self) -> float:
        """当日の累計損益."""
        return self._daily_pnl

    @property
    def total_pnl(self) -> float:
        """全期間の累計損益."""
        return sum(t.pnl for t in self._closed_trades)

    @property
    def open_trade_count(self) -> int:
        """オープンポジション数."""
        return len(self._open_trades)

    def update_peak_balance(self, current_balance: float) -> None:
        """ピーク残高を更新する."""
        if current_balance > self._peak_balance:
            self._peak_balance = current_balance

    @property
    def peak_balance(self) -> float:
        """ピーク残高."""
        return self._peak_balance

    def reset_daily(self) -> None:
        """日次リセット."""
        self._daily_pnl = 0.0
        logger.info("日次P&Lをリセットしました")

    def get_summary(self) -> dict:
        """サマリーを返す."""
        wins = [t for t in self._closed_trades if t.pnl > 0]
        losses = [t for t in self._closed_trades if t.pnl <= 0]
        return {
            "total_pnl": self.total_pnl,
            "daily_pnl": self.daily_pnl,
            "total_trades": len(self._closed_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / max(len(self._closed_trades), 1),
            "open_positions": self.open_trade_count,
        }
