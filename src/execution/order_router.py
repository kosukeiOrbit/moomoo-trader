"""発注・決済管理モジュール.

SIMULATE → ローカルポジション管理（FUTUJP API 未対応のため）
REAL     → moomoo API 経由で発注
SL/TP監視・P&L記録・Discord通知は両モードで同じ動作。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from config import settings
from src.data.moomoo_client import MoomooClient, Order, OrderResult
from src.risk.circuit_breaker import CircuitBreaker, AccountState, BreakerAction
from src.risk.stop_loss import Levels, StopLossManager
from src.signals.and_filter import EntryDecision

logger = logging.getLogger(__name__)

_is_simulate = settings.TRADE_ENV == "SIMULATE"


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


@dataclass
class ExitResult:
    """決済結果."""

    order_result: OrderResult
    position: Position
    exit_price: float
    pnl: float
    reason: str


OnExitCallback = Callable[[ExitResult], None]


class OrderRouter:
    """発注ルーター.

    SIMULATE: ローカル dict でポジション管理 (PAPER-{symbol}-{n})
    REAL:     moomoo API place_order() で発注
    """

    def __init__(
        self,
        client: MoomooClient,
        circuit_breaker: CircuitBreaker,
        on_exit: OnExitCallback | None = None,
    ) -> None:
        self._client = client
        self._circuit_breaker = circuit_breaker
        self._positions: dict[str, Position] = {}
        self._on_exit = on_exit
        self._paper_seq: int = 0

    @property
    def open_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    @property
    def position_count(self) -> int:
        return len(self._positions)

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def enter(
        self,
        signal: EntryDecision,
        symbol: str,
        size: int,
        price: float,
        levels: Levels | None = None,
    ) -> OrderResult | None:
        """エントリー注文."""
        if not signal.go or size <= 0:
            return None

        if self.position_count >= settings.MAX_POSITIONS:
            logger.info("[%s] MAX_POSITIONS(%d)に達しているためスキップ", symbol, settings.MAX_POSITIONS)
            return None

        for pos in self._positions.values():
            if pos.symbol == symbol:
                logger.info("Duplicate entry blocked: %s", symbol)
                return None

        if _is_simulate:
            return self._enter_simulate(signal, symbol, size, price, levels)
        return self._enter_real(signal, symbol, size, price, levels)

    def _enter_simulate(
        self, signal: EntryDecision, symbol: str,
        size: int, price: float, levels: Levels | None,
    ) -> OrderResult:
        self._paper_seq += 1
        order_id = f"PAPER-{symbol}-{self._paper_seq}"
        self._positions[order_id] = Position(
            order_id=order_id, symbol=symbol,
            direction=signal.direction, size=size,
            entry_price=price, levels=levels,
        )
        logger.info(
            "[SIMULATE] ENTRY: %s %s %d shares @ $%.2f id=%s",
            signal.direction, symbol, size, price, order_id,
        )
        return OrderResult(
            order_id=order_id, status="FILLED",
            filled_price=price, filled_quantity=size,
        )

    def _enter_real(
        self, signal: EntryDecision, symbol: str,
        size: int, price: float, levels: Levels | None,
    ) -> OrderResult:
        side = "BUY" if signal.direction == "LONG" else "SELL"
        result = self._client.place_order(Order(symbol=symbol, side=side, quantity=size))
        if result.status == "FAILED":
            logger.error("ENTRY failed: %s %s", symbol, result)
            return result
        self._positions[result.order_id] = Position(
            order_id=result.order_id, symbol=symbol,
            direction=signal.direction, size=size,
            entry_price=price, levels=levels,
        )
        logger.info(
            "[REAL] ENTRY: %s %s %d shares @ $%.2f id=%s",
            signal.direction, symbol, size, price, result.order_id,
        )
        return result

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def exit(self, order_id: str, reason: str) -> ExitResult | None:
        """ポジション決済."""
        position = self._positions.get(order_id)
        if position is None:
            logger.warning("EXIT target not found: %s", order_id)
            return None

        exit_price = self._get_exit_price(position.symbol)

        if position.direction == "LONG":
            pnl = (exit_price - position.entry_price) * position.size
        else:
            pnl = (position.entry_price - exit_price) * position.size

        logger.info(
            "EXIT: %s %s entry=$%.2f exit=$%.2f pnl=$%.2f reason=%s",
            order_id, position.symbol, position.entry_price,
            exit_price, pnl, reason,
        )

        if _is_simulate:
            order_result = self._exit_simulate(order_id)
        else:
            order_result = self._exit_real(order_id, position)
            if order_result is None:
                return None

        exit_result = ExitResult(
            order_result=order_result, position=position,
            exit_price=exit_price, pnl=pnl, reason=reason,
        )
        if self._on_exit:
            try:
                self._on_exit(exit_result)
            except Exception:
                logger.exception("on_exit callback error")
        return exit_result

    def _exit_simulate(self, order_id: str) -> OrderResult:
        del self._positions[order_id]
        return OrderResult(order_id=order_id, status="CLOSED")

    def _exit_real(self, order_id: str, position: Position) -> OrderResult | None:
        side = "SELL" if position.direction == "LONG" else "BUY"
        result = self._client.place_order(
            Order(symbol=position.symbol, side=side, quantity=position.size),
        )
        if result.status == "FAILED":
            logger.error("EXIT order failed: %s", order_id)
            return None
        del self._positions[order_id]
        return result

    def exit_all(self, reason: str) -> list[ExitResult]:
        """全ポジション決済."""
        results: list[ExitResult] = []
        for order_id in list(self._positions.keys()):
            result = self.exit(order_id, reason)
            if result:
                results.append(result)
        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_exit_price(self, symbol: str) -> float:
        try:
            snap = self._client.get_snapshot(symbol)
            if snap.last_price > 0:
                return snap.last_price
        except Exception:
            logger.exception("Failed to get exit price: %s", symbol)
        return 0.0

    # ------------------------------------------------------------------
    # Monitor
    # ------------------------------------------------------------------

    async def monitor_positions(self) -> None:
        """SL/TP監視ループ（5秒間隔）."""
        while True:
            for order_id, pos in list(self._positions.items()):
                if pos.levels is None:
                    continue
                try:
                    snap = self._client.get_snapshot(pos.symbol)
                    price = snap.last_price
                    if price <= 0:
                        continue
                    if price <= pos.levels.stop_loss:
                        logger.warning("SL: %s $%.2f <= $%.2f", pos.symbol, price, pos.levels.stop_loss)
                        self.exit(order_id, "SL")
                    elif price >= pos.levels.take_profit:
                        logger.info("TP: %s $%.2f >= $%.2f", pos.symbol, price, pos.levels.take_profit)
                        self.exit(order_id, "TP")
                except Exception:
                    logger.debug("Monitor error: %s", order_id)
            await asyncio.sleep(5)
