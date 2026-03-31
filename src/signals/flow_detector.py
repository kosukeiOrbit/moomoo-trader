"""大口フロー・空売りデータ検出モジュール.

MoomooClient の get_capital_flow() (分足時系列) から
直近15分間の大口フロー累積を計算して FlowSignal を返す。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from config import settings
from src.data.moomoo_client import MoomooClient, FlowData

logger = logging.getLogger(__name__)

# 空売り比率の閾値（大口売り超過が60%以上でショートスクイーズ候補）
SHORT_SQUEEZE_THRESHOLD = 0.6

# フロー集計ウィンドウ（分）
FLOW_WINDOW_MINUTES = 15

# 履歴保持上限（分）
HISTORY_MAX_MINUTES = 60


@dataclass
class FlowSignal:
    """大口フローシグナル."""

    direction: str  # "BUY" | "SELL" | "NEUTRAL"
    strength: float  # 0.0 ~ 1.0
    short_squeeze: bool


class FlowDetector:
    """大口投資家フロー検出エンジン.

    get_capital_flow() の super_in_flow + big_in_flow を大口フローとして
    15分ウィンドウで累積し、買い超過比率を計算する。
    """

    def __init__(self, client: MoomooClient) -> None:
        self._client = client
        # symbol -> [(timestamp, FlowData)]
        self._flow_history: dict[str, list[tuple[datetime, FlowData]]] = defaultdict(list)

    # ------------------------------------------------------------------
    # 公開API
    # ------------------------------------------------------------------

    def get_flow_signal(self, symbol: str) -> FlowSignal:
        """指定銘柄の大口フローシグナルを生成する.

        Args:
            symbol: 銘柄シンボル

        Returns:
            大口フローシグナル
        """
        # 最新フローデータを取得・記録
        flow = self._client.get_institutional_flow(symbol)
        self._record(symbol, flow)

        # 過去15分のデータを集計
        recent = self._get_recent(symbol, minutes=FLOW_WINDOW_MINUTES)
        if not recent:
            logger.debug("[%s] No recent flow data", symbol)
            return FlowSignal(direction="NEUTRAL", strength=0.0, short_squeeze=False)

        total_buy = sum(f.big_buy for f in recent)
        total_sell = sum(f.big_sell for f in recent)
        total = total_buy + total_sell

        if total == 0:
            logger.debug("[%s] Flow total=0 (no big order activity)", symbol)
            return FlowSignal(direction="NEUTRAL", strength=0.0, short_squeeze=False)

        buy_ratio = total_buy / total

        # 空売りスクイーズ判定
        short_data = self._client.get_short_data(symbol)
        short_squeeze = short_data.short_ratio > SHORT_SQUEEZE_THRESHOLD

        # 方向判定
        if buy_ratio >= settings.FLOW_BUY_THRESHOLD:
            direction = "BUY"
        elif buy_ratio <= (1 - settings.FLOW_BUY_THRESHOLD):
            direction = "SELL"
        else:
            direction = "NEUTRAL"

        # 強度: 0.5 からの乖離を [0.0, 1.0] にスケール
        strength = abs(buy_ratio - 0.5) * 2

        logger.info(
            "[%s] Flow: buy=%.0f sell=%.0f ratio=%.3f -> %s(%.2f) short_ratio=%.3f squeeze=%s [%d samples]",
            symbol, total_buy, total_sell, buy_ratio,
            direction, strength, short_data.short_ratio, short_squeeze,
            len(recent),
        )

        return FlowSignal(
            direction=direction,
            strength=strength,
            short_squeeze=short_squeeze,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _record(self, symbol: str, flow: FlowData) -> None:
        """フローデータを時系列に記録し、古いデータを削除する."""
        now = datetime.now()
        self._flow_history[symbol].append((now, flow))
        cutoff = now - timedelta(minutes=HISTORY_MAX_MINUTES)
        self._flow_history[symbol] = [
            (ts, f) for ts, f in self._flow_history[symbol] if ts >= cutoff
        ]

    def _get_recent(self, symbol: str, minutes: int) -> list[FlowData]:
        """直近N分間のフローデータを返す."""
        cutoff = datetime.now() - timedelta(minutes=minutes)
        return [f for ts, f in self._flow_history[symbol] if ts >= cutoff]
