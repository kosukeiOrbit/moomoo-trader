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
        logger.info(
            "OrderRouter initialized (env=%s, max_positions=%d)",
            settings.TRADE_ENV, settings.MAX_POSITIONS,
        )

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
                logger.info("[%s] Duplicate entry blocked (internal dict)", symbol)
                return None

        # moomoo API にも重複チェック
        logger.debug("[%s] has_position() チェック開始", symbol)
        t0 = time.monotonic()
        try:
            if self._client.has_position(symbol):
                logger.info("[%s] Duplicate entry blocked (moomoo position_list)", symbol)
                return None
        except Exception:
            logger.exception("[%s] has_position() で例外", symbol)
        logger.debug("[%s] has_position() チェック完了 (%.2fs)", symbol, time.monotonic() - t0)

        # 発注
        logger.info(
            "[%s] place_order() 呼び出し: side=%s qty=%d price=$%.2f",
            symbol, "BUY" if signal.direction == "LONG" else "SELL", size, price,
        )
        t0 = time.monotonic()
        side = "BUY" if signal.direction == "LONG" else "SELL"
        result = self._client.place_order(Order(symbol=symbol, side=side, quantity=size))
        elapsed = time.monotonic() - t0
        logger.info(
            "[%s] place_order() 応答: order_id=%s status=%s (%.2fs)",
            symbol, result.order_id, result.status, elapsed,
        )

        if result.status == "FAILED":
            logger.error("[%s] ENTRY failed: %s", symbol, result)
            return result

        # position_list_query() で約定確認（最大5秒待機）
        logger.info("[%s] 約定確認開始 (最大%.0fs, order_id=%s)", symbol, FILL_CHECK_MAX_WAIT, result.order_id)
        filled = self._wait_for_fill(symbol, order_id=result.order_id)
        if filled:
            fill_price = filled.get("cost_price", price)
            fill_qty = int(filled.get("qty", size))
            logger.info(
                "[%s] 約定確認OK: qty=%d cost_price=$%.2f",
                symbol, fill_qty, fill_price,
            )
        else:
            # position_list に出なくても内部管理はする（ラグ対応）
            logger.warning(
                "[%s] 約定確認タイムアウト (%.0fs) — 内部管理で続行 price=$%.2f qty=%d",
                symbol, FILL_CHECK_MAX_WAIT, price, size,
            )
            fill_price = price
            fill_qty = size

        order_id = result.order_id
        self._positions[order_id] = Position(
            order_id=order_id, symbol=symbol,
            direction=signal.direction, size=fill_qty,
            entry_price=fill_price, levels=levels,
        )
        logger.info(
            "ENTRY COMPLETE: %s %s %d shares @ $%.2f id=%s (SL=$%.2f TP=$%.2f)",
            signal.direction, symbol, fill_qty, fill_price, order_id,
            levels.stop_loss if levels else 0,
            levels.take_profit if levels else 0,
        )
        return OrderResult(
            order_id=order_id, status="FILLED",
            filled_price=fill_price, filled_quantity=fill_qty,
        )

    def _wait_for_fill(self, symbol: str, order_id: str = "") -> dict | None:
        """position_list_query() で約定を確認する（最大 FILL_CHECK_MAX_WAIT 秒）.

        同時に order_list_query() で order_status の変化もログに記録する。
        """
        elapsed = 0.0
        attempt = 0
        while elapsed < FILL_CHECK_MAX_WAIT:
            time.sleep(FILL_CHECK_INTERVAL)
            elapsed += FILL_CHECK_INTERVAL
            attempt += 1

            # position_list で約定確認
            t0 = time.monotonic()
            positions = self._client.get_positions()
            api_time = time.monotonic() - t0

            # order_list で order_status も取得
            order_status = "?"
            try:
                order_status = self._client.get_order_status(order_id) if order_id else "?"
            except Exception:
                pass

            logger.info(
                "[%s] fill check #%d (%.1fs): order_status=%s positions=%s (api %.2fs)",
                symbol, attempt, elapsed, order_status,
                list(positions.keys()), api_time,
            )
            if symbol in positions and positions[symbol]["qty"] > 0:
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

        logger.info(
            "[%s] EXIT 開始: id=%s reason=%s direction=%s size=%d entry=$%.2f",
            position.symbol, order_id, reason,
            position.direction, position.size, position.entry_price,
        )

        exit_price = self._get_exit_price(position.symbol)
        logger.info("[%s] 現在価格: $%.2f", position.symbol, exit_price)

        # 決済注文を発注
        side = "SELL" if position.direction == "LONG" else "BUY"
        logger.info(
            "[%s] place_order() 呼び出し: side=%s qty=%d",
            position.symbol, side, position.size,
        )
        t0 = time.monotonic()
        result = self._client.place_order(
            Order(symbol=position.symbol, side=side, quantity=position.size),
        )
        elapsed = time.monotonic() - t0
        logger.info(
            "[%s] place_order() 応答: order_id=%s status=%s (%.2fs)",
            position.symbol, result.order_id, result.status, elapsed,
        )

        if result.status == "FAILED":
            logger.error("[%s] EXIT order failed: id=%s", position.symbol, order_id)
            return None

        # position_list_query() でポジション消滅を確認
        logger.info("[%s] 決済確認開始 (最大%.0fs)", position.symbol, FILL_CHECK_MAX_WAIT)
        closed = self._wait_for_close(position.symbol)

        if position.direction == "LONG":
            pnl = (exit_price - position.entry_price) * position.size
        else:
            pnl = (position.entry_price - exit_price) * position.size

        logger.info(
            "EXIT COMPLETE: %s %s entry=$%.2f exit=$%.2f pnl=$%.2f reason=%s closed=%s",
            order_id, position.symbol, position.entry_price,
            exit_price, pnl, reason, closed,
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
        attempt = 0
        while elapsed < FILL_CHECK_MAX_WAIT:
            time.sleep(FILL_CHECK_INTERVAL)
            elapsed += FILL_CHECK_INTERVAL
            attempt += 1
            t0 = time.monotonic()
            has = self._client.has_position(symbol)
            api_time = time.monotonic() - t0
            logger.debug(
                "[%s] close check #%d: has_position()=%s (%.2fs)",
                symbol, attempt, has, api_time,
            )
            if not has:
                logger.info("[%s] 決済確認OK (%.1fs)", symbol, elapsed)
                return True
        logger.warning("[%s] 決済確認タイムアウト — 内部管理で続行", symbol)
        return False

    def exit_all(self, reason: str) -> list[ExitResult]:
        """全ポジション決済."""
        logger.info("exit_all() 開始: reason=%s positions=%d", reason, self.position_count)
        results: list[ExitResult] = []
        for order_id in list(self._positions.keys()):
            result = self.exit(order_id, reason)
            if result:
                results.append(result)
        logger.info("exit_all() 完了: %d/%d 決済成功", len(results), len(results))
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
        logger.info("monitor_positions() ループ開始")
        loop_count = 0
        while True:
            loop_count += 1
            pos_count = len(self._positions)
            if pos_count > 0:
                logger.debug("monitor loop #%d: %d positions", loop_count, pos_count)

            for order_id, pos in list(self._positions.items()):
                if pos.levels is None:
                    continue
                try:
                    t0 = time.monotonic()
                    snap = self._client.get_snapshot(pos.symbol)
                    api_time = time.monotonic() - t0
                    price = snap.last_price
                    if price <= 0:
                        logger.debug("[%s] monitor: price=0 (%.2fs)", pos.symbol, api_time)
                        continue

                    # 100ループに1回、またはSL/TPに近い時に価格ログ
                    sl_dist = (price - pos.levels.stop_loss) / pos.entry_price * 100
                    tp_dist = (pos.levels.take_profit - price) / pos.entry_price * 100
                    if loop_count % 100 == 1 or sl_dist < 0.5 or tp_dist < 0.5:
                        logger.info(
                            "[%s] monitor: price=$%.2f SL=$%.2f(%.1f%%) TP=$%.2f(%.1f%%) (%.2fs)",
                            pos.symbol, price,
                            pos.levels.stop_loss, sl_dist,
                            pos.levels.take_profit, tp_dist,
                            api_time,
                        )

                    if price <= pos.levels.stop_loss:
                        logger.warning(
                            "SL HIT: %s price=$%.2f <= SL=$%.2f (entry=$%.2f)",
                            pos.symbol, price, pos.levels.stop_loss, pos.entry_price,
                        )
                        self.exit(order_id, "SL")
                    elif price >= pos.levels.take_profit:
                        logger.info(
                            "TP HIT: %s price=$%.2f >= TP=$%.2f (entry=$%.2f)",
                            pos.symbol, price, pos.levels.take_profit, pos.entry_price,
                        )
                        self.exit(order_id, "TP")
                except Exception:
                    logger.exception("[%s] monitor_positions エラー", pos.symbol)
            await asyncio.sleep(5)
