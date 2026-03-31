"""AndFilter のユニットテスト."""

from __future__ import annotations

from unittest.mock import MagicMock
import sys

import pytest

# moomoo SDK がインストールされていない環境でもテスト可能にする
if "futu" not in sys.modules:
    sys.modules["futu"] = MagicMock()

from src.signals.and_filter import AndFilter, EntryDecision
from src.signals.sentiment_analyzer import SentimentResult
from src.signals.flow_detector import FlowSignal


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _sentiment(score: float = 0.5, confidence: float = 0.8) -> SentimentResult:
    """テスト用の SentimentResult を生成する."""
    return SentimentResult(score=score, confidence=confidence, reasoning="test")


def _flow(
    direction: str = "BUY",
    strength: float = 0.8,
    short_squeeze: bool = False,
) -> FlowSignal:
    """テスト用の FlowSignal を生成する."""
    return FlowSignal(direction=direction, strength=strength, short_squeeze=short_squeeze)


# ---------------------------------------------------------------------------
# テスト
# ---------------------------------------------------------------------------

class TestAndFilter:
    """AndFilter.should_enter のテスト."""

    @pytest.fixture()
    def filt(self) -> AndFilter:
        return AndFilter()

    # === 全条件OK ===

    def test_all_conditions_met_returns_go_true(self, filt: AndFilter) -> None:
        """全4条件を満たした場合に go=True, direction='LONG' を返す."""
        decision = filt.should_enter(
            _sentiment(score=0.5, confidence=0.8),
            _flow(direction="BUY", strength=0.8),
        )
        assert decision.go is True
        assert decision.direction == "LONG"
        assert decision.sentiment_score == 0.5
        assert decision.flow_strength == 0.8

    def test_all_conditions_met_reason_contains_bullish(self, filt: AndFilter) -> None:
        """合格時のreasonにBullish情報が含まれる."""
        decision = filt.should_enter(
            _sentiment(score=0.7, confidence=0.9),
            _flow(direction="BUY", strength=0.9),
        )
        assert "Bullish" in decision.reason
        assert "大口買い超過" in decision.reason

    def test_short_squeeze_noted_in_reason(self, filt: AndFilter) -> None:
        """ショートスクイーズ候補の場合reasonに記載される."""
        decision = filt.should_enter(
            _sentiment(score=0.5, confidence=0.8),
            _flow(direction="BUY", strength=0.8, short_squeeze=True),
        )
        assert decision.go is True
        assert "ショートスクイーズ" in decision.reason

    # === センチメントのみOK（フロー不合格） ===

    def test_sentiment_only_flow_neutral(self, filt: AndFilter) -> None:
        """センチメントOKだがフロー方向がNEUTRALの場合 go=False."""
        decision = filt.should_enter(
            _sentiment(score=0.5, confidence=0.8),
            _flow(direction="NEUTRAL", strength=0.8),
        )
        assert decision.go is False
        assert "フロー方向不一致" in decision.reason

    def test_sentiment_only_flow_sell(self, filt: AndFilter) -> None:
        """センチメントOKだがフロー方向がSELLの場合 go=False."""
        decision = filt.should_enter(
            _sentiment(score=0.5, confidence=0.8),
            _flow(direction="SELL", strength=0.8),
        )
        assert decision.go is False
        assert "フロー方向不一致" in decision.reason

    def test_sentiment_only_flow_strength_low(self, filt: AndFilter) -> None:
        """センチメントOKだがフロー強度が閾値以下の場合 go=False."""
        decision = filt.should_enter(
            _sentiment(score=0.5, confidence=0.8),
            _flow(direction="BUY", strength=0.3),
        )
        assert decision.go is False
        assert "フロー強度不足" in decision.reason

    # === 大口フローのみOK（センチメント不合格） ===

    def test_flow_only_sentiment_low(self, filt: AndFilter) -> None:
        """フローOKだがセンチメントスコアが低い場合 go=False."""
        decision = filt.should_enter(
            _sentiment(score=0.1, confidence=0.8),
            _flow(direction="BUY", strength=0.8),
        )
        assert decision.go is False
        assert "センチメント不足" in decision.reason

    def test_flow_only_sentiment_negative(self, filt: AndFilter) -> None:
        """フローOKだがセンチメントが負の場合 go=False."""
        decision = filt.should_enter(
            _sentiment(score=-0.5, confidence=0.8),
            _flow(direction="BUY", strength=0.8),
        )
        assert decision.go is False
        assert "センチメント不足" in decision.reason

    # === confidence低い ===

    def test_low_confidence_returns_false(self, filt: AndFilter) -> None:
        """confidenceが閾値以下の場合 go=False."""
        decision = filt.should_enter(
            _sentiment(score=0.5, confidence=0.3),
            _flow(direction="BUY", strength=0.8),
        )
        assert decision.go is False
        assert "確信度不足" in decision.reason

    def test_zero_confidence_returns_false(self, filt: AndFilter) -> None:
        """confidence=0の場合 go=False."""
        decision = filt.should_enter(
            _sentiment(score=0.8, confidence=0.0),
            _flow(direction="BUY", strength=0.8),
        )
        assert decision.go is False
        assert "確信度不足" in decision.reason

    # === 境界値テスト（閾値ちょうど） ===

    def test_score_exactly_at_threshold_returns_false(self, filt: AndFilter) -> None:
        """score == SENTIMENT_THRESHOLD (0.3) は '>' なので不合格."""
        decision = filt.should_enter(
            _sentiment(score=0.3, confidence=0.8),
            _flow(direction="BUY", strength=0.8),
        )
        assert decision.go is False
        assert "センチメント不足" in decision.reason

    def test_score_just_above_threshold_returns_true(self, filt: AndFilter) -> None:
        """score が閾値をわずかに超えた場合は合格."""
        decision = filt.should_enter(
            _sentiment(score=0.31, confidence=0.8),
            _flow(direction="BUY", strength=0.8),
        )
        assert decision.go is True

    def test_confidence_exactly_at_threshold_returns_false(self, filt: AndFilter) -> None:
        """confidence == CONFIDENCE_MIN (0.6) は '>' なので不合格."""
        decision = filt.should_enter(
            _sentiment(score=0.5, confidence=0.6),
            _flow(direction="BUY", strength=0.8),
        )
        assert decision.go is False
        assert "確信度不足" in decision.reason

    def test_confidence_just_above_threshold_returns_true(self, filt: AndFilter) -> None:
        """confidence が閾値をわずかに超えた場合は合格."""
        decision = filt.should_enter(
            _sentiment(score=0.5, confidence=0.61),
            _flow(direction="BUY", strength=0.8),
        )
        assert decision.go is True

    def test_flow_strength_exactly_at_threshold_returns_false(self, filt: AndFilter) -> None:
        """strength == FLOW_BUY_THRESHOLD (0.65) は '>' なので不合格."""
        decision = filt.should_enter(
            _sentiment(score=0.5, confidence=0.8),
            _flow(direction="BUY", strength=0.65),
        )
        assert decision.go is False
        assert "フロー強度不足" in decision.reason

    def test_flow_strength_just_above_threshold_returns_true(self, filt: AndFilter) -> None:
        """strength が閾値をわずかに超えた場合は合格."""
        decision = filt.should_enter(
            _sentiment(score=0.5, confidence=0.8),
            _flow(direction="BUY", strength=0.66),
        )
        assert decision.go is True

    # === 複数条件が同時に未達 ===

    def test_multiple_failures_all_listed_in_reason(self, filt: AndFilter) -> None:
        """複数条件が未達の場合、全未達理由がreasonに列挙される."""
        decision = filt.should_enter(
            _sentiment(score=0.1, confidence=0.3),
            _flow(direction="SELL", strength=0.2),
        )
        assert decision.go is False
        assert "センチメント不足" in decision.reason
        assert "フロー方向不一致" in decision.reason
        assert "確信度不足" in decision.reason
        assert "フロー強度不足" in decision.reason

    def test_all_zero_returns_false(self, filt: AndFilter) -> None:
        """全入力が0/NEUTRALの場合 go=False."""
        decision = filt.should_enter(
            _sentiment(score=0.0, confidence=0.0),
            _flow(direction="NEUTRAL", strength=0.0),
        )
        assert decision.go is False
