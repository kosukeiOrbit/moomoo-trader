"""大口フロー・空売りデータ検出モジュール."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from config import settings
from src.data.moomoo_client import MoomooClient, FlowData

logger = logging.getLogger(__name__)


@dataclass
class FlowSignal:
    """大口フローシグナル."""
    direction: str    # "BUY" | "SELL" | "NEUTRAL"
    strength: float   # 0.0 ~ 1.0
    short_squeeze: bool


class FlowDetector:
    """大口投資家フロー検出エンジン."""

    def __init__(self, client: MoomooClient) -> None:
        self._client = client
        # symbol -> [(timestamp, FlowData)] の時系列記録
        self._flow_history: dict[str, list[tuple[datetime, FlowData]]] = defaultdict(list)

    def _record_flow(self, symbol: str, flow: FlowData) -> None:
        """フローデータを時系列に記録する."""
        self._flow_history[symbol].append((datetime.now(), flow))
        # 1時間以上前のデータを削除
        cutoff = datetime.now() - timedelta(hours=1)
        self._flow_history[symbol] = [
            (ts, f) for ts, f in self._flow_history[symbol] if ts >= cutoff
        ]

    def get_flow_signal(self, symbol: str) -> FlowSignal:
        """指定銘柄の大口フローシグナルを生成する.

        過去15分間の大口フロー累積値を計算し、
        買い超過比率が閾値を超えた場合にBUYシグナルを出す。

        Args:
            symbol: 銘柄シンボル

        Returns:
            大口フローシグナル
        """
        # 最新フローデータを取得・記録
        flow = self._client.get_institutional_flow(symbol)
        self._record_flow(symbol, flow)

        # 過去15分のデータを集計
        cutoff = datetime.now() - timedelta(minutes=15)
        recent = [
            f for ts, f in self._flow_history[symbol] if ts >= cutoff
        ]

        if not recent:
            return FlowSignal(direction="NEUTRAL", strength=0.0, short_squeeze=False)

        total_buy = sum(f.big_buy for f in recent)
        total_sell = sum(f.big_sell for f in recent)
        total = total_buy + total_sell

        if total == 0:
            return FlowSignal(direction="NEUTRAL", strength=0.0, short_squeeze=False)

        buy_ratio = total_buy / total

        # 空売りスクイーズ判定
        short_data = self._client.get_short_data(symbol)
        short_squeeze = short_data.short_ratio > 0.3  # 30%以上で候補

        # 方向判定
        if buy_ratio >= settings.FLOW_BUY_THRESHOLD:
            direction = "BUY"
        elif buy_ratio <= (1 - settings.FLOW_BUY_THRESHOLD):
            direction = "SELL"
        else:
            direction = "NEUTRAL"

        strength = abs(buy_ratio - 0.5) * 2  # 0.0 ~ 1.0 にスケール

        return FlowSignal(
            direction=direction,
            strength=strength,
            short_squeeze=short_squeeze,
        )
