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
    """サーキットブレーカー: 異常時にトレードを自動停止する.

    判定優先順位:
      1. 最大ドローダウン > MAX_DRAWDOWN_PCT (10%) → 全ポジ強制決済・停止
      2. 日次損失 > MAX_DAILY_LOSS_PCT (3%)        → 新規発注停止
      3. 連続敗北 >= CONSECUTIVE_LOSS_LIMIT (3)     → サイズ50%縮小
    """

    def __init__(self) -> None:
        self._halted: bool = False

    @property
    def is_halted(self) -> bool:
        """発動中かどうか."""
        return self._halted

    def check(self, account_state: AccountState) -> BreakerStatus:
        """口座状態を評価してサーキットブレーカーの判定を行う.

        Args:
            account_state: 口座状態

        Returns:
            サーキットブレーカー判定結果
        """
        # 既に発動済みの場合はリセットされるまで取引不可
        if self._halted:
            return BreakerStatus(
                action=BreakerAction.HALT_NEW_ORDERS,
                reason="サーキットブレーカー発動中",
                can_trade=False,
            )

        balance = account_state.balance
        daily_pnl = account_state.daily_pnl
        peak = account_state.peak_balance

        # --- 1. 最大ドローダウン判定（最優先） ---
        if peak > 0:
            drawdown = (peak - balance) / peak
            if drawdown > settings.MAX_DRAWDOWN_PCT:
                self._halted = True
                reason = (
                    f"最大DD {drawdown:.1%} > {settings.MAX_DRAWDOWN_PCT:.0%}: "
                    f"全ポジション強制決済"
                )
                logger.critical(reason)
                return BreakerStatus(
                    action=BreakerAction.FORCE_CLOSE_ALL,
                    reason=reason,
                    can_trade=False,
                )

        # --- 2. 日次損失判定 ---
        if balance > 0 and daily_pnl < 0:
            daily_loss_pct = abs(daily_pnl) / balance
            if daily_loss_pct > settings.MAX_DAILY_LOSS_PCT:
                self._halted = True
                reason = (
                    f"日次損失 {daily_loss_pct:.1%} > {settings.MAX_DAILY_LOSS_PCT:.0%}: "
                    f"新規発注停止"
                )
                logger.warning(reason)
                return BreakerStatus(
                    action=BreakerAction.HALT_NEW_ORDERS,
                    reason=reason,
                    can_trade=False,
                )

        # --- 3. 連続敗北判定 ---
        if account_state.consecutive_losses >= settings.CONSECUTIVE_LOSS_LIMIT:
            reason = (
                f"連続{account_state.consecutive_losses}敗: サイズ50%縮小"
            )
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
        """毎朝リセットする（9:30 ET に呼び出す想定）."""
        self._halted = False
        logger.info("サーキットブレーカーをリセットしました")
