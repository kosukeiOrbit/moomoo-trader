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
        """テキストが空リストの場合はAPIを呼ばずスコア0を返す."""
        analyzer = _make_mock_analyzer()
        result = analyzer.analyze([], "AAPL")
        assert result.score == 0.0
        assert result.confidence == 0.0
        analyzer._client.messages.create.assert_not_called()

    def test_single_text_calls_api(self) -> None:
        """1件でもAPIを呼ぶ (MIN_TEXTS_FOR_ANALYSIS=1)."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create.return_value = _mock_response(
            '{"score": 0.3, "confidence": 0.5, "reasoning": "Slightly bullish"}'
        )
        result = analyzer.analyze(["only one text"], "AAPL")
        assert result.score == 0.3
        analyzer._client.messages.create.assert_called_once()

    def test_whitespace_only_texts_skipped(self) -> None:
        """空白のみのテキストは有効テキスト0件でスキップ."""
        analyzer = _make_mock_analyzer()
        result = analyzer.analyze(["", "   ", "\n"], "AAPL")
        assert result.score == 0.0
        assert "Skipped" in result.reasoning
        analyzer._client.messages.create.assert_not_called()

    def test_two_texts_calls_api(self) -> None:
        """2件以上ならAPIを呼び出す."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create = MagicMock(
            return_value=_mock_response(
                '{"score": 0.5, "confidence": 0.7, "reasoning": "mixed"}'
            )
        )
        result = analyzer.analyze(["text one", "text two"], "AAPL")
        assert result.score == 0.5
        analyzer._client.messages.create.assert_called_once()

    def test_bullish_response_parsed(self) -> None:
        """Bullishなレスポンスを正しくパースする."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create = MagicMock(
            return_value=_mock_response(
                '{"score": 0.8, "confidence": 0.9, "reasoning": "非常に強気"}'
            )
        )
        result = analyzer.analyze(["AAPL is going to the moon!", "Buy buy buy"], "AAPL")
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
        result = analyzer.analyze(["AAPL is crashing hard", "Sell now"], "AAPL")
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
        result = analyzer.analyze(["text a", "text b"], "AAPL")
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
        result = analyzer.analyze(["text a", "text b"], "AAPL")
        assert result.score == 0.5
        assert result.confidence == 0.7

    def test_invalid_json_returns_zero(self) -> None:
        """不正なJSONレスポンス時にスコア0を返す."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create = MagicMock(
            return_value=_mock_response("This is not JSON at all")
        )
        result = analyzer.analyze(["text a", "text b"], "AAPL")
        assert result.score == 0.0
        assert "Parse error" in result.reasoning

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
        result = analyzer.analyze(["text a", "text b"], "AAPL")
        assert result.score == 0.0
        assert "error" in result.reasoning.lower()

    def test_status_error_529_returns_zero(self) -> None:
        """529 Overloaded (SDK がリトライ後に到達) でスコア0を返す."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create = MagicMock(
            side_effect=anthropic.APIStatusError(
                message="Overloaded",
                response=MagicMock(status_code=529, headers={}),
                body=None,
            )
        )
        result = analyzer.analyze(["text a", "text b"], "AAPL")
        assert result.score == 0.0
        assert "529" in result.reasoning

    def test_connection_error_returns_zero(self) -> None:
        """APIConnectionError でスコア0を返す."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create = MagicMock(
            side_effect=anthropic.APIConnectionError(request=MagicMock()),
        )
        result = analyzer.analyze(["text a", "text b"], "AAPL")
        assert result.score == 0.0
        assert "error" in result.reasoning.lower()

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
        analyzer.analyze(["text a", "text b"], "AAPL")

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
        analyzer.analyze(["MSFT cloud revenue is booming", "AI integration is great"], "MSFT")
        rolling = analyzer.get_rolling_score("MSFT", window_minutes=5)
        assert rolling != 0.0, "Rolling score should be non-zero after a real API call"


# ---------------------------------------------------------------------------
# テキストキャッシュのテスト
# ---------------------------------------------------------------------------

class TestSentimentCache:
    """テキストキャッシュの動作テスト."""

    def test_cache_hit_skips_api(self) -> None:
        """同じテキストの2回目はAPI呼び出しをスキップしてキャッシュを返す."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create.return_value = _mock_response(
            '{"score": 0.5, "confidence": 0.8, "reasoning": "Bullish"}'
        )

        texts = ["NVDA earnings beat expectations", "GPU demand is insane"]
        result1 = analyzer.analyze(texts, "NVDA")
        result2 = analyzer.analyze(texts, "NVDA")

        assert result1.score == result2.score
        assert result1.confidence == result2.confidence
        # API は1回だけ呼ばれる
        assert analyzer._client.messages.create.call_count == 1

    def test_cache_miss_on_different_texts(self) -> None:
        """テキストが変わればAPI を再呼び出しする."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create.return_value = _mock_response(
            '{"score": 0.5, "confidence": 0.8, "reasoning": "Bullish"}'
        )

        texts_a = ["Good earnings report", "Revenue up 20%"]
        texts_b = ["Bad earnings report", "Revenue down 20%"]
        analyzer.analyze(texts_a, "AAPL")
        analyzer.analyze(texts_b, "AAPL")

        assert analyzer._client.messages.create.call_count == 2

    def test_cache_expires_after_ttl(self) -> None:
        """キャッシュはTTL経過後に期限切れになる."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create.return_value = _mock_response(
            '{"score": 0.5, "confidence": 0.8, "reasoning": "Bullish"}'
        )

        texts = ["TSLA deliveries record high", "New factory opens"]
        analyzer.analyze(texts, "TSLA")

        # キャッシュのタイムスタンプを6分前に巻き戻す（TTL=5分）
        h, result, ts = analyzer._cache["TSLA"]
        analyzer._cache["TSLA"] = (h, result, ts - timedelta(minutes=16))

        analyzer.analyze(texts, "TSLA")
        # TTL切れなのでAPI が再度呼ばれる
        assert analyzer._client.messages.create.call_count == 2

    def test_cache_independent_per_symbol(self) -> None:
        """キャッシュは銘柄ごとに独立している."""
        analyzer = _make_mock_analyzer()
        analyzer._client.messages.create.return_value = _mock_response(
            '{"score": 0.5, "confidence": 0.8, "reasoning": "Bullish"}'
        )

        texts = ["Great earnings", "Revenue up"]
        analyzer.analyze(texts, "AAPL")
        analyzer.analyze(texts, "NVDA")

        # 別銘柄なので2回呼ばれる
        assert analyzer._client.messages.create.call_count == 2
