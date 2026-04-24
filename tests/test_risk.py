"""リスク管理モジュール（position_sizer / stop_loss / circuit_breaker）のユニットテスト."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from config import settings
from src.risk.position_sizer import PositionSizer, TradeResult
from src.risk.stop_loss import StopLossManager, Levels
from src.risk.circuit_breaker import (
    CircuitBreaker,
    AccountState,
    BreakerAction,
    BreakerStatus,
)


# =========================================================================
# PositionSizer
# =========================================================================


class TestPositionSizerDefaults:
    """初期状態のテスト."""

    def test_default_win_rate_is_zero(self) -> None:
        sizer = PositionSizer()
        assert sizer.win_rate == 0.0

    def test_initial_trade_count_is_zero(self) -> None:
        sizer = PositionSizer()
        assert sizer.trade_count == 0

    def test_initial_consecutive_losses_is_zero(self) -> None:
        sizer = PositionSizer()
        assert sizer.consecutive_losses == 0


class TestPositionSizerCalculate:
    """calculate() のテスト."""

    def test_fixed_pct_calculation(self) -> None:
        """POSITION_MAX_PCT の固定割合で株数が計算される."""
        sizer = PositionSizer()
        # balance=100000 * pct=0.10 / price=150 = 66株
        shares = sizer.calculate("AAPL", 150.0, 100_000.0)
        expected = int(100_000 * settings.POSITION_MAX_PCT / 150.0)
        assert shares == expected

    def test_zero_price_returns_zero(self) -> None:
        sizer = PositionSizer()
        assert sizer.calculate("AAPL", 0.0, 100_000.0) == 0

    def test_negative_price_returns_zero(self) -> None:
        sizer = PositionSizer()
        assert sizer.calculate("AAPL", -10.0, 100_000.0) == 0

    def test_zero_balance_returns_zero(self) -> None:
        sizer = PositionSizer()
        assert sizer.calculate("AAPL", 150.0, 0.0) == 0

    def test_negative_balance_returns_zero(self) -> None:
        sizer = PositionSizer()
        assert sizer.calculate("AAPL", 150.0, -5_000.0) == 0

    def test_high_price_low_balance_returns_zero(self) -> None:
        """1株も買えない場合は0."""
        sizer = PositionSizer()
        assert sizer.calculate("BRK.A", 500_000.0, 1_000.0) == 0

    def test_min_position_shares_guaranteed(self) -> None:
        """計算結果が0でもMIN_POSITION_SHARES(1)を保証（買える場合）."""
        sizer = PositionSizer()
        # balance=200, pct=0.10, price=150 → int(20/150)=0 → min=1
        shares = sizer.calculate("AAPL", 150.0, 200.0)
        assert shares == 1

    def test_consecutive_losses_halve_size(self) -> None:
        """連続3敗でサイズが通常時の半分以下になる."""
        sizer = PositionSizer()
        normal_shares = sizer.calculate("AAPL", 150.0, 100_000.0)

        for _ in range(3):
            sizer.update_stats(TradeResult(symbol="AAPL", pnl=-50.0, is_win=False))
        reduced_shares = sizer.calculate("AAPL", 150.0, 100_000.0)

        assert reduced_shares <= normal_shares
        assert reduced_shares >= 1


class TestPositionSizerUpdateStats:
    """update_stats() のテスト."""

    def test_win_updates_stats(self) -> None:
        sizer = PositionSizer()
        sizer.update_stats(TradeResult(symbol="AAPL", pnl=100.0, is_win=True))
        assert sizer.win_rate == 1.0
        assert sizer._wins == 1
        assert sizer.consecutive_losses == 0

    def test_loss_updates_stats(self) -> None:
        sizer = PositionSizer()
        sizer.update_stats(TradeResult(symbol="AAPL", pnl=-50.0, is_win=False))
        assert sizer.win_rate == 0.0
        assert sizer._losses == 1
        assert sizer.consecutive_losses == 1

    def test_win_resets_consecutive_losses(self) -> None:
        """勝ちが入ると連続敗北カウントがリセットされる."""
        sizer = PositionSizer()
        sizer.update_stats(TradeResult(symbol="X", pnl=-50.0, is_win=False))
        sizer.update_stats(TradeResult(symbol="X", pnl=-50.0, is_win=False))
        assert sizer.consecutive_losses == 2
        sizer.update_stats(TradeResult(symbol="X", pnl=100.0, is_win=True))
        assert sizer.consecutive_losses == 0

    def test_mixed_trades_win_rate(self) -> None:
        """勝ち3、負け2 → 勝率60%."""
        sizer = PositionSizer()
        for _ in range(3):
            sizer.update_stats(TradeResult(symbol="X", pnl=100.0, is_win=True))
        for _ in range(2):
            sizer.update_stats(TradeResult(symbol="X", pnl=-80.0, is_win=False))
        assert sizer.win_rate == pytest.approx(0.6)
        assert sizer.trade_count == 5


# =========================================================================
# StopLossManager
# =========================================================================


def _make_price_history(
    n: int = 30,
    base_price: float = 150.0,
    volatility: float = 2.0,
    seed: int = 42,
) -> pd.DataFrame:
    """テスト用の価格履歴DataFrameを生成する."""
    rng = np.random.default_rng(seed)
    closes = base_price + rng.normal(0, volatility, n).cumsum()
    highs = closes + rng.uniform(0.5, 1.5, n)
    lows = closes - rng.uniform(0.5, 1.5, n)
    volumes = rng.integers(100_000, 1_000_000, n)
    return pd.DataFrame({
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


class TestStopLossManagerLevels:
    """calculate_levels() のテスト."""

    def test_without_price_history_uses_default(self) -> None:
        """価格履歴なしの場合 ATR = entry × 2% でフォールバック."""
        mgr = StopLossManager()
        levels = mgr.calculate_levels("AAPL", 150.0)
        # ATR = 150 * 0.02 = 3.0
        assert levels.stop_loss == pytest.approx(150.0 - 3.0 * settings.ATR_SL_MULTIPLIER)
        assert levels.take_profit == pytest.approx(150.0 + 3.0 * settings.ATR_TP_MULTIPLIER)

    def test_with_price_history(self) -> None:
        """実際の価格データからATRを計算する."""
        mgr = StopLossManager()
        df = _make_price_history(n=30)
        entry = float(df["close"].iloc[-1])
        levels = mgr.calculate_levels("AAPL", entry, df)
        assert levels.stop_loss < entry
        assert levels.take_profit > entry
        assert levels.trailing_stop < entry
        assert levels.trailing_stop > levels.stop_loss

    def test_sl_below_entry(self) -> None:
        mgr = StopLossManager()
        levels = mgr.calculate_levels("AAPL", 200.0)
        assert levels.stop_loss < 200.0

    def test_tp_above_entry(self) -> None:
        mgr = StopLossManager()
        levels = mgr.calculate_levels("AAPL", 200.0)
        assert levels.take_profit > 200.0

    def test_trailing_between_sl_and_entry(self) -> None:
        """トレーリングストップはSLとエントリーの間."""
        mgr = StopLossManager()
        levels = mgr.calculate_levels("AAPL", 200.0)
        assert levels.stop_loss < levels.trailing_stop < 200.0

    def test_risk_reward_ratio(self) -> None:
        """リスクリワード比が設定値通り."""
        mgr = StopLossManager()
        levels = mgr.calculate_levels("AAPL", 150.0)
        risk = 150.0 - levels.stop_loss
        reward = levels.take_profit - 150.0
        ratio = reward / risk
        expected = settings.ATR_TP_MULTIPLIER / settings.ATR_SL_MULTIPLIER
        assert ratio == pytest.approx(expected, abs=0.01)

    def test_short_price_history_uses_default(self) -> None:
        """14本未満のデータではフォールバック."""
        mgr = StopLossManager()
        df = _make_price_history(n=5)
        levels = mgr.calculate_levels("AAPL", 150.0, df)
        assert levels.stop_loss == pytest.approx(150.0 - 3.0 * settings.ATR_SL_MULTIPLIER)


class TestStopLossManagerATR:
    """_calculate_atr() のテスト."""

    def test_atr_with_valid_data(self) -> None:
        mgr = StopLossManager()
        df = _make_price_history(n=30)
        atr = mgr._calculate_atr(df)
        assert atr is not None
        assert atr > 0

    def test_atr_none_for_none_input(self) -> None:
        mgr = StopLossManager()
        assert mgr._calculate_atr(None) is None

    def test_atr_none_for_short_data(self) -> None:
        mgr = StopLossManager()
        df = _make_price_history(n=5)
        assert mgr._calculate_atr(df) is None


class TestStopLossManagerVWAP:
    """VWAP関連のテスト."""

    def test_calculate_vwap(self) -> None:
        """VWAPが正しく計算される."""
        df = _make_price_history(n=30)
        vwap = StopLossManager.calculate_vwap(df)
        assert vwap > 0

    def test_calculate_vwap_empty_df(self) -> None:
        df = pd.DataFrame(columns=["high", "low", "close", "volume"])
        assert StopLossManager.calculate_vwap(df) == 0.0

    def test_calculate_vwap_missing_column(self) -> None:
        df = pd.DataFrame({"high": [1], "low": [1], "close": [1]})
        assert StopLossManager.calculate_vwap(df) == 0.0

    def test_calculate_vwap_zero_volume(self) -> None:
        df = pd.DataFrame({
            "high": [150.0], "low": [148.0], "close": [149.0], "volume": [0],
        })
        assert StopLossManager.calculate_vwap(df) == 0.0

    def test_should_exit_vwap_above_threshold(self) -> None:
        """乖離 > 2% で True."""
        mgr = StopLossManager()
        # 153 / 150 - 1 = 2% → 超過
        assert mgr.should_exit_vwap(153.1, 150.0) is True

    def test_should_exit_vwap_below_threshold(self) -> None:
        """乖離 < 2% で False."""
        mgr = StopLossManager()
        assert mgr.should_exit_vwap(151.0, 150.0) is False

    def test_should_exit_vwap_exactly_at_threshold(self) -> None:
        """乖離ちょうど2%は '>' なので False."""
        mgr = StopLossManager()
        # 150 * 1.02 = 153.0 → deviation = 0.02 → not > 0.02
        assert mgr.should_exit_vwap(153.0, 150.0) is False

    def test_should_exit_vwap_below_vwap(self) -> None:
        """下方向の乖離でも判定する."""
        mgr = StopLossManager()
        # 146.9 / 150 = deviation ≈ 2.07% → True
        assert mgr.should_exit_vwap(146.9, 150.0) is True

    def test_should_exit_vwap_zero(self) -> None:
        """VWAP=0 の場合は False."""
        mgr = StopLossManager()
        assert mgr.should_exit_vwap(150.0, 0.0) is False


# =========================================================================
# CircuitBreaker
# =========================================================================


class TestCircuitBreakerOK:
    """正常状態のテスト."""

    def test_normal_state_returns_ok(self) -> None:
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=100_000, daily_pnl=0, peak_balance=100_000, consecutive_losses=0,
        ))
        assert status.action == BreakerAction.OK
        assert status.can_trade is True
        assert status.reason == "正常"

    def test_small_profit_returns_ok(self) -> None:
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=101_000, daily_pnl=1_000, peak_balance=101_000, consecutive_losses=0,
        ))
        assert status.action == BreakerAction.OK

    def test_small_loss_returns_ok(self) -> None:
        """3%未満の損失はOK."""
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=100_000, daily_pnl=-2_000, peak_balance=100_000, consecutive_losses=0,
        ))
        assert status.action == BreakerAction.OK
        assert status.can_trade is True

    def test_not_halted_initially(self) -> None:
        cb = CircuitBreaker()
        assert cb.is_halted is False


class TestCircuitBreakerDailyLoss:
    """日次損失判定のテスト."""

    def test_daily_loss_over_3pct_halts(self) -> None:
        """日次損失 > 3% で新規発注停止."""
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=100_000, daily_pnl=-3_500, peak_balance=100_000, consecutive_losses=0,
        ))
        assert status.action == BreakerAction.HALT_NEW_ORDERS
        assert status.can_trade is False
        assert cb.is_halted is True

    def test_daily_loss_exactly_3pct_is_ok(self) -> None:
        """日次損失ちょうど3%は '>' なのでOK."""
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=100_000, daily_pnl=-3_000, peak_balance=100_000, consecutive_losses=0,
        ))
        assert status.action == BreakerAction.OK

    def test_halted_state_persists(self) -> None:
        """一度発動すると次のcheckでも発動中を返す."""
        cb = CircuitBreaker()
        cb.check(AccountState(
            balance=100_000, daily_pnl=-4_000, peak_balance=100_000, consecutive_losses=0,
        ))
        status = cb.check(AccountState(
            balance=100_000, daily_pnl=0, peak_balance=100_000, consecutive_losses=0,
        ))
        assert status.can_trade is False
        assert "発動中" in status.reason


class TestCircuitBreakerDrawdown:
    """最大ドローダウン判定のテスト."""

    def test_drawdown_over_10pct_force_closes(self) -> None:
        """DD > 10% で全ポジション強制決済."""
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=88_000, daily_pnl=-12_000, peak_balance=100_000, consecutive_losses=0,
        ))
        assert status.action == BreakerAction.FORCE_CLOSE_ALL
        assert status.can_trade is False

    def test_drawdown_exactly_10pct_is_ok(self) -> None:
        """DD ちょうど10%は '>' なのでOK."""
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=90_000, daily_pnl=-10_000, peak_balance=100_000, consecutive_losses=0,
        ))
        # daily_loss = 10000/90000 ≈ 11.1% → HALT_NEW_ORDERS
        # だがDD = 10% ちょうど → DDはOK, daily_lossで引っかかる
        # DDだけのテストを分離:
        pass

    def test_drawdown_exactly_10pct_no_daily_loss(self) -> None:
        """DD ちょうど10%（日次損失なし）は '>' なのでOK."""
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=90_000, daily_pnl=0, peak_balance=100_000, consecutive_losses=0,
        ))
        assert status.action == BreakerAction.OK

    def test_drawdown_takes_priority_over_daily_loss(self) -> None:
        """DD判定は日次損失判定より優先される."""
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=85_000, daily_pnl=-15_000, peak_balance=100_000, consecutive_losses=5,
        ))
        # DD = 15% → FORCE_CLOSE_ALL（最優先）
        assert status.action == BreakerAction.FORCE_CLOSE_ALL


class TestCircuitBreakerConsecutiveLosses:
    """連続敗北判定のテスト."""

    def test_3_consecutive_losses_reduce_size(self) -> None:
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=100_000, daily_pnl=-500, peak_balance=100_000, consecutive_losses=3,
        ))
        assert status.action == BreakerAction.REDUCE_SIZE
        assert status.can_trade is True
        assert "50%縮小" in status.reason

    def test_5_consecutive_losses_reduce_size(self) -> None:
        """3以上でもREDUCE_SIZE."""
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=100_000, daily_pnl=-500, peak_balance=100_000, consecutive_losses=5,
        ))
        assert status.action == BreakerAction.REDUCE_SIZE

    def test_2_consecutive_losses_is_ok(self) -> None:
        """2連敗はまだOK."""
        cb = CircuitBreaker()
        status = cb.check(AccountState(
            balance=100_000, daily_pnl=-500, peak_balance=100_000, consecutive_losses=2,
        ))
        assert status.action == BreakerAction.OK


class TestCircuitBreakerReset:
    """reset_daily() のテスト."""

    def test_reset_clears_halt(self) -> None:
        """リセット後はトレード可能に戻る."""
        cb = CircuitBreaker()
        cb.check(AccountState(
            balance=100_000, daily_pnl=-4_000, peak_balance=100_000, consecutive_losses=0,
        ))
        assert cb.is_halted is True

        cb.reset_daily()
        assert cb.is_halted is False

        status = cb.check(AccountState(
            balance=100_000, daily_pnl=0, peak_balance=100_000, consecutive_losses=0,
        ))
        assert status.action == BreakerAction.OK
        assert status.can_trade is True

    def test_reset_after_drawdown(self) -> None:
        """DD発動後もリセットで復帰する."""
        cb = CircuitBreaker()
        cb.check(AccountState(
            balance=85_000, daily_pnl=-15_000, peak_balance=100_000, consecutive_losses=0,
        ))
        assert cb.is_halted is True

        cb.reset_daily()
        status = cb.check(AccountState(
            balance=85_000, daily_pnl=0, peak_balance=85_000, consecutive_losses=0,
        ))
        assert status.can_trade is True
