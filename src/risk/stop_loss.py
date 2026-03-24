"""ATRベース動的SL/TP設定モジュール."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd
import pandas_ta as ta

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class Levels:
    """損切り・利確・トレーリングストップの価格水準."""
    stop_loss: float
    take_profit: float
    trailing_stop: float


class StopLossManager:
    """ATRベースの動的SL/TP管理エンジン."""

    def calculate_levels(
        self,
        symbol: str,
        entry_price: float,
        price_history: pd.DataFrame | None = None,
    ) -> Levels:
        """ATRに基づきSL/TP/トレーリングストップを計算する.

        SL = エントリー価格 - (ATR × 1.5)
        TP = エントリー価格 + (ATR × 2.5)
        リスクリワード比 = 1:1.67

        Args:
            symbol: 銘柄シンボル
            entry_price: エントリー価格
            price_history: 価格履歴DataFrame（high, low, close列が必要）

        Returns:
            損切り・利確水準
        """
        atr_value = self._calculate_atr(price_history)
        if atr_value is None or atr_value == 0:
            # ATR計算不可の場合はエントリー価格の2%をデフォルトに
            atr_value = entry_price * 0.02
            logger.warning("ATR計算不可: %s デフォルト値を使用 (%.2f)", symbol, atr_value)

        sl = entry_price - (atr_value * settings.ATR_SL_MULTIPLIER)
        tp = entry_price + (atr_value * settings.ATR_TP_MULTIPLIER)
        trailing = entry_price - (atr_value * settings.ATR_SL_MULTIPLIER * 0.8)

        logger.info(
            "SL/TP計算: %s entry=%.2f SL=%.2f TP=%.2f trailing=%.2f ATR=%.2f",
            symbol, entry_price, sl, tp, trailing, atr_value,
        )
        return Levels(stop_loss=sl, take_profit=tp, trailing_stop=trailing)

    def _calculate_atr(self, price_history: pd.DataFrame | None) -> float | None:
        """ATR（Average True Range）を計算する."""
        if price_history is None or len(price_history) < 14:
            return None
        atr_series = ta.atr(
            high=price_history["high"],
            low=price_history["low"],
            close=price_history["close"],
            length=14,
        )
        if atr_series is None or atr_series.empty:
            return None
        return float(atr_series.iloc[-1])

    def should_exit_vwap(self, current_price: float, vwap: float) -> bool:
        """VWAPからの乖離が閾値を超えた場合にTrue.

        Args:
            current_price: 現在の株価
            vwap: VWAP

        Returns:
            即時撤退すべきかどうか
        """
        if vwap == 0:
            return False
        deviation = abs(current_price - vwap) / vwap
        if deviation > settings.VWAP_DEVIATION_EXIT:
            logger.warning("VWAP乖離%.2f%%超過: 即時撤退推奨", deviation * 100)
            return True
        return False
