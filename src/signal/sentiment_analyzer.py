"""Claude APIを使ったLLMセンチメント解析モジュール."""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

import anthropic

from config import settings

logger = logging.getLogger(__name__)

# リトライ設定
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # 秒


@dataclass
class SentimentResult:
    """センチメント解析結果."""

    score: float  # -1.0 (Bearish) ~ +1.0 (Bullish)
    confidence: float  # 0.0 ~ 1.0
    reasoning: str


def _clamp(value: float, lo: float, hi: float) -> float:
    """値を [lo, hi] の範囲にクランプする."""
    return max(lo, min(hi, value))


class SentimentAnalyzer:
    """Claude APIによるテキストセンチメント解析エンジン.

    複数テキストをバッチ処理して1回のAPIコールで分析し、
    レート制限・接続断に対して指数バックオフでリトライする。
    直近N分のスコアを移動平均として保持する。
    """

    SYSTEM_PROMPT = (
        "あなたは株式市場のセンチメント分析専門家です。\n"
        "与えられたテキストを総合的に分析し、投資家のセンチメントを判定してください。\n"
        "皮肉、ジャーゴン、絵文字も考慮してコンテキストを正確に理解してください。\n\n"
        "必ず以下のJSON形式のみで回答してください（説明文は不要）:\n"
        '{"score": 0.0, "confidence": 0.0, "reasoning": ""}\n\n'
        "score: -1.0 (非常に弱気) ~ +1.0 (非常に強気)\n"
        "confidence: 0.0 (確信なし) ~ 1.0 (強い確信)\n"
        "reasoning: 判定理由の簡潔な説明（日本語）"
    )

    BATCH_LIMIT = 20  # 1回のAPIコールで処理する最大テキスト数

    def __init__(self, api_key: str | None = None) -> None:
        self._client = anthropic.Anthropic(
            api_key=api_key or settings.ANTHROPIC_API_KEY,
        )
        # symbol -> [(timestamp, score)] のローリングウィンドウ
        self._score_history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)

    def analyze(self, texts: list[str], symbol: str) -> SentimentResult:
        """テキストリストを一括分析してセンチメントスコアを返す.

        複数テキストを1つのプロンプトにバッチ結合し、APIコール数を最小化する。
        レート制限・接続断は指数バックオフでリトライする。

        Args:
            texts: 掲示板投稿・ニュース記事のテキストリスト
            symbol: 銘柄シンボル

        Returns:
            センチメント解析結果
        """
        if not texts:
            return SentimentResult(score=0.0, confidence=0.0, reasoning="テキストなし")

        # バッチ上限でトリム & 空文字を除去
        filtered = [t.strip() for t in texts if t.strip()][:self.BATCH_LIMIT]
        if not filtered:
            return SentimentResult(score=0.0, confidence=0.0, reasoning="有効なテキストなし")

        combined = "\n---\n".join(filtered)
        user_message = (
            f"銘柄: {symbol}\n"
            f"以下のテキスト({len(filtered)}件)のセンチメントを総合分析してください:\n\n"
            f"{combined}"
        )

        # APIコール（リトライ付き）
        result = self._call_api_with_retry(user_message)

        # ローリングウィンドウに記録
        if result.score != 0.0 or result.confidence != 0.0:
            self._score_history[symbol].append((datetime.now(), result.score))
            self._prune_history(symbol)

        return result

    def _call_api_with_retry(self, user_message: str) -> SentimentResult:
        """指数バックオフ付きでClaude APIを呼び出す."""
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.messages.create(
                    model=settings.CLAUDE_MODEL,
                    max_tokens=256,
                    system=self.SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_message}],
                )
                return self._parse_response(response)

            except anthropic.RateLimitError as e:
                last_error = e
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "レート制限 (attempt %d/%d): %.1f秒後にリトライ",
                    attempt + 1, MAX_RETRIES, delay,
                )
                time.sleep(delay)

            except anthropic.APIConnectionError as e:
                last_error = e
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "接続エラー (attempt %d/%d): %.1f秒後にリトライ — %s",
                    attempt + 1, MAX_RETRIES, delay, e,
                )
                time.sleep(delay)

            except anthropic.APIError as e:
                logger.error("Claude API エラー: %s", e)
                return SentimentResult(
                    score=0.0, confidence=0.0, reasoning=f"APIエラー: {e}",
                )

        # 全リトライ失敗
        logger.error("APIリトライ上限到達: %s", last_error)
        return SentimentResult(
            score=0.0, confidence=0.0, reasoning=f"リトライ上限到達: {last_error}",
        )

    def _parse_response(self, response: anthropic.types.Message) -> SentimentResult:
        """APIレスポンスをパースしてSentimentResultに変換する."""
        try:
            raw = response.content[0].text
            # JSON部分を抽出（前後に余計なテキストがある場合に対応）
            json_start = raw.find("{")
            json_end = raw.rfind("}") + 1
            if json_start == -1 or json_end == 0:
                raise json.JSONDecodeError("No JSON found", raw, 0)
            json_str = raw[json_start:json_end]
            data = json.loads(json_str)

            score = _clamp(float(data["score"]), -1.0, 1.0)
            confidence = _clamp(float(data["confidence"]), 0.0, 1.0)
            reasoning = str(data.get("reasoning", ""))

            return SentimentResult(
                score=score,
                confidence=confidence,
                reasoning=reasoning,
            )
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as e:
            logger.error("レスポンスのパースに失敗: %s (raw=%s)", e, raw[:200] if 'raw' in dir() else "N/A")
            return SentimentResult(
                score=0.0, confidence=0.0, reasoning=f"パースエラー: {e}",
            )

    def get_rolling_score(self, symbol: str, window_minutes: int = 30) -> float:
        """直近N分間のセンチメントスコアの移動平均を返す.

        Args:
            symbol: 銘柄シンボル
            window_minutes: ウィンドウ幅（分）

        Returns:
            移動平均スコア（データなしの場合は0.0）
        """
        cutoff = datetime.now() - timedelta(minutes=window_minutes)
        history = self._score_history.get(symbol, [])
        recent = [score for ts, score in history if ts >= cutoff]
        if not recent:
            return 0.0
        return sum(recent) / len(recent)

    def _prune_history(self, symbol: str, max_age_minutes: int = 60) -> None:
        """古いスコア履歴を削除する."""
        cutoff = datetime.now() - timedelta(minutes=max_age_minutes)
        self._score_history[symbol] = [
            (ts, score) for ts, score in self._score_history[symbol] if ts >= cutoff
        ]
