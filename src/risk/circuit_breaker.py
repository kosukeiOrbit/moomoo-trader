"""異常時の自動停止とリスク管理モジュール."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from config import settings

logger = logging.getLogger(__name__)


class BreakerAction(Enum):
    """サーキットブレーカーのアクション."""
    OK = "OK"
    HALT_NEW_ORDERS = "HALT_NEW_ORDERS"
    FORCE_CLOSE_ALL = "FORCE_CLOSE_ALL"
    REDUCE_SIZE = "REDUCE_SIZE"


@dataclass
class AccountState:
    """口座状態."""
    balance: float
    daily_pnl: float
    peak_balance: float
    consecutive_losses: int


@dataclass
class BreakerStatus:
    """サーキットブレーカーの判定結果."""
    action: BreakerAction
    reason: str
    can_trade: bool


class CircuitBreaker:
    """サーキットブレーカー: 異常時にトレードを自動停止する."""

    def __init__(self) -> None:
        self._halted: bool = False

    def check(self, account_state: AccountState) -> BreakerStatus:
        """口座状態を評価してサーキットブレーカーの判定を行う.

        発動条件:
        - 日次損失が資金の3%超過 → 当日の全新規発注停止
        - 最大ドローダウンが10%超過 → 全ポジション強制決済・停止
        - 連続3敗 → ポジションサイズを50%に縮小

        Args:
            account_state: 口座状態

        Returns:
            サーキットブレーカー判定結果
        """
        if self._halted:
            return BreakerStatus(
                action=BreakerAction.HALT_NEW_ORDERS,
                reason="サーキットブレーカー発動中",
                can_trade=False,
            )

        balance = account_state.balance
        daily_pnl = account_state.daily_pnl
        peak = account_state.peak_balance

        # 最大ドローダウン判定（最優先）
        if peak > 0:
            drawdown = (peak - balance) / peak
            if drawdown > settings.MAX_DRAWDOWN_PCT:
                self._halted = True
                reason = f"最大DD {drawdown:.1%} > {settings.MAX_DRAWDOWN_PCT:.0%}: 全ポジション強制決済"
                logger.critical(reason)
                return BreakerStatus(
                    action=BreakerAction.FORCE_CLOSE_ALL,
                    reason=reason,
                    can_trade=False,
                )

        # 日次損失判定
        if balance > 0:
            daily_loss_pct = abs(daily_pnl) / balance if daily_pnl < 0 else 0
            if daily_loss_pct > settings.MAX_DAILY_LOSS_PCT:
                self._halted = True
                reason = f"日次損失 {daily_loss_pct:.1%} > {settings.MAX_DAILY_LOSS_PCT:.0%}: 新規発注停止"
                logger.warning(reason)
                return BreakerStatus(
                    action=BreakerAction.HALT_NEW_ORDERS,
                    reason=reason,
                    can_trade=False,
                )

        # 連続敗北判定
        if account_state.consecutive_losses >= settings.CONSECUTIVE_LOSS_LIMIT:
            reason = f"連続{account_state.consecutive_losses}敗: サイズ50%縮小"
            logger.warning(reason)
            return BreakerStatus(
                action=BreakerAction.REDUCE_SIZE,
                reason=reason,
                can_trade=True,
            )

        return BreakerStatus(
            action=BreakerAction.OK,
            reason="正常",
            can_trade=True,
        )

    def reset_daily(self) -> None:
        """毎朝9:30（ET）に自動リセットする."""
        self._halted = False
        logger.info("サーキットブレーカーをリセットしました")
