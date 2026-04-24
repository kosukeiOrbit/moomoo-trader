"""固定割合によるポジションサイズ計算モジュール.

POSITION_MAX_PCT の割合で口座残高から株数を計算する。
連続敗北時はサイズを50%に縮小するリスク管理を維持。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class TradeResult:
    """トレード結果."""

    symbol: str
    pnl: float
    is_win: bool


class PositionSizer:
    """固定割合によるポジションサイズ計算エンジン.

    shares = int(account_balance * POSITION_MAX_PCT / price)
    連続3敗でサイズ50%縮小。
    """

    def __init__(self) -> None:
        self._wins: int = 0
        self._losses: int = 0
        self._consecutive_losses: int = 0

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def trade_count(self) -> int:
        return self._wins + self._losses

    @property
    def win_rate(self) -> float:
        total = self._wins + self._losses
        if total == 0:
            return 0.0
        return self._wins / total

    def calculate(self, symbol: str, price: float, account_balance: float) -> int:
        """ポジションサイズ（株数）を計算する.

        Args:
            symbol: 銘柄シンボル
            price: 現在の株価
            account_balance: 口座残高（買付余力）

        Returns:
            発注株数
        """
        if price <= 0 or account_balance <= 0:
            return 0

        max_pct = settings.POSITION_MAX_PCT

        # 固定割合で株数を計算
        position_value = account_balance * max_pct
        shares = int(position_value / price)

        # MIN_POSITION_SHARES の保証
        if shares < settings.MIN_POSITION_SHARES:
            shares = settings.MIN_POSITION_SHARES

        # 連続敗北時はサイズを50%に縮小
        if self._consecutive_losses >= settings.CONSECUTIVE_LOSS_LIMIT:
            shares = max(1, int(shares * 0.5))
            logger.warning(
                "Consecutive %d losses: position size halved -> %d shares",
                self._consecutive_losses, shares,
            )

        # 口座残高の絶対上限
        max_affordable = int(account_balance / price)
        shares = min(shares, max_affordable)

        # 本当に1株も買えない場合のみ 0
        if max_affordable < 1:
            shares = 0

        logger.info(
            "[%s] PositionSize: price=%.2f balance=%.0f "
            "pct=%.0f%% shares=%d (value=$%.0f)",
            symbol, price, account_balance,
            max_pct * 100, shares, shares * price,
        )
        return shares

    def update_stats(self, trade_result: TradeResult) -> None:
        """トレード結果で連続敗北カウントを更新する."""
        if trade_result.is_win:
            self._wins += 1
            self._consecutive_losses = 0
        else:
            self._losses += 1
            self._consecutive_losses += 1
        logger.info(
            "Stats: win_rate=%.0f%% consecutive_losses=%d trades=%d",
            self.win_rate * 100,
            self._consecutive_losses,
            self.trade_count,
        )
