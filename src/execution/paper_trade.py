"""ペーパートレードモジュール."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class PaperPosition:
    """ペーパートレードのポジション."""
    symbol: str
    direction: str  # "LONG" or "SHORT"
    size: int
    entry_price: float
    opened_at: datetime = field(default_factory=datetime.now)


@dataclass
class PaperTradeResult:
    """ペーパートレードの決済結果."""
    symbol: str
    direction: str
    size: int
    entry_price: float
    exit_price: float
    pnl: float
    is_win: bool


class PaperTradeEngine:
    """ペーパートレードエンジン: 実際の発注を行わずにトレードをシミュレートする."""

    def __init__(self, initial_balance: float = 100_000.0) -> None:
        self._balance = initial_balance
        self._initial_balance = initial_balance
        self._positions: dict[str, PaperPosition] = {}
        self._trade_history: list[PaperTradeResult] = []

    @property
    def balance(self) -> float:
        """現在の残高."""
        return self._balance

    @property
    def total_pnl(self) -> float:
        """累計損益."""
        return self._balance - self._initial_balance

    @property
    def trade_count(self) -> int:
        """総取引回数."""
        return len(self._trade_history)

    def open_position(
        self,
        symbol: str,
        direction: str,
        size: int,
        price: float,
    ) -> str:
        """ペーパーポジションを開く.

        Args:
            symbol: 銘柄シンボル
            direction: "LONG" or "SHORT"
            size: 株数
            price: エントリー価格

        Returns:
            ポジションID
        """
        position_id = f"PAPER-{symbol}-{datetime.now().strftime('%H%M%S%f')}"
        self._positions[position_id] = PaperPosition(
            symbol=symbol,
            direction=direction,
            size=size,
            entry_price=price,
        )
        logger.info(
            "[PAPER] ポジションオープン: %s %s %d株 @ %.2f",
            direction, symbol, size, price,
        )
        return position_id

    def close_position(self, position_id: str, exit_price: float) -> PaperTradeResult | None:
        """ペーパーポジションを決済する.

        Args:
            position_id: ポジションID
            exit_price: 決済価格

        Returns:
            決済結果（ポジションが存在しない場合はNone）
        """
        position = self._positions.pop(position_id, None)
        if position is None:
            logger.warning("[PAPER] ポジションが見つかりません: %s", position_id)
            return None

        if position.direction == "LONG":
            pnl = (exit_price - position.entry_price) * position.size
        else:
            pnl = (position.entry_price - exit_price) * position.size

        self._balance += pnl
        result = PaperTradeResult(
            symbol=position.symbol,
            direction=position.direction,
            size=position.size,
            entry_price=position.entry_price,
            exit_price=exit_price,
            pnl=pnl,
            is_win=pnl > 0,
        )
        self._trade_history.append(result)
        logger.info(
            "[PAPER] ポジション決済: %s %s PnL=%.2f (残高=%.2f)",
            position.symbol, position.direction, pnl, self._balance,
        )
        return result

    def get_summary(self) -> dict:
        """トレードサマリーを返す."""
        wins = sum(1 for t in self._trade_history if t.is_win)
        losses = len(self._trade_history) - wins
        return {
            "balance": self._balance,
            "total_pnl": self.total_pnl,
            "trade_count": self.trade_count,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / max(self.trade_count, 1),
        }
