"""AndFilter のユニットテスト (LONG + SHORT)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import sys

import pytest

if "futu" not in sys.modules:
    sys.modules["futu"] = MagicMock()

from src.signals.and_filter import AndFilter, EntryDecision
from src.signals.sentiment_analyzer import SentimentResult
from src.signals.flow_detector import FlowSignal


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _sentiment(score: float = 0.75, confidence: float = 0.85) -> SentimentResult:
    return SentimentResult(score=score, confidence=confidence, reasoning="test")

def _flow(direction: str = "BUY", strength: float = 0.8, short_squeeze: bool = False) -> FlowSignal:
    return FlowSignal(direction=direction, strength=strength, short_squeeze=short_squeeze)


# ---------------------------------------------------------------------------
# LONG テスト
# ---------------------------------------------------------------------------

class TestLong:

    @pytest.fixture()
    def filt(self) -> AndFilter:
        return AndFilter()

    def test_all_conditions_met(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(0.75, 0.85), _flow("BUY", 0.8))
        assert decision.go is True
        assert decision.direction == "LONG"

    def test_reason_contains_bullish(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(0.7, 0.9), _flow("BUY", 0.9))
        assert "Bullish" in decision.reason

    def test_short_squeeze_in_reason(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(0.75, 0.85), _flow("BUY", 0.8, short_squeeze=True))
        assert "ショートスクイーズ" in decision.reason

    def test_sentiment_low_fails(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(0.1, 0.8), _flow("BUY", 0.8))
        assert decision.go is False

    def test_confidence_low_fails(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(0.75, 0.3), _flow("BUY", 0.8))
        assert decision.go is False
        assert "確信度不足" in decision.reason

    def test_flow_strength_low_fails(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(0.75, 0.85), _flow("BUY", 0.3))
        assert decision.go is False
        assert "フロー強度不足" in decision.reason

    def test_score_at_threshold_fails(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(0.6, 0.85), _flow("BUY", 0.8))
        assert decision.go is False

    def test_score_above_threshold_passes(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(0.61, 0.85), _flow("BUY", 0.8))
        assert decision.go is True

    def test_neutral_flow_fails(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(0.75, 0.85), _flow("NEUTRAL", 0.8))
        assert decision.go is False
        assert "フロー方向不一致" in decision.reason


# ---------------------------------------------------------------------------
# SHORT テスト
# ---------------------------------------------------------------------------

@patch("config.settings.ENABLE_SHORT", True)
class TestShort:

    @pytest.fixture()
    def filt(self) -> AndFilter:
        return AndFilter()

    def test_short_all_conditions_met(self, filt: AndFilter) -> None:
        """Bearish sentiment + SELL flow -> SHORT."""
        decision = filt.should_enter(_sentiment(-0.5, 0.8), _flow("SELL", 0.8))
        assert decision.go is True
        assert decision.direction == "SHORT"
        assert "Bearish" in decision.reason

    def test_short_sentiment_not_bearish_enough(self, filt: AndFilter) -> None:
        """score=-0.2 > -0.3 なので不合格."""
        decision = filt.should_enter(_sentiment(-0.2, 0.8), _flow("SELL", 0.8))
        assert decision.go is False
        assert "Bearish不足" in decision.reason

    def test_short_at_threshold_fails(self, filt: AndFilter) -> None:
        """score=-0.3 == threshold は '<' なので不合格."""
        decision = filt.should_enter(_sentiment(-0.3, 0.8), _flow("SELL", 0.8))
        assert decision.go is False

    def test_short_just_below_threshold_passes(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(-0.31, 0.8), _flow("SELL", 0.8))
        assert decision.go is True
        assert decision.direction == "SHORT"

    def test_short_low_confidence_fails(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(-0.5, 0.3), _flow("SELL", 0.8))
        assert decision.go is False
        assert "確信度不足" in decision.reason

    def test_short_low_flow_strength_fails(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(-0.5, 0.8), _flow("SELL", 0.3))
        assert decision.go is False
        assert "フロー強度不足" in decision.reason

    def test_bullish_sentiment_sell_flow_no_short(self, filt: AndFilter) -> None:
        """Bullish sentiment + SELL flow -> neither LONG nor SHORT."""
        decision = filt.should_enter(_sentiment(0.75, 0.85), _flow("SELL", 0.8))
        assert decision.go is False


# ---------------------------------------------------------------------------
# SHORT 無効テスト（クラス外）
# ---------------------------------------------------------------------------

@patch("config.settings.ENABLE_SHORT", False)
def test_short_disabled() -> None:
    """ENABLE_SHORT=False なら SHORT は発動しない."""
    filt = AndFilter()
    decision = filt.should_enter(_sentiment(-0.5, 0.8), _flow("SELL", 0.8))
    assert decision.go is False
    assert "SHORT無効" in decision.reason


# ---------------------------------------------------------------------------
# LONG + SHORT 混在テスト
# ---------------------------------------------------------------------------

@patch("config.settings.ENABLE_SHORT", True)
class TestMixed:

    @pytest.fixture()
    def filt(self) -> AndFilter:
        return AndFilter()

    def test_buy_flow_triggers_long_not_short(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(0.75, 0.85), _flow("BUY", 0.8))
        assert decision.direction == "LONG"

    def test_sell_flow_triggers_short_not_long(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(-0.5, 0.8), _flow("SELL", 0.8))
        assert decision.direction == "SHORT"

    def test_neutral_flow_no_entry(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(0.75, 0.85), _flow("NEUTRAL", 0.8))
        assert decision.go is False

    def test_all_zero_no_entry(self, filt: AndFilter) -> None:
        decision = filt.should_enter(_sentiment(0.0, 0.0), _flow("NEUTRAL", 0.0))
        assert decision.go is False
