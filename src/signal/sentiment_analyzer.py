"""Claude APIを使ったLLMセンチメント解析モジュール."""

from __future__ import annotations

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
    score: float         # -1.0 (Bearish) ~ +1.0 (Bullish)
    confidence: float    # 0.0 ~ 1.0
    reasoning: str


class SentimentAnalyzer:
    """Claude APIによるテキストセンチメント解析エンジン."""

    SYSTEM_PROMPT = (
        "あなたは株式市場のセンチメント分析専門家です。"
        "与えられたテキストを分析し、投資家のセンチメントを判定してください。"
        "皮肉、ジャーゴン、絵文字も考慮してコンテキストを正確に理解してください。"
        "必ず以下のJSON形式で回答してください:\n"
        '{"score": 0.0, "confidence": 0.0, "reasoning": ""}\n'
        "score: -1.0 (強い弱気) ~ +1.0 (強い強気)\n"
        "confidence: 0.0 (確信なし) ~ 1.0 (確信あり)\n"
        "reasoning: 判定理由の簡潔な説明"
    )

    def __init__(self, api_key: str | None = None) -> None:
        self._client = anthropic.Anthropic(
            api_key=api_key or settings.ANTHROPIC_API_KEY,
        )
        # symbol -> [(timestamp, score)] のローリングウィンドウ
        self._score_history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)

    def analyze(self, texts: list[str], symbol: str) -> SentimentResult:
        """テキストリストを一括分析してセンチメントスコアを返す.

        Args:
            texts: 掲示板投稿・ニュース記事のテキストリスト
            symbol: 銘柄シンボル

        Returns:
            センチメント解析結果
        """
        if not texts:
            return SentimentResult(score=0.0, confidence=0.0, reasoning="テキストなし")

        combined = "\n---\n".join(texts[:20])  # APIコスト制御のため20件まで
        user_message = (
            f"銘柄: {symbol}\n"
            f"以下のテキスト({len(texts)}件)のセンチメントを分析してください:\n\n"
            f"{combined}"
        )

        try:
            response = self._client.messages.create(
                model=settings.CLAUDE_MODEL,
                max_tokens=256,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            raw = response.content[0].text
            result_data = json.loads(raw)
            result = SentimentResult(
                score=float(result_data["score"]),
                confidence=float(result_data["confidence"]),
                reasoning=result_data.get("reasoning", ""),
            )
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error("センチメント解析レスポンスのパースに失敗: %s", e)
            return SentimentResult(score=0.0, confidence=0.0, reasoning=f"パースエラー: {e}")
        except anthropic.APIError as e:
            logger.error("Claude API エラー: %s", e)
            return SentimentResult(score=0.0, confidence=0.0, reasoning=f"APIエラー: {e}")

        # ローリングウィンドウに記録
        self._score_history[symbol].append((datetime.now(), result.score))
        return result

    def get_rolling_score(self, symbol: str, window_minutes: int = 30) -> float:
        """直近N分間のセンチメントスコアの移動平均を返す.

        Args:
            symbol: 銘柄シンボル
            window_minutes: ウィンドウ幅（分）

        Returns:
            移動平均スコア
        """
        cutoff = datetime.now() - timedelta(minutes=window_minutes)
        history = self._score_history.get(symbol, [])
        recent = [score for ts, score in history if ts >= cutoff]
        if not recent:
            return 0.0
        return sum(recent) / len(recent)
