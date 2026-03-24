"""SentimentAnalyzer のユニットテスト."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.signal.sentiment_analyzer import SentimentAnalyzer, SentimentResult


class TestSentimentAnalyzer:
    """SentimentAnalyzer のテスト."""

    def _make_analyzer(self) -> SentimentAnalyzer:
        with patch("src.signal.sentiment_analyzer.anthropic.Anthropic"):
            return SentimentAnalyzer(api_key="test-key")

    def test_analyze_empty_texts(self) -> None:
        """テキストが空の場合はスコア0を返す."""
        analyzer = self._make_analyzer()
        result = analyzer.analyze([], "AAPL")
        assert result.score == 0.0
        assert result.confidence == 0.0

    def test_analyze_bullish(self) -> None:
        """Bullishなレスポンスを正しくパースする."""
        analyzer = self._make_analyzer()
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(text='{"score": 0.8, "confidence": 0.9, "reasoning": "Very bullish"}')
        ]
        analyzer._client.messages.create = MagicMock(return_value=mock_response)

        result = analyzer.analyze(["AAPL is going to the moon!"], "AAPL")
        assert result.score == 0.8
        assert result.confidence == 0.9
        assert "bullish" in result.reasoning.lower()

    def test_analyze_bearish(self) -> None:
        """Bearishなレスポンスを正しくパースする."""
        analyzer = self._make_analyzer()
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(text='{"score": -0.7, "confidence": 0.85, "reasoning": "Bearish sentiment"}')
        ]
        analyzer._client.messages.create = MagicMock(return_value=mock_response)

        result = analyzer.analyze(["AAPL is crashing"], "AAPL")
        assert result.score == -0.7
        assert result.confidence == 0.85

    def test_analyze_handles_api_error(self) -> None:
        """APIエラー時にスコア0を返す."""
        import anthropic
        analyzer = self._make_analyzer()
        analyzer._client.messages.create = MagicMock(
            side_effect=anthropic.APIError(
                message="rate limit",
                request=MagicMock(),
                body=None,
            )
        )

        result = analyzer.analyze(["some text"], "AAPL")
        assert result.score == 0.0

    def test_analyze_handles_invalid_json(self) -> None:
        """不正なJSONレスポンス時にスコア0を返す."""
        analyzer = self._make_analyzer()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="not valid json")]
        analyzer._client.messages.create = MagicMock(return_value=mock_response)

        result = analyzer.analyze(["some text"], "AAPL")
        assert result.score == 0.0

    def test_rolling_score(self) -> None:
        """ローリングスコアが正しく計算される."""
        analyzer = self._make_analyzer()
        # 手動でスコア履歴を追加
        from datetime import datetime
        analyzer._score_history["AAPL"] = [
            (datetime.now(), 0.5),
            (datetime.now(), 0.7),
            (datetime.now(), 0.3),
        ]
        rolling = analyzer.get_rolling_score("AAPL", window_minutes=30)
        assert abs(rolling - 0.5) < 0.01

    def test_rolling_score_no_data(self) -> None:
        """データがない場合のローリングスコアは0."""
        analyzer = self._make_analyzer()
        assert analyzer.get_rolling_score("AAPL") == 0.0
