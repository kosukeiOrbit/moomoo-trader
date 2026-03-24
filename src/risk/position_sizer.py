"""Kelly基準によるポジションサイズ計算モジュール."""

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
    """ハーフKelly基準によるポジションサイズ計算エンジン.

    Kelly% = (勝率 × 平均利益 - 敗率 × 平均損失) / 平均損失
    実際のサイズ = Kelly% × KELLY_FRACTION (0.5 = ハーフケリー)
    上限 = POSITION_MAX_PCT (2%)
    """

    def __init__(self) -> None:
        self._wins: int = 0
        self._losses: int = 0
        self._total_profit: float = 0.0
        self._total_loss: float = 0.0
        self._consecutive_losses: int = 0

    # ------------------------------------------------------------------
    # プロパティ
    # ------------------------------------------------------------------

    @property
    def win_rate(self) -> float:
        """勝率を返す（データなしはデフォルト50%）."""
        total = self._wins + self._losses
        if total == 0:
            return 0.5
        return self._wins / total

    @property
    def avg_profit(self) -> float:
        """平均利益を返す（勝ちなしは1.0）."""
        if self._wins == 0:
            return 1.0
        return self._total_profit / self._wins

    @property
    def avg_loss(self) -> float:
        """平均損失を返す（正の値、負けなしは1.0）."""
        if self._losses == 0:
            return 1.0
        return abs(self._total_loss) / self._losses

    @property
    def consecutive_losses(self) -> int:
        """連続敗北数."""
        return self._consecutive_losses

    @property
    def trade_count(self) -> int:
        """総トレード数."""
        return self._wins + self._losses

    # ------------------------------------------------------------------
    # Kelly計算
    # ------------------------------------------------------------------

    def _kelly_fraction(self) -> float:
        """ハーフKelly基準の割合を算出する.

        Kelly% = (W × avg_W - L × avg_L) / avg_L
        Half-Kelly = Kelly% × KELLY_FRACTION
        """
        w = self.win_rate
        avg_w = self.avg_profit
        avg_l = self.avg_loss
        if avg_l == 0:
            return 0.0
        kelly = (w * avg_w - (1 - w) * avg_l) / avg_l
        # Kelly が負 = 期待値マイナス → 0 にクランプ
        return max(kelly * settings.KELLY_FRACTION, 0.0)

    # ------------------------------------------------------------------
    # ポジションサイズ計算
    # ------------------------------------------------------------------

    def calculate(self, symbol: str, price: float, account_balance: float) -> int:
        """最適ポジションサイズ（株数）を計算する.

        Args:
            symbol: 銘柄シンボル
            price: 現在の株価
            account_balance: 口座残高

        Returns:
            発注株数（最小0）
        """
        if price <= 0 or account_balance <= 0:
            return 0

        kelly_pct = self._kelly_fraction()
        max_pct = settings.POSITION_MAX_PCT  # 上限 2%

        size_pct = min(kelly_pct, max_pct)

        # 連続敗北時はサイズを50%に縮小
        if self._consecutive_losses >= settings.CONSECUTIVE_LOSS_LIMIT:
            size_pct *= 0.5
            logger.warning(
                "連続%d敗: ポジションサイズを50%%に縮小",
                self._consecutive_losses,
            )

        position_value = account_balance * size_pct
        shares = int(position_value / price)
        logger.info(
            "ポジションサイズ計算: %s kelly=%.4f size=%.4f shares=%d",
            symbol, kelly_pct, size_pct, shares,
        )
        return max(shares, 0)

    # ------------------------------------------------------------------
    # 統計更新
    # ------------------------------------------------------------------

    def update_stats(self, trade_result: TradeResult) -> None:
        """トレード結果で勝率・平均損益を動的更新する.

        Args:
            trade_result: トレード結果
        """
        if trade_result.is_win:
            self._wins += 1
            self._total_profit += trade_result.pnl
            self._consecutive_losses = 0
        else:
            self._losses += 1
            self._total_loss += trade_result.pnl  # pnl は負の値
            self._consecutive_losses += 1
        logger.info(
            "統計更新: 勝率=%.1f%% 連続敗北=%d trades=%d",
            self.win_rate * 100,
            self._consecutive_losses,
            self.trade_count,
        )
