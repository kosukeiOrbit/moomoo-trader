"""固定額によるポジションサイズ計算モジュール.

POSITION_SIZE_USD の固定額で株数を計算する。
連続敗北時はサイズを50%に縮小するリスク管理を維持。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


@dataclass
class TradeResult:
    """トレード結果."""

    symbol: str
    pnl: float
    is_win: bool


class PositionSizer:
    """固定額によるポジションサイズ計算エンジン.

    shares = int(POSITION_SIZE_USD / price)
    連続3敗でサイズ50%縮小。
    """

    def __init__(self) -> None:
        self._wins: int = 0
        self._losses: int = 0
        self._consecutive_losses: int = 0
        # 10/04 追加: 余力等分モード用。 セッション内で観測した最大の買付余力。
        self._session_key: str = ""
        self._baseline_bp: float = 0.0

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

    def calculate(
        self,
        symbol: str,
        price: float,
        account_balance: float,
        direction: str = "LONG",
    ) -> int:
        """ポジションサイズ（株数）を計算する.

        Args:
            symbol: 銘柄シンボル
            price: 現在の株価
            account_balance: 口座残高（買付余力）
            direction: "LONG" or "SHORT" — SHORT は SHORT_POSITION_SIZE_USD を使う

        Returns:
            発注株数
        """
        if price <= 0 or account_balance <= 0:
            return 0

        # 固定額で株数を計算 (SHORT は専用サイズ)
        if direction == "SHORT":
            position_value = settings.SHORT_POSITION_SIZE_USD
            size_mode = "fixed"
        elif settings.POSITION_SIZE_BP_SPLIT_ENABLED:
            # 10/04 追加: 買付余力を枠数で等分する。
            # 注意: calculate() に渡る account_balance は「その時点の残余力」なので
            # そのまま割ると2件目以降が小さくなる。 セッション内で観測した最大値を
            # baseline として使い、 全枠に同じ額を割り当てる。
            # 建玉保有中に Bot を再起動した場合は baseline が小さく出るが、
            # 枠が小さくなる = 安全側に倒れるので許容する。
            session = datetime.now(_ET).date().isoformat()
            if self._session_key != session:
                self._session_key = session
                self._baseline_bp = 0.0
            self._baseline_bp = max(self._baseline_bp, account_balance)
            slots = max(1, settings.LONG_MAX_POSITIONS)
            position_value = (
                self._baseline_bp / slots * settings.POSITION_SIZE_BP_SAFETY
            )
            size_mode = f"bp_split({self._baseline_bp:,.0f}/{slots})"
        else:
            position_value = settings.POSITION_SIZE_USD
            size_mode = "fixed"
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
            "[%s] PositionSize: price=$%.2f budget=$%.0f mode=%s "
            "shares=%d (value=$%.0f, balance=$%.0f)",
            symbol, price, position_value, size_mode,
            shares, shares * price, account_balance,
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
