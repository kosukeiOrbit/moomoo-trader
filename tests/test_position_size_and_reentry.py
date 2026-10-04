"""10/04 追加機能のユニットテスト.

1) 買付余力の枠数等分サイズ決定 (POSITION_SIZE_BP_SPLIT_ENABLED)
2) LONG の同一セッション内 再エントリー禁止 (LONG_REENTRY_BLOCK_ENABLED)

どちらも運用の .env に依存しないよう、設定値は patch で固定する。
"""

from __future__ import annotations

import sys
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

if "futu" not in sys.modules:
    sys.modules["futu"] = MagicMock()

from src.risk.position_sizer import PositionSizer


# =========================================================================
# 1) 余力等分サイズ決定
# =========================================================================

@patch("config.settings.POSITION_SIZE_BP_SPLIT_ENABLED", True)
@patch("config.settings.POSITION_SIZE_BP_SAFETY", 0.95)
@patch("config.settings.LONG_MAX_POSITIONS", 3)
class TestBpSplitSizing:

    def test_all_slots_get_equal_budget(self) -> None:
        """余力が減っていっても全枠が同額になる (3枠目だけ小さくなる問題の解消)."""
        sizer = PositionSizer()
        bp0 = 11_275.0
        price = 100.0
        deployed = 0.0
        sizes = []
        for _ in range(3):
            shares = sizer.calculate("T", price, bp0 - deployed)
            sizes.append(shares)
            deployed += shares * price
        assert len(set(sizes)) == 1, f"枠ごとに株数が違う: {sizes}"
        assert deployed <= bp0, f"余力超過: {deployed} > {bp0}"

    def test_budget_is_baseline_divided_by_slots(self) -> None:
        sizer = PositionSizer()
        bp0 = 12_000.0
        shares = sizer.calculate("T", 100.0, bp0)
        assert shares == int(bp0 / 3 * 0.95 / 100.0)

    def test_baseline_uses_session_max_not_current(self) -> None:
        """余力が減った後の呼び出しでも baseline は最大値を保持する.

        株数そのものは比較しない。 残余力 $3,000 では $3,800 分を買えないため、
        既存の max_affordable ガードが 30 株に丸めるのが正しい挙動。
        ここで確認したいのは「budget が残余力ではなく baseline から決まること」。
        意図した構成 (budget = baseline / 枠数) では 3 枠合計が baseline を
        下回るので、 このガードは発動しない (test_all_slots_get_equal_budget 参照)。
        """
        sizer = PositionSizer()
        sizer.calculate("A", 100.0, 12_000.0)
        assert sizer._baseline_bp == 12_000.0
        sizer.calculate("B", 100.0, 3_000.0)
        assert sizer._baseline_bp == 12_000.0, "残余力で baseline が下がってはいけない"

    def test_new_session_resets_baseline(self) -> None:
        sizer = PositionSizer()
        sizer.calculate("A", 100.0, 12_000.0)
        sizer._session_key = "1999-01-01"
        sizer.calculate("B", 100.0, 6_000.0)
        assert sizer._baseline_bp == 6_000.0

    @patch("config.settings.SHORT_POSITION_SIZE_USD", 1_500.0)
    def test_short_still_uses_fixed_size(self) -> None:
        """SHORT は等分モードの対象外 (専用の固定額)."""
        sizer = PositionSizer()
        shares = sizer.calculate("T", 100.0, 12_000.0, direction="SHORT")
        assert shares == 15


@patch("config.settings.POSITION_SIZE_BP_SPLIT_ENABLED", False)
@patch("config.settings.POSITION_SIZE_USD", 5_000.0)
def test_disabled_falls_back_to_fixed_size() -> None:
    """フラグ false なら従来の固定額動作 (revert 可能性の担保)."""
    sizer = PositionSizer()
    assert sizer.calculate("T", 100.0, 12_000.0) == 50


# =========================================================================
# 2) 再エントリー禁止
# =========================================================================

def _main():
    import src.main as m
    return m


@patch("config.settings.LONG_REENTRY_BLOCK_ENABLED", True)
class TestLongReentryBlock:

    def test_not_blocked_before_entry(self) -> None:
        m = _main()
        m._long_entered_real.clear()
        assert m._long_reentry_blocked("PSKY") is False

    def test_blocked_after_entry(self) -> None:
        m = _main()
        m._long_entered_real.clear()
        m._mark_long_entered("PSKY")
        assert m._long_reentry_blocked("PSKY") is True

    def test_other_symbol_unaffected(self) -> None:
        m = _main()
        m._long_entered_real.clear()
        m._mark_long_entered("PSKY")
        assert m._long_reentry_blocked("CDNS") is False

    def test_key_is_et_session_date(self) -> None:
        """JST 日付ではなく ET 日付でキーを持つ (JST 深夜 0 時で解除されないこと)."""
        m = _main()
        expected = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        assert m._et_session_key() == expected

    def test_stale_session_key_does_not_block(self) -> None:
        """前セッションの記録は当セッションをブロックしない."""
        m = _main()
        m._long_entered_real.clear()
        m._long_entered_real["PSKY"] = "1999-01-01"
        assert m._long_reentry_blocked("PSKY") is False


@patch("config.settings.LONG_REENTRY_BLOCK_ENABLED", False)
def test_reentry_block_disabled() -> None:
    """フラグ false なら常に通る (revert 可能性の担保)."""
    m = _main()
    m._long_entered_real.clear()
    m._mark_long_entered("PSKY")
    assert m._long_reentry_blocked("PSKY") is False
