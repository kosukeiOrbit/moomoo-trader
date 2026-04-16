"""ATRベース動的SL/TP設定モジュール."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
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
    """ATRベースの動的SL/TP管理エンジン.

    SL = entry - ATR × ATR_SL_MULTIPLIER (1.5)
    TP = entry + ATR × ATR_TP_MULTIPLIER (2.5)
    リスクリワード比 = 2.5 / 1.5 ≈ 1:1.67
    """

    # ------------------------------------------------------------------
    # SL / TP 計算
    # ------------------------------------------------------------------

    def calculate_levels(
        self,
        symbol: str,
        entry_price: float,
        price_history: pd.DataFrame | None = None,
        direction: str = "LONG",
    ) -> Levels:
        """ATRに基づきSL/TP/トレーリングストップを計算する.

        LONG: SL = entry - ATR×1.5,  TP = entry + ATR×2.5
        SHORT: SL = entry + ATR×1.5, TP = entry - ATR×2.5

        Args:
            symbol: 銘柄シンボル
            entry_price: エントリー価格
            price_history: 価格履歴DataFrame（high, low, close 列が必要）
            direction: "LONG" or "SHORT"

        Returns:
            損切り・利確水準
        """
        atr_value = self._calculate_atr(price_history)
        if atr_value is None or atr_value == 0:
            atr_value = entry_price * 0.02
            logger.warning(
                "ATR計算不可: %s デフォルト値を使用 (%.4f)", symbol, atr_value,
            )

        if direction == "SHORT":
            sl = entry_price + (atr_value * settings.ATR_SL_MULTIPLIER)
            tp = entry_price - (atr_value * settings.ATR_TP_MULTIPLIER)
            trailing = entry_price + (atr_value * settings.ATR_SL_MULTIPLIER * 0.8)
        else:
            sl = entry_price - (atr_value * settings.ATR_SL_MULTIPLIER)
            tp = entry_price + (atr_value * settings.ATR_TP_MULTIPLIER)
            trailing = entry_price - (atr_value * settings.ATR_SL_MULTIPLIER * 0.8)

        logger.info(
            "SL/TP計算: %s %s entry=%.2f SL=%.2f TP=%.2f trailing=%.2f ATR=%.4f",
            symbol, direction, entry_price, sl, tp, trailing, atr_value,
        )
        return Levels(stop_loss=sl, take_profit=tp, trailing_stop=trailing)

    # ------------------------------------------------------------------
    # ATR 計算
    # ------------------------------------------------------------------

    def calc_atr_pct(
        self,
        price_history: pd.DataFrame | None,
        entry_price: float,
    ) -> float:
        """ATR を entry_price に対するパーセント（0.0〜1.0）で返す.

        ATR計算不可 or entry<=0 の場合は 0.02（2%）をフォールバック。
        """
        if entry_price <= 0:
            return 0.02
        atr = self._calculate_atr(price_history)
        if atr is None or atr == 0:
            return 0.02
        return atr / entry_price

    def _calculate_atr(
        self,
        price_history: pd.DataFrame | None,
        length: int = 14,
    ) -> float | None:
        """ATR（Average True Range）を計算する.

        Args:
            price_history: high, low, close 列を含む DataFrame
            length: ATR期間（デフォルト14）

        Returns:
            最新のATR値（計算不可の場合はNone）
        """
        if price_history is None or len(price_history) < length:
            return None

        atr_series = ta.atr(
            high=price_history["high"],
            low=price_history["low"],
            close=price_history["close"],
            length=length,
        )
        if atr_series is None or atr_series.empty or atr_series.isna().all():
            return None
        return float(atr_series.iloc[-1])

    # ------------------------------------------------------------------
    # VWAP 計算
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_vwap(price_history: pd.DataFrame) -> float:
        """VWAPを計算する.

        VWAP = Σ(典型価格 × 出来高) / Σ(出来高)

        Args:
            price_history: high, low, close, volume 列を含む DataFrame

        Returns:
            VWAP値（計算不可の場合は0.0）
        """
        required = {"high", "low", "close", "volume"}
        if not required.issubset(price_history.columns):
            return 0.0
        if price_history.empty:
            return 0.0

        typical_price = (
            price_history["high"] + price_history["low"] + price_history["close"]
        ) / 3
        total_volume = price_history["volume"].sum()
        if total_volume == 0:
            return 0.0
        return float((typical_price * price_history["volume"]).sum() / total_volume)

    # ------------------------------------------------------------------
    # VWAP 乖離判定
    # ------------------------------------------------------------------

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
            logger.warning(
                "VWAP乖離 %.2f%% > %.2f%%: 即時撤退推奨",
                deviation * 100,
                settings.VWAP_DEVIATION_EXIT * 100,
            )
            return True
        return False
