"""Claude APIを使ったLLMセンチメント解析モジュール."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

import anthropic

from config import settings

logger = logging.getLogger(__name__)


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

    Anthropic SDK の内蔵リトライ (max_retries=3, 指数バックオフ) に任せ、
    アプリ側では追加リトライをしない。
    529 Overloaded が連続する場合は score=0.0 を返してループを続行する。
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

    CACHE_TTL_MINUTES = 30  # テキストキャッシュ有効期限

    def __init__(self, api_key: str | None = None) -> None:
        self._client = anthropic.Anthropic(
            api_key=api_key or settings.ANTHROPIC_API_KEY,
            max_retries=3,       # SDK内蔵リトライ (429/529 で指数バックオフ)
            timeout=30.0,        # 30秒タイムアウト
        )
        # symbol -> [(timestamp, score)] のローリングウィンドウ
        self._score_history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        # テキストキャッシュ: symbol -> (texts_hash, result, timestamp)
        self._cache: dict[str, tuple[str, SentimentResult, datetime]] = {}

    @staticmethod
    def _hash_texts(texts: list[str]) -> str:
        """テキストリストのハッシュ値を計算する."""
        combined = "\n".join(sorted(texts))
        return hashlib.md5(combined.encode("utf-8")).hexdigest()

    def analyze(self, texts: list[str], symbol: str) -> SentimentResult:
        """テキストリストを一括分析してセンチメントスコアを返す.

        Args:
            texts: 掲示板投稿・ニュース記事のテキストリスト
            symbol: 銘柄シンボル

        Returns:
            センチメント解析結果
        """
        if not texts:
            return SentimentResult(score=0.0, confidence=0.0, reasoning="No texts")

        # バッチ上限でトリム & 空文字を除去
        filtered = [t.strip() for t in texts if t.strip()][:self.BATCH_LIMIT]
        if len(filtered) < settings.MIN_TEXTS_FOR_ANALYSIS:
            return SentimentResult(
                score=0.0, confidence=0.0,
                reasoning=f"Skipped: {len(filtered)} texts < {settings.MIN_TEXTS_FOR_ANALYSIS}",
            )

        # キャッシュチェック: 同じテキストなら前回の結果を返す
        texts_hash = self._hash_texts(filtered)
        if symbol in self._cache:
            cached_hash, cached_result, cached_at = self._cache[symbol]
            age = datetime.now() - cached_at
            if cached_hash == texts_hash and age < timedelta(minutes=self.CACHE_TTL_MINUTES):
                logger.debug("[%s] Cache hit (age=%.0fs)", symbol, age.total_seconds())
                return cached_result

        combined = "\n---\n".join(filtered)
        user_message = (
            f"銘柄: {symbol}\n"
            f"以下のテキスト({len(filtered)}件)のセンチメントを総合分析してください:\n\n"
            f"{combined}"
        )

        result = self._call_api(user_message)

        # キャッシュに保存
        self._cache[symbol] = (texts_hash, result, datetime.now())

        # ローリングウィンドウに記録
        if result.score != 0.0 or result.confidence != 0.0:
            self._score_history[symbol].append((datetime.now(), result.score))
            self._prune_history(symbol)

        return result

    def _call_api(self, user_message: str) -> SentimentResult:
        """Claude API を呼び出す（リトライは SDK 内蔵に任せる）."""
        try:
            response = self._client.messages.create(
                model=settings.CLAUDE_MODEL,
                max_tokens=256,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            return self._parse_response(response)

        except anthropic.APIStatusError as e:
            # 529 Overloaded, 500 Internal Error etc. (SDK が max_retries 回リトライ後にここに来る)
            logger.error("Claude API %d: %s", e.status_code, e.message)
            return SentimentResult(
                score=0.0, confidence=0.0,
                reasoning=f"API error {e.status_code}",
            )

        except anthropic.APIConnectionError as e:
            logger.error("Claude API connection error: %s", e)
            return SentimentResult(
                score=0.0, confidence=0.0,
                reasoning=f"Connection error: {e}",
            )

        except anthropic.APIError as e:
            logger.error("Claude API error: %s", e)
            return SentimentResult(
                score=0.0, confidence=0.0,
                reasoning=f"API error: {e}",
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
            logger.error("Response parse error: %s", e)
            return SentimentResult(
                score=0.0, confidence=0.0, reasoning=f"Parse error: {e}",
            )

    def get_rolling_score(self, symbol: str, window_minutes: int = 30) -> float:
        """直近N分間のセンチメントスコアの移動平均を返す."""
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
