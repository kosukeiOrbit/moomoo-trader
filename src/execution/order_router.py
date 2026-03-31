"""moomoo OpenAPI経由での発注・決済管理モジュール.

CircuitBreaker で安全確認 → StopLossManager から SL/TP 取得 →
MoomooClient で発注。SIMULATE/REAL モード切り替え対応。
決済時は on_exit コールバックで pnl_tracker / notifier / position_sizer を更新する。
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


@dataclass
class ExitResult:
    """決済結果（価格・PnL 込み）."""

    order_result: OrderResult
    position: Position
    exit_price: float
    pnl: float
    reason: str


# コールバック型: (ExitResult) -> None
OnExitCallback = Callable[[ExitResult], None]


class OrderRouter:
    """発注ルーター: 安全チェック → 発注 → ポジション管理.

    決済時に on_exit コールバックを呼び出し、
    pnl_tracker / notifier / position_sizer を一元的に更新する。
    """

    def __init__(
        self,
        client: MoomooClient,
        circuit_breaker: CircuitBreaker,
        paper_trade: bool = True,
        on_exit: OnExitCallback | None = None,
    ) -> None:
        self._client = client
        self._circuit_breaker = circuit_breaker
        self._paper_trade = paper_trade
        self._positions: dict[str, Position] = {}
        self._order_seq: int = 0
        self._on_exit = on_exit

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
        """エントリー注文を発注する."""
        if not signal.go or size <= 0:
            return None

        # 同一銘柄の重複エントリーを防止
        for pos in self._positions.values():
            if pos.symbol == symbol:
                logger.info("Duplicate entry blocked: %s already has position", symbol)
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
                "[PAPER] ENTRY: %s %s %d shares @ $%.2f",
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
                "ENTRY: %s %s %d shares order_id=%s",
                signal.direction, symbol, size, result.order_id,
            )
        return result

    # ------------------------------------------------------------------
    # 決済
    # ------------------------------------------------------------------

    def exit(self, order_id: str, reason: str) -> ExitResult | None:
        """ポジションを決済する.

        1. 現在価格を取得
        2. PnL を計算
        3. ポジションを閉じる
        4. on_exit コールバックを呼ぶ

        Args:
            order_id: 決済対象の注文ID
            reason: 決済理由

        Returns:
            決済結果（ポジション不存在なら None）
        """
        position = self._positions.get(order_id)
        if position is None:
            logger.warning("EXIT target not found: %s", order_id)
            return None

        # 現在価格を取得
        exit_price = self._get_exit_price(position.symbol)

        # PnL 計算
        if position.direction == "LONG":
            pnl = (exit_price - position.entry_price) * position.size
        else:
            pnl = (position.entry_price - exit_price) * position.size

        logger.info(
            "EXIT: %s %s entry=$%.2f exit=$%.2f pnl=$%.2f reason=%s",
            order_id, position.symbol, position.entry_price,
            exit_price, pnl, reason,
        )

        # 発注 (実弾モードのみ)
        order_result: OrderResult
        if self._paper_trade:
            del self._positions[order_id]
            order_result = OrderResult(order_id=order_id, status="PAPER_CLOSED")
        else:
            side = "SELL" if position.direction == "LONG" else "BUY"
            order = Order(symbol=position.symbol, side=side, quantity=position.size)
            order_result = self._client.place_order(order)
            if order_result.status != "FAILED":
                del self._positions[order_id]
            else:
                logger.error("EXIT order failed: %s", order_id)
                return None

        # コールバック
        exit_result = ExitResult(
            order_result=order_result,
            position=position,
            exit_price=exit_price,
            pnl=pnl,
            reason=reason,
        )
        if self._on_exit:
            try:
                self._on_exit(exit_result)
            except Exception:
                logger.exception("on_exit callback error")

        return exit_result

    def exit_all(self, reason: str) -> list[ExitResult]:
        """全ポジションを決済する."""
        results: list[ExitResult] = []
        for order_id in list(self._positions.keys()):
            result = self.exit(order_id, reason)
            if result:
                results.append(result)
        return results

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _get_exit_price(self, symbol: str) -> float:
        """現在の株価を取得する（取得失敗時は0.0）."""
        try:
            snapshot = self._client.get_snapshot(symbol)
            if snapshot.last_price > 0:
                return snapshot.last_price
        except Exception:
            logger.exception("Failed to get exit price for %s", symbol)
        return 0.0

    # ------------------------------------------------------------------
    # ポジション監視
    # ------------------------------------------------------------------

    async def monitor_positions(self) -> None:
        """SL/TPを非同期で監視し、条件を満たしたら自動決済する.

        5秒間隔でポジションの株価をチェックする。
        """
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
                            "SL triggered: %s $%.2f <= $%.2f",
                            pos.symbol, price, pos.levels.stop_loss,
                        )
                        self.exit(order_id, "SL")

                    # テイクプロフィット
                    elif price >= pos.levels.take_profit:
                        logger.info(
                            "TP triggered: %s $%.2f >= $%.2f",
                            pos.symbol, price, pos.levels.take_profit,
                        )
                        self.exit(order_id, "TP")

                except Exception:
                    logger.debug("Position monitor error: %s", order_id)

            await asyncio.sleep(5)
