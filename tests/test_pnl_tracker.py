"""PnLTracker のユニットテスト."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from src.monitor.pnl_tracker import PnLTracker, TradeRecord


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _register_and_close(
    tracker: PnLTracker,
    order_id: str,
    symbol: str = "AAPL",
    direction: str = "LONG",
    size: int = 10,
    entry: float = 150.0,
    exit_: float = 155.0,
    reason: str = "TP",
) -> float:
    """トレードを登録→決済する."""
    tracker.register(order_id, symbol, direction, size, entry)
    return tracker.close_trade(order_id, exit_, reason)


# =========================================================================
# トレード登録・決済
# =========================================================================

class TestPnLTrackerRegister:
    """register() / close_trade() のテスト."""

    def test_register_creates_open_trade(self) -> None:
        t = PnLTracker()
        t.register("O1", "AAPL", "LONG", 10, 150.0)
        assert t.open_trade_count == 1

    def test_close_trade_long_profit(self) -> None:
        """LONG で値上がり → 正の損益."""
        t = PnLTracker()
        pnl = _register_and_close(t, "O1", entry=150.0, exit_=155.0)
        assert pnl == pytest.approx(50.0)  # (155-150)*10
        assert t.open_trade_count == 0
        assert t.closed_trade_count == 1

    def test_close_trade_long_loss(self) -> None:
        """LONG で値下がり → 負の損益."""
        t = PnLTracker()
        pnl = _register_and_close(t, "O1", entry=150.0, exit_=145.0, reason="SL")
        assert pnl == pytest.approx(-50.0)

    def test_close_trade_short_profit(self) -> None:
        """SHORT で値下がり → 正の損益."""
        t = PnLTracker()
        pnl = _register_and_close(
            t, "O1", direction="SHORT", entry=150.0, exit_=145.0,
        )
        assert pnl == pytest.approx(50.0)

    def test_close_trade_short_loss(self) -> None:
        """SHORT で値上がり → 負の損益."""
        t = PnLTracker()
        pnl = _register_and_close(
            t, "O1", direction="SHORT", entry=150.0, exit_=155.0, reason="SL",
        )
        assert pnl == pytest.approx(-50.0)

    def test_close_unknown_order_returns_zero(self) -> None:
        t = PnLTracker()
        assert t.close_trade("UNKNOWN", 100.0) == 0.0

    def test_close_trade_records_reason(self) -> None:
        t = PnLTracker()
        _register_and_close(t, "O1", reason="センチメント反転")
        assert t._closed_trades[0].reason == "センチメント反転"

    def test_daily_pnl_accumulates(self) -> None:
        """複数トレードで日次PnLが累積する."""
        t = PnLTracker()
        _register_and_close(t, "O1", entry=150.0, exit_=155.0)  # +50
        _register_and_close(t, "O2", entry=150.0, exit_=148.0, reason="SL")  # -20
        assert t.daily_pnl == pytest.approx(30.0)

    def test_total_pnl(self) -> None:
        t = PnLTracker()
        _register_and_close(t, "O1", entry=100.0, exit_=110.0)  # +100
        _register_and_close(t, "O2", entry=100.0, exit_=95.0, reason="SL")  # -50
        assert t.total_pnl == pytest.approx(50.0)


# =========================================================================
# 勝率
# =========================================================================

class TestPnLTrackerWinRate:
    """get_win_rate() のテスト."""

    def test_no_trades(self) -> None:
        t = PnLTracker()
        assert t.get_win_rate() == 0.0

    def test_all_wins(self) -> None:
        t = PnLTracker()
        _register_and_close(t, "O1", entry=100.0, exit_=110.0)
        _register_and_close(t, "O2", entry=100.0, exit_=105.0)
        assert t.get_win_rate() == pytest.approx(1.0)

    def test_all_losses(self) -> None:
        t = PnLTracker()
        _register_and_close(t, "O1", entry=100.0, exit_=90.0, reason="SL")
        assert t.get_win_rate() == pytest.approx(0.0)

    def test_mixed_trades(self) -> None:
        """3勝2敗 → 60%."""
        t = PnLTracker()
        for i in range(3):
            _register_and_close(t, f"W{i}", entry=100.0, exit_=110.0)
        for i in range(2):
            _register_and_close(t, f"L{i}", entry=100.0, exit_=95.0, reason="SL")
        assert t.get_win_rate() == pytest.approx(0.6)

    def test_last_n_trades(self) -> None:
        """直近3トレードの勝率."""
        t = PnLTracker()
        # 古いトレード: 3勝
        for i in range(3):
            _register_and_close(t, f"W{i}", entry=100.0, exit_=110.0)
        # 直近3トレード: 1勝2敗
        _register_and_close(t, "R1", entry=100.0, exit_=110.0)
        _register_and_close(t, "R2", entry=100.0, exit_=90.0, reason="SL")
        _register_and_close(t, "R3", entry=100.0, exit_=90.0, reason="SL")
        assert t.get_win_rate(last_n=3) == pytest.approx(1 / 3)

    def test_last_n_none_is_all(self) -> None:
        """last_n=None は全期間."""
        t = PnLTracker()
        _register_and_close(t, "O1", entry=100.0, exit_=110.0)
        _register_and_close(t, "O2", entry=100.0, exit_=90.0, reason="SL")
        assert t.get_win_rate(last_n=None) == pytest.approx(0.5)


# =========================================================================
# 最大ドローダウン
# =========================================================================

class TestPnLTrackerDrawdown:
    """get_max_drawdown() のテスト."""

    def test_no_trades(self) -> None:
        t = PnLTracker()
        assert t.get_max_drawdown() == 0.0

    def test_all_wins_no_drawdown(self) -> None:
        t = PnLTracker()
        _register_and_close(t, "O1", entry=100.0, exit_=110.0)
        _register_and_close(t, "O2", entry=100.0, exit_=120.0)
        assert t.get_max_drawdown() == 0.0

    def test_drawdown_after_peak(self) -> None:
        """利益 → 損失 のパターン."""
        t = PnLTracker()
        _register_and_close(t, "O1", entry=100.0, exit_=120.0)  # +200 (peak=200)
        _register_and_close(t, "O2", entry=100.0, exit_=80.0, reason="SL")  # -200 (cum=0)
        # DD = (200 - 0) / 200 = 1.0
        assert t.get_max_drawdown() == pytest.approx(1.0)

    def test_partial_drawdown(self) -> None:
        t = PnLTracker()
        _register_and_close(t, "O1", entry=100.0, exit_=110.0)  # +100 (peak=100)
        _register_and_close(t, "O2", entry=100.0, exit_=95.0, reason="SL")  # -50 (cum=50)
        # DD = (100 - 50) / 100 = 0.5
        assert t.get_max_drawdown() == pytest.approx(0.5)


# =========================================================================
# シャープレシオ
# =========================================================================

class TestPnLTrackerSharpe:
    """get_sharpe_ratio() のテスト."""

    def test_less_than_2_trades(self) -> None:
        t = PnLTracker()
        assert t.get_sharpe_ratio() == 0.0
        _register_and_close(t, "O1", entry=100.0, exit_=110.0)
        assert t.get_sharpe_ratio() == 0.0

    def test_positive_sharpe(self) -> None:
        """全て利益 → 正のシャープ."""
        t = PnLTracker()
        _register_and_close(t, "O1", entry=100.0, exit_=110.0)  # +100
        _register_and_close(t, "O2", entry=100.0, exit_=115.0)  # +150
        assert t.get_sharpe_ratio() > 0

    def test_all_same_pnl_returns_zero(self) -> None:
        """全トレードが同じPnL → std=0 → 0."""
        t = PnLTracker()
        _register_and_close(t, "O1", entry=100.0, exit_=110.0)  # +100
        _register_and_close(t, "O2", entry=100.0, exit_=110.0)  # +100
        assert t.get_sharpe_ratio() == 0.0


# =========================================================================
# 日次サマリー
# =========================================================================

class TestPnLTrackerDailySummary:
    """get_daily_summary() のテスト."""

    def test_empty_summary(self) -> None:
        t = PnLTracker()
        s = t.get_daily_summary()
        assert s["daily_pnl"] == 0.0
        assert s["total_trades"] == 0
        assert s["win_rate"] == 0.0
        assert s["open_positions"] == 0

    def test_summary_with_trades(self) -> None:
        t = PnLTracker()
        _register_and_close(t, "O1", entry=100.0, exit_=110.0)
        _register_and_close(t, "O2", entry=100.0, exit_=95.0, reason="SL")
        s = t.get_daily_summary()
        assert s["daily_pnl"] == pytest.approx(50.0)
        assert s["total_trades"] == 2
        assert s["wins"] == 1
        assert s["losses"] == 1
        assert s["win_rate"] == pytest.approx(0.5)
        assert s["date"] == date.today().isoformat()

    def test_summary_includes_sharpe_and_dd(self) -> None:
        t = PnLTracker()
        _register_and_close(t, "O1", entry=100.0, exit_=110.0)
        _register_and_close(t, "O2", entry=100.0, exit_=95.0, reason="SL")
        s = t.get_daily_summary()
        assert "sharpe_ratio" in s
        assert "max_drawdown" in s


# =========================================================================
# リセット
# =========================================================================

class TestPnLTrackerReset:
    """reset_daily() のテスト."""

    def test_reset_clears_daily_pnl(self) -> None:
        t = PnLTracker()
        _register_and_close(t, "O1", entry=100.0, exit_=110.0)
        assert t.daily_pnl != 0
        t.reset_daily()
        assert t.daily_pnl == 0.0

    def test_reset_keeps_closed_trades(self) -> None:
        """リセットしても決済済みトレードは残る."""
        t = PnLTracker()
        _register_and_close(t, "O1", entry=100.0, exit_=110.0)
        t.reset_daily()
        assert t.closed_trade_count == 1


# =========================================================================
# ピーク残高
# =========================================================================

class TestPnLTrackerPeakBalance:
    """update_peak_balance() のテスト."""

    def test_initial_peak_is_zero(self) -> None:
        t = PnLTracker()
        assert t.peak_balance == 0.0

    def test_peak_updates_upward(self) -> None:
        t = PnLTracker()
        t.update_peak_balance(100_000)
        assert t.peak_balance == 100_000
        t.update_peak_balance(105_000)
        assert t.peak_balance == 105_000

    def test_peak_does_not_decrease(self) -> None:
        t = PnLTracker()
        t.update_peak_balance(100_000)
        t.update_peak_balance(90_000)
        assert t.peak_balance == 100_000


# =========================================================================
# CSV 保存・読み込み
# =========================================================================

class TestPnLTrackerCSV:
    """save_to_csv() / load_from_csv() のテスト."""

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        """保存→読み込みで同じデータが復元される."""
        t1 = PnLTracker(csv_dir=tmp_path)
        _register_and_close(t1, "O1", symbol="AAPL", entry=150.0, exit_=155.0, reason="TP")
        _register_and_close(t1, "O2", symbol="NVDA", entry=120.0, exit_=115.0, reason="SL")
        filepath = t1.save_to_csv("test.csv")

        assert filepath.exists()

        t2 = PnLTracker(csv_dir=tmp_path)
        loaded = t2.load_from_csv(filepath)
        assert loaded == 2
        assert t2.closed_trade_count == 2
        assert t2._closed_trades[0].symbol == "AAPL"
        assert t2._closed_trades[0].pnl == pytest.approx(50.0)
        assert t2._closed_trades[0].reason == "TP"
        assert t2._closed_trades[1].symbol == "NVDA"
        assert t2._closed_trades[1].pnl == pytest.approx(-50.0)

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        """保存先ディレクトリが存在しない場合に自動作成する."""
        csv_dir = tmp_path / "sub" / "dir"
        t = PnLTracker(csv_dir=csv_dir)
        _register_and_close(t, "O1", entry=100.0, exit_=110.0)
        filepath = t.save_to_csv("test.csv")
        assert filepath.exists()

    def test_save_empty_trades(self, tmp_path: Path) -> None:
        """トレードなしでもCSVヘッダーのみ保存できる."""
        t = PnLTracker(csv_dir=tmp_path)
        filepath = t.save_to_csv("empty.csv")
        assert filepath.exists()
        content = filepath.read_text()
        assert "order_id" in content  # ヘッダーあり

    def test_load_nonexistent_file(self, tmp_path: Path) -> None:
        t = PnLTracker(csv_dir=tmp_path)
        loaded = t.load_from_csv(tmp_path / "nope.csv")
        assert loaded == 0

    def test_default_filename_uses_today(self, tmp_path: Path) -> None:
        """filenameを省略すると today の日付ファイルになる."""
        t = PnLTracker(csv_dir=tmp_path)
        _register_and_close(t, "O1", entry=100.0, exit_=110.0)
        filepath = t.save_to_csv()
        assert date.today().isoformat() in filepath.name
