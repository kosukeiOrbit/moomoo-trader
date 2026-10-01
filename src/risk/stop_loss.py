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
    # 計算されるが 2026-10-01 時点でどこからも参照されていない (order_router は
    # stop_loss / take_profit のみ参照)。有効化するなら monitor_positions に
    # 追従ロジックを実装する必要がある。現状「設定されているのに効かない」状態。
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
        fallback_atr_pct: float | None = None,
    ) -> Levels:
        """ATRに基づきSL/TP/トレーリングストップを計算する.

        乗数は settings で調整するため、ここでは式のみ記す
        (現行値は LONG: SL 0.7 / TP 1.0、 SHORT: SL 0.7 / TP 0.7)。

        LONG : SL = entry - ATR×ATR_SL_MULTIPLIER
               TP = entry + ATR×ATR_TP_MULTIPLIER
        SHORT: SL = entry + ATR×ATR_SL_MULTIPLIER_SHORT
               TP = entry - ATR×ATR_TP_MULTIPLIER_SHORT

        トレーリングストップ (SL 幅の 0.8 倍) も返すが、**この値はどこからも
        参照されていない** (2026-10-01 にリポジトリ全体を確認)。
        `order_router.monitor_positions` は `stop_loss` と `take_profit` だけを見ており、
        `Levels.trailing_stop` の読み出し箇所は存在しない (計算・ログ出力・テストのみ)。
        実際に機能している決済は **SL / TP / 引け FORCE_CLOSE の 3 つ**。
        なお決済最適化 (TP 縮小・トレーリング・時間決済) は過去に実測で全パターン
        棄却済みなので、 トレーリングが無効であること自体は現状の検証結果と整合する。

        price_history から ATR を計算できない場合:
          - fallback_atr_pct があれば entry_price × その値を使う
            (9/30 追加。 履歴K線の枠切れ時に screener 記録の atr_pct を渡す)
          - なければ従来どおり entry_price × 2%

        注 (9/30): 2% 既定値は Filter G 閾値 3% を下回るため、 従来は
        「ATR 取得に失敗した銘柄は実エントリーに進まない」 という前提があった。
        main.py が Filter G 判定に screener の atr_pct をフォールバック使用する
        ようになったため**この前提はもう成り立たない**。 実弾経路では必ず
        fallback_atr_pct を渡すこと (渡さないと狭すぎる SL で建ててしまう)。

        Args:
            symbol: 銘柄シンボル
            entry_price: エントリー価格
            price_history: 価格履歴DataFrame（high, low, close 列が必要）
            direction: "LONG" or "SHORT"
            fallback_atr_pct: ATR 計算不可時に使う ATR% (0.06 = 6%)

        Returns:
            損切り・利確水準
        """
        atr_value = self._calculate_atr(price_history)
        if atr_value is None or atr_value == 0:
            if fallback_atr_pct and fallback_atr_pct > 0:
                atr_value = entry_price * fallback_atr_pct
                logger.warning(
                    "ATR計算不可: %s → フォールバック ATR%%=%.2f%% を使用 (%.4f)",
                    symbol, fallback_atr_pct * 100, atr_value,
                )
            else:
                atr_value = entry_price * 0.02
                logger.warning(
                    "ATR計算不可: %s デフォルト値を使用 (%.4f)", symbol, atr_value,
                )

        if direction == "SHORT":
            # SHORT 専用 ATR 乗数 (LONG とは非対称、 backlog FINAL-7 推奨)
            sl_mult = settings.ATR_SL_MULTIPLIER_SHORT
            tp_mult = settings.ATR_TP_MULTIPLIER_SHORT
            sl = entry_price + (atr_value * sl_mult)
            tp = entry_price - (atr_value * tp_mult)
            trailing = entry_price + (atr_value * sl_mult * 0.8)
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
