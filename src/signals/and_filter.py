"""AND条件フィルター（エントリー判定）モジュール."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config import settings
from src.signals.sentiment_analyzer import SentimentResult
from src.signals.flow_detector import FlowSignal

logger = logging.getLogger(__name__)


@dataclass
class EntryDecision:
    """エントリー判定結果."""

    go: bool
    direction: str = ""  # "LONG" or "SHORT" or ""
    sentiment_score: float = 0.0
    flow_strength: float = 0.0
    reason: str = ""


class AndFilter:
    """センチメントと大口フローのAND条件フィルター.

    LONG 条件 (4つ全て):
      1. sentiment.score      > SENTIMENT_THRESHOLD  (+0.3)
      2. flow.direction      == "BUY"
      3. sentiment.confidence > CONFIDENCE_MIN        (0.6)
      4. flow.strength        > FLOW_BUY_THRESHOLD   (0.65)

    SHORT 条件 (4つ全て + ENABLE_SHORT):
      1. sentiment.score      < SHORT_SENTIMENT_THRESHOLD (-0.3)
      2. flow.direction      == "SELL"
      3. sentiment.confidence > CONFIDENCE_MIN        (0.6)
      4. flow.strength        > FLOW_BUY_THRESHOLD   (0.65)
    """

    def should_enter(
        self,
        sentiment: SentimentResult,
        flow: FlowSignal,
    ) -> EntryDecision:
        """センチメントと大口フローの両シグナルを評価してエントリー判定を行う."""

        # --- LONG 判定 ---
        long_result = self._check_long(sentiment, flow)
        if long_result.go:
            return long_result

        # --- SHORT 判定 ---
        if settings.ENABLE_SHORT:
            short_result = self._check_short(sentiment, flow)
            if short_result.go:
                return short_result

        # --- 不合格理由を収集 ---
        failures = self._collect_failures(sentiment, flow)
        reason = "AND条件未達: " + "; ".join(failures)
        return EntryDecision(
            go=False,
            sentiment_score=sentiment.score,
            flow_strength=flow.strength,
            reason=reason,
        )

    def _check_long(self, sentiment: SentimentResult, flow: FlowSignal) -> EntryDecision:
        """LONG エントリー判定."""
        if (
            sentiment.score > settings.SENTIMENT_THRESHOLD
            and flow.direction == "BUY"
            and sentiment.confidence > settings.CONFIDENCE_MIN
            and flow.strength > settings.FLOW_BUY_THRESHOLD
        ):
            reason = (
                f"Bullishセンチメント(score={sentiment.score:.2f}) "
                f"+ 高確信度(confidence={sentiment.confidence:.2f}) "
                f"+ 大口買い超過(strength={flow.strength:.2f})"
            )
            if flow.short_squeeze:
                reason += " + ショートスクイーズ候補"
            logger.info("LONG エントリーシグナル: %s", reason)
            return EntryDecision(
                go=True, direction="LONG",
                sentiment_score=sentiment.score,
                flow_strength=flow.strength,
                reason=reason,
            )
        return EntryDecision(go=False)

    def _check_short(self, sentiment: SentimentResult, flow: FlowSignal) -> EntryDecision:
        """SHORT エントリー判定."""
        if (
            sentiment.score < settings.SHORT_SENTIMENT_THRESHOLD
            and flow.direction == "SELL"
            and sentiment.confidence > settings.CONFIDENCE_MIN
            and flow.strength > settings.FLOW_BUY_THRESHOLD
        ):
            reason = (
                f"Bearishセンチメント(score={sentiment.score:.2f}) "
                f"+ 高確信度(confidence={sentiment.confidence:.2f}) "
                f"+ 大口売り超過(strength={flow.strength:.2f})"
            )
            logger.info("SHORT エントリーシグナル: %s", reason)
            return EntryDecision(
                go=True, direction="SHORT",
                sentiment_score=sentiment.score,
                flow_strength=flow.strength,
                reason=reason,
            )
        return EntryDecision(go=False)

    def tight_filter_long(
        self,
        snap,
        vwap_price: float | None,
        atr_pct: float | None = None,
        is_dynamic: bool = False,
    ) -> tuple[bool, str]:
        """LONG エントリーの tight filter (高値掴み・動的中ボラ罠 排除).

        条件:
          - A2 (R2): vwap_deviation_pct > 1.0% で除外 (強トレンド例外なし)
          - D  (R1): is_dynamic AND atr_pct ∈ [0.04, 0.05) で除外 (動的小型中ボラ罠)

        Returns:
            (passed: bool, reason: str)
            passed=True ならエントリー可、False ならエントリー禁止
        """
        if not settings.TIGHT_FILTER_ENABLED:
            return True, "tight_filter_disabled"

        # Filter D (R1): dynamic + 中ボラ罠 (SNDK, MU, NOW, TER, WDC 等)
        if (
            is_dynamic
            and atr_pct is not None
            and settings.TIGHT_DYN_MID_ATR_LOW <= atr_pct < settings.TIGHT_DYN_MID_ATR_HIGH
        ):
            return False, (
                f"Filter D: dynamic mid-vol trap "
                f"(atr_pct={atr_pct:.4f} in [{settings.TIGHT_DYN_MID_ATR_LOW}, "
                f"{settings.TIGHT_DYN_MID_ATR_HIGH}))"
            )

        # Filter A2 (R2): VWAP乖離率 (% 表記) で除外 (強トレンド例外を削除)
        vwap_dev_pct = None
        if vwap_price and vwap_price > 0 and snap.last_price > 0:
            vwap_dev_pct = (snap.last_price - vwap_price) / vwap_price * 100

        if vwap_dev_pct is not None and vwap_dev_pct > settings.TIGHT_VWAP_DEV_PCT:
            return False, (
                f"Filter A2: vwap_dev={vwap_dev_pct:.2f}% > {settings.TIGHT_VWAP_DEV_PCT}%"
            )

        return True, "passed"

    def _collect_failures(self, sentiment: SentimentResult, flow: FlowSignal) -> list[str]:
        """LONG/SHORT 両方の未達理由を収集する."""
        failures: list[str] = []

        if flow.direction == "BUY":
            if sentiment.score <= settings.SENTIMENT_THRESHOLD:
                failures.append(f"センチメント不足(score={sentiment.score:.2f} <= {settings.SENTIMENT_THRESHOLD})")
            if sentiment.confidence <= settings.CONFIDENCE_MIN:
                failures.append(f"確信度不足(confidence={sentiment.confidence:.2f} <= {settings.CONFIDENCE_MIN})")
            if flow.strength <= settings.FLOW_BUY_THRESHOLD:
                failures.append(f"フロー強度不足(strength={flow.strength:.2f} <= {settings.FLOW_BUY_THRESHOLD})")
        elif flow.direction == "SELL":
            if not settings.ENABLE_SHORT:
                failures.append("SHORT無効(ENABLE_SHORT=False)")
            elif sentiment.score >= settings.SHORT_SENTIMENT_THRESHOLD:
                failures.append(f"Bearish不足(score={sentiment.score:.2f} >= {settings.SHORT_SENTIMENT_THRESHOLD})")
            if sentiment.confidence <= settings.CONFIDENCE_MIN:
                failures.append(f"確信度不足(confidence={sentiment.confidence:.2f} <= {settings.CONFIDENCE_MIN})")
            if flow.strength <= settings.FLOW_BUY_THRESHOLD:
                failures.append(f"フロー強度不足(strength={flow.strength:.2f} <= {settings.FLOW_BUY_THRESHOLD})")
        else:
            failures.append(f"フロー方向不一致(direction={flow.direction})")

        return failures if failures else ["条件未達"]
