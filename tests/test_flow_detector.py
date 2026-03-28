"""FlowDetector のユニットテスト."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

# moomoo SDK が未インストールでもテスト可能にする
if "moomoo" not in sys.modules:
    sys.modules["moomoo"] = MagicMock()

from src.data.moomoo_client import FlowData, ShortData
from src.signal.flow_detector import FlowDetector, FlowSignal


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _make_detector(
    short_ratio: float = 0.1,
) -> tuple[FlowDetector, MagicMock]:
    """モッククライアント付き FlowDetector を生成する."""
    mock_client = MagicMock()
    mock_client.get_short_data.return_value = ShortData(
        symbol="AAPL", short_volume=0.0, short_ratio=short_ratio,
    )
    return FlowDetector(mock_client), mock_client


def _set_flow(mock_client: MagicMock, buy: float, sell: float) -> None:
    """モックのフローデータを設定する."""
    mock_client.get_institutional_flow.return_value = FlowData(
        symbol="AAPL", big_buy=buy, big_sell=sell, net_flow=buy - sell,
    )


# ---------------------------------------------------------------------------
# 基本動作
# ---------------------------------------------------------------------------

class TestFlowDetectorBasic:
    """基本的なシグナル生成のテスト."""

    def test_neutral_when_zero_flow(self) -> None:
        """買い・売りともにゼロ → NEUTRAL."""
        det, cli = _make_detector()
        _set_flow(cli, 0.0, 0.0)
        sig = det.get_flow_signal("AAPL")
        assert sig.direction == "NEUTRAL"
        assert sig.strength == 0.0

    def test_buy_signal(self) -> None:
        """買い超過 (80/100=0.8) → BUY."""
        det, cli = _make_detector()
        _set_flow(cli, 80.0, 20.0)
        sig = det.get_flow_signal("AAPL")
        assert sig.direction == "BUY"
        assert sig.strength > 0.0

    def test_sell_signal(self) -> None:
        """売り超過 (10/100=0.1) → SELL."""
        det, cli = _make_detector()
        _set_flow(cli, 10.0, 90.0)
        sig = det.get_flow_signal("AAPL")
        assert sig.direction == "SELL"

    def test_neutral_when_balanced(self) -> None:
        """均衡 (50/100=0.5) → NEUTRAL."""
        det, cli = _make_detector()
        _set_flow(cli, 50.0, 50.0)
        sig = det.get_flow_signal("AAPL")
        assert sig.direction == "NEUTRAL"

    def test_strength_scales_0_to_1(self) -> None:
        """strength は [0.0, 1.0] の範囲."""
        det, cli = _make_detector()
        _set_flow(cli, 100.0, 0.0)
        sig = det.get_flow_signal("AAPL")
        assert 0.0 <= sig.strength <= 1.0


# ---------------------------------------------------------------------------
# ショートスクイーズ
# ---------------------------------------------------------------------------

class TestFlowDetectorShortSqueeze:
    """ショートスクイーズ判定のテスト."""

    def test_short_squeeze_true(self) -> None:
        """空売り比率 > 30% → short_squeeze=True."""
        det, cli = _make_detector(short_ratio=0.4)
        _set_flow(cli, 80.0, 20.0)
        sig = det.get_flow_signal("AAPL")
        assert sig.short_squeeze is True

    def test_short_squeeze_false(self) -> None:
        """空売り比率 <= 30% → short_squeeze=False."""
        det, cli = _make_detector(short_ratio=0.1)
        _set_flow(cli, 80.0, 20.0)
        sig = det.get_flow_signal("AAPL")
        assert sig.short_squeeze is False

    def test_short_squeeze_boundary(self) -> None:
        """空売り比率ちょうど30% → > なので False."""
        det, cli = _make_detector(short_ratio=0.3)
        _set_flow(cli, 80.0, 20.0)
        sig = det.get_flow_signal("AAPL")
        assert sig.short_squeeze is False


# ---------------------------------------------------------------------------
# 履歴蓄積
# ---------------------------------------------------------------------------

class TestFlowDetectorHistory:
    """フロー履歴の蓄積・集計のテスト."""

    def test_history_accumulates(self) -> None:
        """複数回呼び出しで履歴が蓄積される."""
        det, cli = _make_detector()
        _set_flow(cli, 50.0, 50.0)
        det.get_flow_signal("AAPL")
        det.get_flow_signal("AAPL")
        assert len(det._flow_history["AAPL"]) == 2

    def test_old_history_pruned(self) -> None:
        """60分以上前のデータは自動削除される."""
        det, cli = _make_detector()
        _set_flow(cli, 50.0, 50.0)

        old = datetime.now() - timedelta(minutes=90)
        det._flow_history["AAPL"] = [(old, FlowData("AAPL", 50, 50, 0))]

        det.get_flow_signal("AAPL")
        # 古いデータは削除され、新しいデータだけ残る
        assert len(det._flow_history["AAPL"]) == 1

    def test_recent_data_used_for_signal(self) -> None:
        """15分以内のデータのみシグナル計算に使われる."""
        det, cli = _make_detector()

        # 20分前のデータ（ウィンドウ外）
        old = datetime.now() - timedelta(minutes=20)
        det._flow_history["AAPL"] = [
            (old, FlowData("AAPL", 100.0, 0.0, 100.0)),
        ]

        # 最新データは均衡
        _set_flow(cli, 50.0, 50.0)
        sig = det.get_flow_signal("AAPL")
        # 15分以内は最新の50/50のみ → NEUTRAL
        assert sig.direction == "NEUTRAL"
