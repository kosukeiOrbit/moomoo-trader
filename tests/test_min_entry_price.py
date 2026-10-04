"""Filter P (株価下限 MIN_ENTRY_PRICE) のユニットテスト.

tight_filter_long() は純関数なので snapshot は SimpleNamespace で組む
(moomoo_client を import すると futu 依存を引き込むため)。
他の Filter は閾値を 0 等に固定して Filter P だけを観測する。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

if "futu" not in sys.modules:
    sys.modules["futu"] = MagicMock()

from src.signals.and_filter import AndFilter


def _snap(price: float) -> SimpleNamespace:
    """他の Filter を通過する値を入れた snapshot."""
    return SimpleNamespace(
        symbol="TEST",
        last_price=price,
        amplitude=10.0,          # Filter F 通過
        volume_ratio=3.0,        # Filter I 通過
        gap_pct=0.0,             # Filter H 通過
        pre_change_rate=0.0,     # Filter H 通過
    )


# 他フィルタを無効化して Filter P だけを見る
_OTHERS = [
    patch("config.settings.TIGHT_FILTER_ENABLED", True),
    patch("config.settings.TIGHT_AMPLITUDE_MIN", 0.0),
    patch("config.settings.TIGHT_ATR_PCT_MIN", 0.0),
    patch("config.settings.TIGHT_VOL_RATIO_MIN", 0.0),
    patch("config.settings.TIGHT_OVERHEAT_GUARD_MINUTES", 0),
    patch("config.settings.TIGHT_VWAP_DEV_PCT", 100.0),
]


def _run(price: float, floor: float):
    """Filter P 以外を無効化して tight_filter_long を実行する."""
    filt = AndFilter()
    stack = [*_OTHERS, patch("config.settings.MIN_ENTRY_PRICE", floor)]
    for p in stack:
        p.start()
    try:
        return filt.tight_filter_long(_snap(price), None)
    finally:
        for p in reversed(stack):
            p.stop()


class TestFilterP:

    def test_below_floor_rejected(self) -> None:
        """PSKY の実例 ($9.92 < $20) は除外される."""
        passed, reason = _run(9.92, 20.0)
        assert passed is False
        assert "Filter P" in reason
        assert "9.92" in reason

    def test_at_floor_passes(self) -> None:
        """境界はちょうど $20 なら通す (< 比較)."""
        passed, reason = _run(20.0, 20.0)
        assert passed is True, reason

    def test_above_floor_passes(self) -> None:
        passed, reason = _run(43.88, 20.0)
        assert passed is True, reason

    def test_high_price_passes(self) -> None:
        passed, reason = _run(960.97, 20.0)
        assert passed is True, reason

    def test_floor_zero_disables(self) -> None:
        """MIN_ENTRY_PRICE=0 なら低位株も通る (revert 可能性の担保)."""
        passed, reason = _run(9.92, 0.0)
        assert passed is True, reason

    def test_zero_price_not_rejected_by_filter_p(self) -> None:
        """price=0 (取得失敗) は Filter P の対象外。別経路で弾かれる."""
        passed, reason = _run(0.0, 20.0)
        assert "Filter P" not in reason

    def test_disabled_tight_filter_skips_all(self) -> None:
        """TIGHT_FILTER_ENABLED=false では Filter P も評価されない (仕様の明示)."""
        filt = AndFilter()
        with patch("config.settings.TIGHT_FILTER_ENABLED", False), \
             patch("config.settings.MIN_ENTRY_PRICE", 20.0):
            passed, reason = filt.tight_filter_long(_snap(9.92), None)
        assert passed is True
        assert reason == "tight_filter_disabled"
