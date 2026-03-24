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
    direction: str = ""  # "LONG" or ""
    sentiment_score: float = 0.0
    flow_strength: float = 0.0
    reason: str = ""


class AndFilter:
    """センチメントと大口フローのAND条件フィルター.

    4つの条件すべてを満たした場合のみエントリーを許可する:
      1. sentiment.score     > SENTIMENT_THRESHOLD  (+0.3)
      2. flow.direction     == "BUY"
      3. sentiment.confidence > CONFIDENCE_MIN       (0.6)
      4. flow.strength       > FLOW_BUY_THRESHOLD   (0.65)
    """

    def should_enter(
        self,
        sentiment: SentimentResult,
        flow: FlowSignal,
    ) -> EntryDecision:
        """センチメントと大口フローの両シグナルを評価してエントリー判定を行う.

        Args:
            sentiment: センチメント解析結果
            flow: 大口フローシグナル

        Returns:
            エントリー判定結果
        """
        # 各条件を個別に評価し、未達理由を収集
        failures: list[str] = []

        if sentiment.score <= settings.SENTIMENT_THRESHOLD:
            failures.append(
                f"センチメント不足(score={sentiment.score:.2f} <= {settings.SENTIMENT_THRESHOLD})"
            )

        if flow.direction != "BUY":
            failures.append(
                f"フロー方向不一致(direction={flow.direction}, 必要=BUY)"
            )

        if sentiment.confidence <= settings.CONFIDENCE_MIN:
            failures.append(
                f"確信度不足(confidence={sentiment.confidence:.2f} <= {settings.CONFIDENCE_MIN})"
            )

        if flow.strength <= settings.FLOW_BUY_THRESHOLD:
            failures.append(
                f"フロー強度不足(strength={flow.strength:.2f} <= {settings.FLOW_BUY_THRESHOLD})"
            )

        # 1つでも未達があれば不合格
        if failures:
            reason = "AND条件未達: " + "; ".join(failures)
            logger.debug("エントリー見送り: %s", reason)
            return EntryDecision(
                go=False,
                sentiment_score=sentiment.score,
                flow_strength=flow.strength,
                reason=reason,
            )

        # 全条件クリア → LONG エントリー
        reason = (
            f"Bullishセンチメント(score={sentiment.score:.2f}) "
            f"+ 高確信度(confidence={sentiment.confidence:.2f}) "
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
