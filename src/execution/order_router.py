"""発注・決済管理モジュール.

SIMULATE / REAL 両対応。
約定確認は position_list_query() ベースで行う（order_status は不正確なため）。
SL/TP監視は内部 dict の価格比較で判定する。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from config import settings
from src.data.moomoo_client import MoomooClient, Order, OrderResult
from src.risk.circuit_breaker import CircuitBreaker, AccountState, BreakerAction
from src.risk.stop_loss import Levels, StopLossManager
from src.signals.and_filter import EntryDecision

logger = logging.getLogger(__name__)

# 約定確認のリトライ設定
FILL_CHECK_INTERVAL = 1.0  # 秒
FILL_CHECK_MAX_WAIT = 5.0  # 最大待機秒数


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

    SIMULATE / REAL 共通:
      - moomoo API の place_order() で発注
      - position_list_query() で約定確認
      - 内部 dict で SL/TP 監視
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

        # 重複エントリー防止: 内部 dict チェック
        for pos in self._positions.values():
            if pos.symbol == symbol:
                logger.info("Duplicate entry blocked (internal): %s", symbol)
                return None

        # moomoo API にも重複チェック
        if self._client.has_position(symbol):
            logger.info("Duplicate entry blocked (moomoo): %s", symbol)
            return None

        side = "BUY" if signal.direction == "LONG" else "SELL"
        result = self._client.place_order(Order(symbol=symbol, side=side, quantity=size))
        if result.status == "FAILED":
            logger.error("ENTRY failed: %s %s", symbol, result)
            return result

        # position_list_query() で約定確認（最大5秒待機）
        filled = self._wait_for_fill(symbol)
        if filled:
            fill_price = filled.get("cost_price", price)
            fill_qty = int(filled.get("qty", size))
        else:
            # position_list に出なくても内部管理はする（ラグ対応）
            logger.warning("[%s] 約定確認タイムアウト — 内部管理で続行", symbol)
            fill_price = price
            fill_qty = size

        order_id = result.order_id
        self._positions[order_id] = Position(
            order_id=order_id, symbol=symbol,
            direction=signal.direction, size=fill_qty,
            entry_price=fill_price, levels=levels,
        )
        logger.info(
            "ENTRY: %s %s %d shares @ $%.2f id=%s",
            signal.direction, symbol, fill_qty, fill_price, order_id,
        )
        return OrderResult(
            order_id=order_id, status="FILLED",
            filled_price=fill_price, filled_quantity=fill_qty,
        )

    def _wait_for_fill(self, symbol: str) -> dict | None:
        """position_list_query() で約定を確認する（最大 FILL_CHECK_MAX_WAIT 秒）."""
        elapsed = 0.0
        while elapsed < FILL_CHECK_MAX_WAIT:
            time.sleep(FILL_CHECK_INTERVAL)
            elapsed += FILL_CHECK_INTERVAL
            positions = self._client.get_positions()
            if symbol in positions and positions[symbol]["qty"] > 0:
                logger.info("[%s] 約定確認OK (%.1fs)", symbol, elapsed)
                return positions[symbol]
        return None

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

        # 決済注文を発注
        side = "SELL" if position.direction == "LONG" else "BUY"
        result = self._client.place_order(
            Order(symbol=position.symbol, side=side, quantity=position.size),
        )
        if result.status == "FAILED":
            logger.error("EXIT order failed: %s %s", order_id, position.symbol)
            return None

        # position_list_query() でポジション消滅を確認
        self._wait_for_close(position.symbol)

        if position.direction == "LONG":
            pnl = (exit_price - position.entry_price) * position.size
        else:
            pnl = (position.entry_price - exit_price) * position.size

        logger.info(
            "EXIT: %s %s entry=$%.2f exit=$%.2f pnl=$%.2f reason=%s",
            order_id, position.symbol, position.entry_price,
            exit_price, pnl, reason,
        )

        del self._positions[order_id]

        exit_result = ExitResult(
            order_result=result, position=position,
            exit_price=exit_price, pnl=pnl, reason=reason,
        )
        if self._on_exit:
            try:
                self._on_exit(exit_result)
            except Exception:
                logger.exception("on_exit callback error")
        return exit_result

    def _wait_for_close(self, symbol: str) -> bool:
        """position_list_query() でポジション消滅を確認する."""
        elapsed = 0.0
        while elapsed < FILL_CHECK_MAX_WAIT:
            time.sleep(FILL_CHECK_INTERVAL)
            elapsed += FILL_CHECK_INTERVAL
            if not self._client.has_position(symbol):
                logger.info("[%s] 決済確認OK (%.1fs)", symbol, elapsed)
                return True
        logger.warning("[%s] 決済確認タイムアウト — 内部管理で続行", symbol)
        return False

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
        """SL/TP監視ループ（5秒間隔）.

        内部 dict のポジション情報と現在価格を比較して SL/TP を判定する。
        moomoo API のポーリングは get_snapshot() のみ。
        """
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
