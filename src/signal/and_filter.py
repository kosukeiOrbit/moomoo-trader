"""AND条件フィルター（エントリー判定）モジュール."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config import settings
from src.signal.sentiment_analyzer import SentimentResult
from src.signal.flow_detector import FlowSignal

logger = logging.getLogger(__name__)


@dataclass
class EntryDecision:
    """エントリー判定結果."""
    go: bool
    direction: str = ""        # "LONG" or "SHORT" or ""
    sentiment_score: float = 0.0
    flow_strength: float = 0.0
    reason: str = ""


class AndFilter:
    """センチメントと大口フローのAND条件フィルター."""

    def should_enter(
        self,
        sentiment: SentimentResult,
        flow: FlowSignal,
    ) -> EntryDecision:
        """センチメントと大口フローの両シグナルを評価してエントリー判定を行う.

        AND条件: センチメントスコア > 閾値 AND 大口フロー方向一致 AND 確信度十分

        Args:
            sentiment: センチメント解析結果
            flow: 大口フローシグナル

        Returns:
            エントリー判定結果
        """
        # ロングエントリー判定
        if (
            sentiment.score > settings.SENTIMENT_THRESHOLD
            and flow.direction == "BUY"
            and sentiment.confidence > settings.CONFIDENCE_MIN
        ):
            reason = (
                f"Bullishセンチメント({sentiment.score:.2f}) "
                f"+ 大口買い超過(strength={flow.strength:.2f})"
            )
            if flow.short_squeeze:
                reason += " + ショートスクイーズ候補"
            logger.info("LONG エントリーシグナル: %s", reason)
            return EntryDecision(
                go=True,
                direction="LONG",
                sentiment_score=sentiment.score,
                flow_strength=flow.strength,
                reason=reason,
            )

        # ショートエントリー判定（将来拡張用）
        if (
            sentiment.score < -settings.SENTIMENT_THRESHOLD
            and flow.direction == "SELL"
            and sentiment.confidence > settings.CONFIDENCE_MIN
        ):
            reason = (
                f"Bearishセンチメント({sentiment.score:.2f}) "
                f"+ 大口売り超過(strength={flow.strength:.2f})"
            )
            logger.info("SHORT エントリーシグナル: %s", reason)
            return EntryDecision(
                go=True,
                direction="SHORT",
                sentiment_score=sentiment.score,
                flow_strength=flow.strength,
                reason=reason,
            )

        return EntryDecision(go=False, reason="AND条件未達")
