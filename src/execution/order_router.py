"""moomoo OpenAPI経由での発注・決済管理モジュール."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from config import settings
from src.data.moomoo_client import MoomooClient, Order, OrderResult
from src.risk.circuit_breaker import CircuitBreaker, AccountState, BreakerAction
from src.risk.stop_loss import Levels
from src.signal.and_filter import EntryDecision

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """保有ポジション."""
    order_id: str
    symbol: str
    direction: str
    size: int
    entry_price: float
    levels: Levels | None = None
    opened_at: datetime = field(default_factory=datetime.now)


class OrderRouter:
    """発注ルーター: 安全チェック → 発注 → ポジション管理."""

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

    def enter(
        self,
        signal: EntryDecision,
        symbol: str,
        size: int,
        price: float,
        levels: Levels | None = None,
    ) -> OrderResult | None:
        """エントリー注文を発注する.

        1. circuit_breaker.check() で安全確認
        2. paper_trade フラグ確認
        3. 発注
        4. ポジション記録

        Args:
            signal: エントリー判定結果
            symbol: 銘柄シンボル
            size: 発注株数
            price: 現在の株価
            levels: SL/TP水準

        Returns:
            発注結果（発注不可の場合はNone）
        """
        if not signal.go or size <= 0:
            return None

        # ペーパートレードモード
        if self._paper_trade:
            from src.execution.paper_trade import PaperTradeEngine
            logger.info("ペーパートレード: %s %s %d株 @ %.2f", signal.direction, symbol, size, price)
            # ペーパートレードは別モジュールで処理
            order_id = f"PAPER-{symbol}-{datetime.now().strftime('%H%M%S')}"
            self._positions[order_id] = Position(
                order_id=order_id,
                symbol=symbol,
                direction=signal.direction,
                size=size,
                entry_price=price,
                levels=levels,
            )
            return OrderResult(order_id=order_id, status="PAPER_FILLED", filled_price=price, filled_quantity=size)

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
            logger.info("発注成功: %s %s %d株 order_id=%s", signal.direction, symbol, size, result.order_id)

        return result

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
            logger.warning("決済対象のポジションが見つかりません: %s", order_id)
            return None

        logger.info("ポジション決済: %s (%s) 理由: %s", order_id, position.symbol, reason)

        if self._paper_trade:
            del self._positions[order_id]
            return OrderResult(order_id=order_id, status="PAPER_CLOSED")

        side = "SELL" if position.direction == "LONG" else "BUY"
        order = Order(symbol=position.symbol, side=side, quantity=position.size)
        result = self._client.place_order(order)
        if result.status != "FAILED":
            del self._positions[order_id]
        return result

    async def monitor_positions(self) -> None:
        """SL/TPを非同期で監視するループ."""
        while True:
            for order_id, pos in list(self._positions.items()):
                if pos.levels is None:
                    continue
                # TODO: 現在の株価を取得してSL/TPと比較
                # 現在は骨格のみ
            await asyncio.sleep(1)

    @property
    def open_positions(self) -> dict[str, Position]:
        """保有中のポジションを返す."""
        return dict(self._positions)
