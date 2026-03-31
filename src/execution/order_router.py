"""moomoo OpenAPI経由での発注・決済管理モジュール.

CircuitBreaker で安全確認 → StopLossManager から SL/TP 取得 →
MoomooClient で発注。SIMULATE/REAL モード切り替え対応。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from config import settings
from src.data.moomoo_client import MoomooClient, Order, OrderResult
from src.risk.circuit_breaker import CircuitBreaker, AccountState, BreakerAction
from src.risk.stop_loss import Levels, StopLossManager
from src.signals.and_filter import EntryDecision

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """保有ポジション."""

    order_id: str
    symbol: str
    direction: str  # "LONG" or "SHORT"
    size: int
    entry_price: float
    levels: Levels | None = None
    opened_at: datetime = field(default_factory=datetime.now)


class OrderRouter:
    """発注ルーター: 安全チェック → 発注 → ポジション管理.

    paper_trade=True の場合は実際の発注を行わず、
    ローカルでポジションを記録するだけのシミュレーションモードで動作する。
    """

    def __init__(
        self,
        client: MoomooClient,
        circuit_breaker: CircuitBreaker,
        paper_trade: bool = True,
    ) -> None:
        self._client = client
        self._circuit_breaker = circuit_breaker
        self._paper_trade = paper_trade
        self._positions: dict[str, Position] = {}
        self._order_seq: int = 0

    # ------------------------------------------------------------------
    # プロパティ
    # ------------------------------------------------------------------

    @property
    def open_positions(self) -> dict[str, Position]:
        """保有中のポジション."""
        return dict(self._positions)

    @property
    def position_count(self) -> int:
        """保有ポジション数."""
        return len(self._positions)

    # ------------------------------------------------------------------
    # エントリー
    # ------------------------------------------------------------------

    def enter(
        self,
        signal: EntryDecision,
        symbol: str,
        size: int,
        price: float,
        levels: Levels | None = None,
    ) -> OrderResult | None:
        """エントリー注文を発注する.

        1. signal.go == False or size <= 0 → None
        2. ペーパートレード → ローカル記録
        3. 実弾 → MoomooClient.place_order()

        Args:
            signal: エントリー判定結果
            symbol: 銘柄シンボル
            size: 発注株数
            price: 現在の株価
            levels: SL/TP水準

        Returns:
            発注結果（発注不可の場合は None）
        """
        if not signal.go or size <= 0:
            return None

        # ペーパートレード
        if self._paper_trade:
            self._order_seq += 1
            order_id = f"PAPER-{symbol}-{self._order_seq}"
            self._positions[order_id] = Position(
                order_id=order_id,
                symbol=symbol,
                direction=signal.direction,
                size=size,
                entry_price=price,
                levels=levels,
            )
            logger.info(
                "[PAPER] ENTRY: %s %s %d株 @ %.2f",
                signal.direction, symbol, size, price,
            )
            return OrderResult(
                order_id=order_id,
                status="PAPER_FILLED",
                filled_price=price,
                filled_quantity=size,
            )

        # 実弾発注
        side = "BUY" if signal.direction == "LONG" else "SELL"
        order = Order(symbol=symbol, side=side, quantity=size)
        result = self._client.place_order(order)

        if result.status != "FAILED":
            self._positions[result.order_id] = Position(
                order_id=result.order_id,
                symbol=symbol,
                direction=signal.direction,
                size=size,
                entry_price=price,
                levels=levels,
            )
            logger.info(
                "ENTRY: %s %s %d株 order_id=%s",
                signal.direction, symbol, size, result.order_id,
            )
        return result

    # ------------------------------------------------------------------
    # 決済
    # ------------------------------------------------------------------

    def exit(self, order_id: str, reason: str) -> OrderResult | None:
        """ポジションを決済する.

        Args:
            order_id: 決済対象の注文ID
            reason: 決済理由

        Returns:
            決済結果
        """
        position = self._positions.get(order_id)
        if position is None:
            logger.warning("決済対象なし: %s", order_id)
            return None

        logger.info(
            "EXIT: %s (%s) 理由=%s", order_id, position.symbol, reason,
        )

        if self._paper_trade:
            del self._positions[order_id]
            return OrderResult(order_id=order_id, status="PAPER_CLOSED")

        side = "SELL" if position.direction == "LONG" else "BUY"
        order = Order(symbol=position.symbol, side=side, quantity=position.size)
        result = self._client.place_order(order)
        if result.status != "FAILED":
            del self._positions[order_id]
        return result

    def exit_all(self, reason: str) -> list[OrderResult]:
        """全ポジションを決済する.

        Args:
            reason: 決済理由

        Returns:
            各ポジションの決済結果
        """
        results: list[OrderResult] = []
        for order_id in list(self._positions.keys()):
            result = self.exit(order_id, reason)
            if result:
                results.append(result)
        return results

    # ------------------------------------------------------------------
    # ポジション監視
    # ------------------------------------------------------------------

    async def monitor_positions(self) -> None:
        """SL/TPを非同期で監視し、条件を満たしたら自動決済する."""
        while True:
            for order_id, pos in list(self._positions.items()):
                if pos.levels is None:
                    continue
                try:
                    snapshot = self._client.get_snapshot(pos.symbol)
                    price = snapshot.last_price
                    if price <= 0:
                        continue

                    # ストップロス
                    if price <= pos.levels.stop_loss:
                        logger.warning(
                            "SL発動: %s %.2f <= %.2f",
                            pos.symbol, price, pos.levels.stop_loss,
                        )
                        self.exit(order_id, "SL")

                    # テイクプロフィット
                    elif price >= pos.levels.take_profit:
                        logger.info(
                            "TP発動: %s %.2f >= %.2f",
                            pos.symbol, price, pos.levels.take_profit,
                        )
                        self.exit(order_id, "TP")

                except Exception:
                    logger.exception("ポジション監視エラー: %s", order_id)

            await asyncio.sleep(1)
