"""Filter T (エントリー時刻の上限 ENTRY_CUTOFF_MINUTES_BEFORE_CLOSE) のテスト.

現在時刻に依存するので and_filter 名前空間の datetime を差し替えて決定論的に検証する。
他の Filter は閾値を 0 等に固定して Filter T だけを観測する。
"""

from __future__ import annotations

import sys
from datetime import datetime as _dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

if "futu" not in sys.modules:
    sys.modules["futu"] = MagicMock()

from src.signals.and_filter import AndFilter

_ET = ZoneInfo("America/New_York")


def _snap() -> SimpleNamespace:
    """他の Filter を通過する値を入れた snapshot."""
    return SimpleNamespace(
        symbol="TEST",
        last_price=300.0,
        amplitude=10.0,
        volume_ratio=3.0,
        gap_pct=0.0,
        pre_change_rate=0.0,
    )


def _run(hour: int, minute: int, cutoff: int):
    """ET の指定時刻として tight_filter_long を実行する."""
    fake_dt = MagicMock()
    fake_dt.now.return_value = _dt(2026, 10, 6, hour, minute, 0, tzinfo=_ET)
    stack = [
        patch("config.settings.TIGHT_FILTER_ENABLED", True),
        patch("config.settings.ENTRY_CUTOFF_MINUTES_BEFORE_CLOSE", cutoff),
        patch("config.settings.MIN_ENTRY_PRICE", 0.0),
        patch("config.settings.TIGHT_AMPLITUDE_MIN", 0.0),
        patch("config.settings.TIGHT_ATR_PCT_MIN", 0.0),
        patch("config.settings.TIGHT_VOL_RATIO_MIN", 0.0),
        patch("config.settings.TIGHT_OVERHEAT_GUARD_MINUTES", 0),
        patch("config.settings.TIGHT_VWAP_DEV_PCT", 100.0),
        patch("src.signals.and_filter.datetime", fake_dt),
    ]
    for p in stack:
        p.start()
    try:
        return AndFilter().tight_filter_long(_snap(), None)
    finally:
        for p in reversed(stack):
            p.stop()


class TestFilterT:

    def test_morning_passes(self) -> None:
        passed, reason = _run(10, 0, 60)   # 残り 350 分
        assert passed is True, reason

    def test_exactly_at_cutoff_passes(self) -> None:
        """ET 14:50 = 残り60分。判定は < なので通す."""
        passed, reason = _run(14, 50, 60)
        assert passed is True, reason

    def test_one_minute_past_cutoff_rejected(self) -> None:
        passed, reason = _run(14, 51, 60)   # 残り 59 分
        assert passed is False
        assert "Filter T" in reason

    def test_real_late_entry_rejected(self) -> None:
        """実績の最遅 ET 15:39 (残り11分、手数料$4.58で gross -$1.96) を止める."""
        passed, reason = _run(15, 39, 60)
        assert passed is False
        assert "Filter T" in reason
        assert "11" in reason

    def test_after_force_exit_rejected(self) -> None:
        """強制決済時刻を過ぎていれば残りは負 → 当然止まる."""
        passed, reason = _run(16, 0, 60)
        assert passed is False
        assert "Filter T" in reason

    def test_cutoff_zero_disables(self) -> None:
        """0 なら無効 (revert 可能性の担保)."""
        passed, reason = _run(15, 39, 0)
        assert passed is True, reason

    def test_disabled_tight_filter_skips_all(self) -> None:
        """TIGHT_FILTER_ENABLED=false では Filter T も評価されない (仕様の明示)."""
        fake_dt = MagicMock()
        fake_dt.now.return_value = _dt(2026, 10, 6, 15, 45, 0, tzinfo=_ET)
        with patch("config.settings.TIGHT_FILTER_ENABLED", False), \
             patch("config.settings.ENTRY_CUTOFF_MINUTES_BEFORE_CLOSE", 60), \
             patch("src.signals.and_filter.datetime", fake_dt):
            passed, reason = AndFilter().tight_filter_long(_snap(), None)
        assert passed is True
        assert reason == "tight_filter_disabled"
