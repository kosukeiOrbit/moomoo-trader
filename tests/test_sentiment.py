"""SentimentAnalyzer のユニットテスト.

モックテスト: 常に実行（API不要）
統合テスト: ANTHROPIC_API_KEY が設定されている場合のみ実行
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from src.signals.sentiment_analyzer import SentimentAnalyzer, SentimentResult, _clamp


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _make_mock_analyzer() -> SentimentAnalyzer:
    """APIクライアントをモックしたAnalyzerを作成する."""
    with patch("src.signals.sentiment_analyzer.anthropic.Anthropic"):
        return SentimentAnalyzer(api_key="test-key")


def _mock_response(text: str) -> MagicMock:
    """Claude APIのレスポンスをモックする."""
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    return resp


# ---------------------------------------------------------------------------
# ユニットテスト（モック）
# ---------------------------------------------------------------------------

class TestClamp:
    """_clamp ヘルパーのテスト."""

    def test_within_range(self) -> None:
        assert _clamp(0.5, -1.0, 1.0) == 0.5

    def test_below_min(self) -> None:
        assert _clamp(-1.5, -1.0, 1.0) == -1.0

    def test_above_max(self) -> None:
        assert _clamp(1.5, -1.0, 1.0) == 1.0


class TestSentimentAnalyzerUnit:
    """SentimentAnalyzer のモックテスト."""

    def test_empty_texts_returns_zero(self) -> None:
        """テキストが空リストの場合はスコア0を返す."""
        analyzer = _make_mock_analyzer()
        result = analyzer.analyze([], "AAPL")
        assert result.score == 0.0
        assert result.confidence == 0.0
        assert "テキストなし" in result.reasoning

    def test_whitespace_only_texts_returns_zero(self) -> None:
        """空白のみのテキストは無効として扱う."""
        analyzer = _make_mock_analyzer()
        result = analyzer.analyze(["", "   ", "\n"], "AAPL")
        assert result.score == 0.0
        assert "有効なテキストなし" in result.reasoning

    def test_bullish_response_parsed(self) -> None:
        """Bullishなレスポンスを正しくパースする."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create = MagicMock(
            return_value=_mock_response(
                '{"score": 0.8, "confidence": 0.9, "reasoning": "非常に強気"}'
            )
        )
        result = analyzer.analyze(["AAPL is going to the moon!"], "AAPL")
        assert result.score == 0.8
        assert result.confidence == 0.9
        assert "強気" in result.reasoning

    def test_bearish_response_parsed(self) -> None:
        """Bearishなレスポンスを正しくパースする."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create = MagicMock(
            return_value=_mock_response(
                '{"score": -0.7, "confidence": 0.85, "reasoning": "弱気センチメント"}'
            )
        )
        result = analyzer.analyze(["AAPL is crashing hard"], "AAPL")
        assert result.score == -0.7
        assert result.confidence == 0.85

    def test_score_clamped_to_valid_range(self) -> None:
        """スコアが範囲外の場合にクランプされる."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create = MagicMock(
            return_value=_mock_response(
                '{"score": 2.5, "confidence": 1.8, "reasoning": "out of range"}'
            )
        )
        result = analyzer.analyze(["test"], "AAPL")
        assert result.score == 1.0
        assert result.confidence == 1.0

    def test_json_with_surrounding_text(self) -> None:
        """JSON前後に余計なテキストがあっても抽出できる."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create = MagicMock(
            return_value=_mock_response(
                'Here is the analysis:\n{"score": 0.5, "confidence": 0.7, "reasoning": "moderate"}\nEnd.'
            )
        )
        result = analyzer.analyze(["test"], "AAPL")
        assert result.score == 0.5
        assert result.confidence == 0.7

    def test_invalid_json_returns_zero(self) -> None:
        """不正なJSONレスポンス時にスコア0を返す."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create = MagicMock(
            return_value=_mock_response("This is not JSON at all")
        )
        result = analyzer.analyze(["some text"], "AAPL")
        assert result.score == 0.0
        assert "パースエラー" in result.reasoning

    def test_api_error_returns_zero(self) -> None:
        """APIError 時にスコア0を返す."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create = MagicMock(
            side_effect=anthropic.APIError(
                message="internal error",
                request=MagicMock(),
                body=None,
            )
        )
        result = analyzer.analyze(["some text"], "AAPL")
        assert result.score == 0.0
        assert "APIエラー" in result.reasoning

    @patch("src.signals.sentiment_analyzer.time.sleep")
    def test_rate_limit_retries(self, mock_sleep: MagicMock) -> None:
        """RateLimitError 時にリトライしてから成功する."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create = MagicMock(
            side_effect=[
                anthropic.RateLimitError(
                    message="rate limited",
                    response=MagicMock(status_code=429, headers={}),
                    body=None,
                ),
                _mock_response('{"score": 0.6, "confidence": 0.8, "reasoning": "retry ok"}'),
            ]
        )
        result = analyzer.analyze(["test"], "AAPL")
        assert result.score == 0.6
        assert mock_sleep.call_count == 1

    @patch("src.signals.sentiment_analyzer.time.sleep")
    def test_connection_error_retries(self, mock_sleep: MagicMock) -> None:
        """APIConnectionError 時にリトライする."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create = MagicMock(
            side_effect=[
                anthropic.APIConnectionError(request=MagicMock()),
                _mock_response('{"score": 0.4, "confidence": 0.7, "reasoning": "recovered"}'),
            ]
        )
        result = analyzer.analyze(["test"], "AAPL")
        assert result.score == 0.4
        assert mock_sleep.call_count == 1

    @patch("src.signals.sentiment_analyzer.time.sleep")
    def test_all_retries_exhausted(self, mock_sleep: MagicMock) -> None:
        """全リトライが失敗した場合にスコア0を返す."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create = MagicMock(
            side_effect=anthropic.RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429, headers={}),
                body=None,
            )
        )
        result = analyzer.analyze(["test"], "AAPL")
        assert result.score == 0.0
        assert "リトライ上限" in result.reasoning
        assert mock_sleep.call_count == 3  # MAX_RETRIES

    def test_batch_limit_trims_texts(self) -> None:
        """BATCH_LIMIT を超えるテキストは切り詰められる."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create = MagicMock(
            return_value=_mock_response(
                '{"score": 0.1, "confidence": 0.5, "reasoning": "batch"}'
            )
        )
        texts = [f"text {i}" for i in range(50)]
        result = analyzer.analyze(texts, "AAPL")
        assert result.score == 0.1
        # messages.create が1回だけ呼ばれる（バッチ処理）
        assert analyzer._client.messages.create.call_count == 1

    def test_rolling_score_calculation(self) -> None:
        """ローリングスコアが正しく移動平均を計算する."""
        analyzer = _make_mock_analyzer()
        now = datetime.now()
        analyzer._score_history["AAPL"] = [
            (now, 0.5),
            (now, 0.7),
            (now, 0.3),
        ]
        rolling = analyzer.get_rolling_score("AAPL", window_minutes=30)
        assert abs(rolling - 0.5) < 0.01

    def test_rolling_score_excludes_old_data(self) -> None:
        """ウィンドウ外の古いデータは除外される."""
        analyzer = _make_mock_analyzer()
        now = datetime.now()
        old = now - timedelta(minutes=60)
        analyzer._score_history["AAPL"] = [
            (old, 0.9),   # 除外される
            (now, 0.3),
        ]
        rolling = analyzer.get_rolling_score("AAPL", window_minutes=30)
        assert abs(rolling - 0.3) < 0.01

    def test_rolling_score_no_data(self) -> None:
        """データがない場合のローリングスコアは0."""
        analyzer = _make_mock_analyzer()
        assert analyzer.get_rolling_score("AAPL") == 0.0

    def test_history_pruned_after_analyze(self) -> None:
        """analyze後に古い履歴が自動削除される."""
        analyzer = _make_mock_analyzer()
        old = datetime.now() - timedelta(minutes=120)
        analyzer._score_history["AAPL"] = [(old, 0.5)]

        analyzer._client.messages.create = MagicMock(
            return_value=_mock_response(
                '{"score": 0.3, "confidence": 0.6, "reasoning": "ok"}'
            )
        )
        analyzer.analyze(["test"], "AAPL")

        # 古いエントリーは削除され、新しいものだけ残る
        assert len(analyzer._score_history["AAPL"]) == 1
        assert analyzer._score_history["AAPL"][0][1] == 0.3


# ---------------------------------------------------------------------------
# 統合テスト（実API）
# ---------------------------------------------------------------------------

HAS_API_KEY = bool(os.getenv("ANTHROPIC_API_KEY"))


@pytest.mark.skipif(not HAS_API_KEY, reason="ANTHROPIC_API_KEY not set")
class TestSentimentAnalyzerIntegration:
    """実APIを使った統合テスト."""

    @pytest.fixture()
    def analyzer(self) -> SentimentAnalyzer:
        return SentimentAnalyzer()

    def test_bullish_text_positive_score(self, analyzer: SentimentAnalyzer) -> None:
        """強気テキストでスコアが +0.3 以上になる."""
        texts = [
            "NVDA just crushed earnings! Revenue up 200%, AI demand is insane.",
            "Massive institutional buying in NVDA, this stock is going to $200.",
            "Every hedge fund is loading up on NVDA, the AI revolution is real.",
        ]
        result = analyzer.analyze(texts, "NVDA")
        print(f"[BULLISH] score={result.score}, confidence={result.confidence}, reasoning={result.reasoning}")
        assert result.score >= 0.3, f"Expected score >= 0.3, got {result.score}"
        assert result.confidence > 0.0

    def test_bearish_text_negative_score(self, analyzer: SentimentAnalyzer) -> None:
        """弱気テキストでスコアが -0.3 以下になる."""
        texts = [
            "TSLA is a disaster. Sales are plummeting in China and Europe.",
            "Elon is distracted, the company has no direction. Sell everything.",
            "Short sellers are piling in, this stock is headed to $50.",
        ]
        result = analyzer.analyze(texts, "TSLA")
        print(f"[BEARISH] score={result.score}, confidence={result.confidence}, reasoning={result.reasoning}")
        assert result.score <= -0.3, f"Expected score <= -0.3, got {result.score}"
        assert result.confidence > 0.0

    def test_mixed_text_moderate_score(self, analyzer: SentimentAnalyzer) -> None:
        """混在テキストではスコアが極端にならない."""
        texts = [
            "AAPL had decent earnings but guidance was weak.",
            "iPhone sales are steady but services growth is slowing.",
            "Some analysts upgraded, others downgraded. Mixed signals.",
        ]
        result = analyzer.analyze(texts, "AAPL")
        print(f"[MIXED] score={result.score}, confidence={result.confidence}, reasoning={result.reasoning}")
        assert -0.7 <= result.score <= 0.7, f"Expected moderate score, got {result.score}"

    def test_rolling_score_after_real_calls(self, analyzer: SentimentAnalyzer) -> None:
        """実API呼び出し後にローリングスコアが記録される."""
        analyzer.analyze(["MSFT cloud revenue is booming, AI integration is great"], "MSFT")
        rolling = analyzer.get_rolling_score("MSFT", window_minutes=5)
        assert rolling != 0.0, "Rolling score should be non-zero after a real API call"
