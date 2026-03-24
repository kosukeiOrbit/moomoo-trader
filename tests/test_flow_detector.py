"""FlowDetector のユニットテスト."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from src.data.moomoo_client import FlowData, ShortData
from src.signal.flow_detector import FlowDetector, FlowSignal


class TestFlowDetector:
    """FlowDetector のテスト."""

    def _make_detector(self) -> tuple[FlowDetector, MagicMock]:
        mock_client = MagicMock()
        mock_client.get_short_data.return_value = ShortData(
            symbol="AAPL", short_volume=0.0, short_ratio=0.1,
        )
        detector = FlowDetector(mock_client)
        return detector, mock_client

    def test_neutral_when_no_flow(self) -> None:
        """フローデータがゼロの場合はNEUTRAL."""
        detector, mock_client = self._make_detector()
        mock_client.get_institutional_flow.return_value = FlowData(
            symbol="AAPL", big_buy=0.0, big_sell=0.0, net_flow=0.0,
        )
        signal = detector.get_flow_signal("AAPL")
        assert signal.direction == "NEUTRAL"
        assert signal.strength == 0.0

    def test_buy_signal_when_strong_buying(self) -> None:
        """大口買いが閾値を超えた場合はBUY."""
        detector, mock_client = self._make_detector()
        mock_client.get_institutional_flow.return_value = FlowData(
            symbol="AAPL", big_buy=80.0, big_sell=20.0, net_flow=60.0,
        )
        signal = detector.get_flow_signal("AAPL")
        assert signal.direction == "BUY"
        assert signal.strength > 0.0

    def test_sell_signal_when_strong_selling(self) -> None:
        """大口売りが閾値を超えた場合はSELL."""
        detector, mock_client = self._make_detector()
        mock_client.get_institutional_flow.return_value = FlowData(
            symbol="AAPL", big_buy=10.0, big_sell=90.0, net_flow=-80.0,
        )
        signal = detector.get_flow_signal("AAPL")
        assert signal.direction == "SELL"

    def test_short_squeeze_flag(self) -> None:
        """空売り比率が高い場合にshort_squeeze=True."""
        detector, mock_client = self._make_detector()
        mock_client.get_short_data.return_value = ShortData(
            symbol="AAPL", short_volume=1000.0, short_ratio=0.4,
        )
        mock_client.get_institutional_flow.return_value = FlowData(
            symbol="AAPL", big_buy=80.0, big_sell=20.0, net_flow=60.0,
        )
        signal = detector.get_flow_signal("AAPL")
        assert signal.short_squeeze is True

    def test_no_short_squeeze_when_low_ratio(self) -> None:
        """空売り比率が低い場合にshort_squeeze=False."""
        detector, mock_client = self._make_detector()
        mock_client.get_institutional_flow.return_value = FlowData(
            symbol="AAPL", big_buy=80.0, big_sell=20.0, net_flow=60.0,
        )
        signal = detector.get_flow_signal("AAPL")
        assert signal.short_squeeze is False

    def test_flow_history_accumulation(self) -> None:
        """複数回呼び出しでフロー履歴が蓄積される."""
        detector, mock_client = self._make_detector()
        mock_client.get_institutional_flow.return_value = FlowData(
            symbol="AAPL", big_buy=50.0, big_sell=50.0, net_flow=0.0,
        )
        detector.get_flow_signal("AAPL")
        detector.get_flow_signal("AAPL")
        assert len(detector._flow_history["AAPL"]) == 2
