"""リスク管理モジュールのユニットテスト."""

from __future__ import annotations

import pytest

from src.risk.position_sizer import PositionSizer, TradeResult
from src.risk.circuit_breaker import CircuitBreaker, AccountState, BreakerAction
from src.risk.stop_loss import StopLossManager


class TestPositionSizer:
    """PositionSizer のテスト."""

    def test_initial_position_size(self) -> None:
        """初期状態（データなし）でのポジションサイズ計算."""
        sizer = PositionSizer()
        shares = sizer.calculate("AAPL", 150.0, 100_000.0)
        assert shares >= 0
        # 上限2%: 100,000 * 0.02 / 150 = 13株以下
        assert shares <= 14

    def test_zero_price(self) -> None:
        """価格0の場合は0株."""
        sizer = PositionSizer()
        assert sizer.calculate("AAPL", 0.0, 100_000.0) == 0

    def test_zero_balance(self) -> None:
        """残高0の場合は0株."""
        sizer = PositionSizer()
        assert sizer.calculate("AAPL", 150.0, 0.0) == 0

    def test_update_stats_win(self) -> None:
        """勝ちトレードで勝率が更新される."""
        sizer = PositionSizer()
        sizer.update_stats(TradeResult(symbol="AAPL", pnl=100.0, is_win=True))
        assert sizer.win_rate == 1.0
        assert sizer._consecutive_losses == 0

    def test_update_stats_loss(self) -> None:
        """負けトレードで連続敗北がカウントされる."""
        sizer = PositionSizer()
        sizer.update_stats(TradeResult(symbol="AAPL", pnl=-50.0, is_win=False))
        assert sizer.win_rate == 0.0
        assert sizer._consecutive_losses == 1

    def test_consecutive_losses_reduce_size(self) -> None:
        """連続3敗でサイズが縮小される."""
        sizer = PositionSizer()
        for _ in range(3):
            sizer.update_stats(TradeResult(symbol="AAPL", pnl=-50.0, is_win=False))
        shares_reduced = sizer.calculate("AAPL", 150.0, 100_000.0)

        sizer2 = PositionSizer()
        shares_normal = sizer2.calculate("AAPL", 150.0, 100_000.0)

        # 縮小後は通常時以下になる
        assert shares_reduced <= shares_normal


class TestCircuitBreaker:
    """CircuitBreaker のテスト."""

    def test_ok_state(self) -> None:
        """正常な口座状態ではOK."""
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=100_000, daily_pnl=0, peak_balance=100_000, consecutive_losses=0,
        ))
        assert status.action == BreakerAction.OK
        assert status.can_trade is True

    def test_daily_loss_halt(self) -> None:
        """日次損失が3%を超えると新規発注停止."""
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=100_000, daily_pnl=-3_500, peak_balance=100_000, consecutive_losses=0,
        ))
        assert status.action == BreakerAction.HALT_NEW_ORDERS
        assert status.can_trade is False

    def test_max_drawdown_force_close(self) -> None:
        """ドローダウン10%超で全ポジション強制決済."""
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=88_000, daily_pnl=-12_000, peak_balance=100_000, consecutive_losses=0,
        ))
        assert status.action == BreakerAction.FORCE_CLOSE_ALL
        assert status.can_trade is False

    def test_consecutive_losses_reduce(self) -> None:
        """連続3敗でサイズ縮小."""
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=100_000, daily_pnl=-500, peak_balance=100_000, consecutive_losses=3,
        ))
        assert status.action == BreakerAction.REDUCE_SIZE
        assert status.can_trade is True

    def test_reset_daily(self) -> None:
        """リセット後はトレード可能に戻る."""
        cb = CircuitBreaker()
        cb.check(AccountState(
            balance=100_000, daily_pnl=-3_500, peak_balance=100_000, consecutive_losses=0,
        ))
        cb.reset_daily()
        status = cb.check(AccountState(
            balance=100_000, daily_pnl=0, peak_balance=100_000, consecutive_losses=0,
        ))
        assert status.can_trade is True


class TestStopLossManager:
    """StopLossManager のテスト."""

    def test_default_levels_without_price_history(self) -> None:
        """価格履歴なしの場合はデフォルト値を使用."""
        manager = StopLossManager()
        levels = manager.calculate_levels("AAPL", 150.0)
        assert levels.stop_loss < 150.0
        assert levels.take_profit > 150.0
        assert levels.trailing_stop < 150.0

    def test_vwap_exit_signal(self) -> None:
        """VWAP乖離が2%超で撤退シグナル."""
        manager = StopLossManager()
        assert manager.should_exit_vwap(153.0, 150.0) is True  # 2%超
        assert manager.should_exit_vwap(151.0, 150.0) is False  # 2%未満

    def test_vwap_zero(self) -> None:
        """VWAPが0の場合はFalse."""
        manager = StopLossManager()
        assert manager.should_exit_vwap(150.0, 0.0) is False
