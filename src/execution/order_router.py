"""発注・決済管理モジュール.

SIMULATE / REAL 両対応。
約定確認は position_list_query() ベースで行う（order_status は不正確なため）。
SL/TP監視は内部 dict の価格比較で判定する。

注意: futu SDK はスレッドセーフでないため asyncio.to_thread() は使わない。
同期 API 呼び出しは直接行い、待機のみ asyncio.sleep() を使う。
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
    mfe: float = 0.0  # Maximum Favorable Excursion（最大含み益・ドル）
    mae: float = 0.0  # Maximum Adverse Excursion（最大含み損・ドル）
    # 直近観測価格 (monitor_positions で更新)。強制決済時に snapshot が
    # 一時的に 0 を返した場合のフォールバック値として使う。
    last_known_price: float = 0.0


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
        # 同銘柄の enter() 多重実行を防ぐためのロックセット
        # 発注処理進行中 (SUBMITTED〜約定確認完了まで) は symbol を保持し、
        # その間の同銘柄 enter() は即 skip する
        self._in_flight_symbols: set[str] = set()
        logger.info(
            "OrderRouter initialized (env=%s, long_max=%d, short_max=%d)",
            settings.TRADE_ENV, settings.LONG_MAX_POSITIONS, settings.SHORT_MAX_POSITIONS,
        )

    def recover_positions(self) -> int:
        """moomoo の既存ポジションを内部 dict に復元する.

        position_id を使って一意に識別し、再起動時の重複復元を防ぐ。
        DRYRUN モードでは復元しない (仮想ポジションのみで運用)。
        """
        if not settings.ENABLE_REAL_TRADING:
            logger.info("DRYRUN モード: ポジション復元をスキップ (仮想ポジションのみ)")
            return 0
        positions = self._client.get_positions()
        count = 0
        for symbol, info in positions.items():
            qty = int(info["qty"])
            if qty <= 0:
                continue
            # position_id があればそれを使い、なければ symbol ベース
            pos_id = info.get("position_id", "")
            order_id = f"POS-{pos_id}" if pos_id else f"RECOVERED-{symbol}"
            if order_id in self._positions:
                continue
            cost_price = info["cost_price"]
            # get_positions() が direction ("LONG"/"SHORT") を返す。 SHORT 建玉も復元する
            direction = info.get("direction", "LONG")
            self._positions[order_id] = Position(
                order_id=order_id,
                symbol=symbol,
                direction=direction,
                size=qty,
                entry_price=cost_price,
                levels=None,
            )
            logger.info(
                "[RECOVERED] %s %s %d shares @ $%.2f (id=%s)",
                symbol, direction, qty, cost_price, order_id,
            )
            count += 1
        if count == 0:
            logger.info("No existing positions to recover")
        else:
            logger.info("Recovered %d positions from moomoo", count)
        return count

    @property
    def open_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    @property
    def position_count(self) -> int:
        return len(self._positions)

    @property
    def long_count(self) -> int:
        return sum(1 for p in self._positions.values() if p.direction == "LONG")

    @property
    def short_count(self) -> int:
        return sum(1 for p in self._positions.values() if p.direction == "SHORT")

    # ------------------------------------------------------------------
    # Entry (async — asyncio.sleep で待機、イベントループをブロックしない)
    # ------------------------------------------------------------------

    async def enter(
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

        # 同銘柄の発注処理進行中なら即 skip (race condition 対策)
        # moomoo の position_list_query は約定後しばらく qty=0 のラグがあり、
        # has_position() だけでは未約定発注を捕捉できないため
        if symbol in self._in_flight_symbols:
            logger.warning(
                "[%s] 発注処理進行中 — duplicate entry blocked (in-flight)",
                symbol,
            )
            return None

        if signal.direction == "LONG":
            if self.long_count >= settings.LONG_MAX_POSITIONS:
                logger.info(
                    "[%s] LONG_MAX_POSITIONS(%d)に達しているためスキップ",
                    symbol, settings.LONG_MAX_POSITIONS,
                )
                return None
        else:
            if self.short_count >= settings.SHORT_MAX_POSITIONS:
                logger.info(
                    "[%s] SHORT_MAX_POSITIONS(%d)に達しているためスキップ",
                    symbol, settings.SHORT_MAX_POSITIONS,
                )
                return None

        for pos in self._positions.values():
            if pos.symbol == symbol:
                logger.info("[%s] Duplicate entry blocked (internal dict)", symbol)
                return None

        self._in_flight_symbols.add(symbol)
        try:
            return await self._enter_inner(signal, symbol, size, price, levels)
        finally:
            self._in_flight_symbols.discard(symbol)

    async def _enter_inner(
        self,
        signal: EntryDecision,
        symbol: str,
        size: int,
        price: float,
        levels: Levels | None,
    ) -> OrderResult | None:
        """enter() の内部処理 — in-flight ロック内で実行."""

        # --- DRYRUN モード: 実発注せず仮想ポジション登録 ---
        if not settings.ENABLE_REAL_TRADING:
            order_id = f"DRYRUN-{symbol}-{int(time.time() * 1000)}"
            self._positions[order_id] = Position(
                order_id=order_id, symbol=symbol,
                direction=signal.direction, size=size,
                entry_price=price, levels=levels,
            )
            logger.info(
                "[%s] DRYRUN ENTRY: %s %d shares @ $%.2f id=%s (SL=$%.2f TP=$%.2f)",
                symbol, signal.direction, size, price, order_id,
                levels.stop_loss if levels else 0,
                levels.take_profit if levels else 0,
            )
            return OrderResult(
                order_id=order_id, status="FILLED",
                filled_price=price, filled_quantity=size,
            )

        logger.debug("[%s] has_position() チェック開始", symbol)
        t0 = time.monotonic()
        try:
            if self._client.has_position(symbol):
                logger.info("[%s] Duplicate entry blocked (moomoo position_list)", symbol)
                return None
        except Exception:
            logger.exception("[%s] has_position() で例外", symbol)
        logger.debug("[%s] has_position() チェック完了 (%.2fs)", symbol, time.monotonic() - t0)

        # エントリー時の side: LONG→BUY (現物/信用買い)、 SHORT→SELL_SHORT (信用空売り、 JP_TOKUTEI_SHORT)
        side = "BUY" if signal.direction == "LONG" else "SELL_SHORT"

        # SHORT エントリー前に max_sell_short qty をチェック (借株不足で発注失敗を未然回避)
        if signal.direction == "SHORT":
            try:
                max_short = self._client.get_max_sell_short_qty(symbol)
                if max_short < size:
                    logger.warning(
                        "[%s] SHORT 発注スキップ: max_sell_short=%d < 要求 qty=%d (借株不足)",
                        symbol, max_short, size,
                    )
                    return OrderResult(order_id="", status="FAILED")
                logger.info("[%s] SHORT 借株 OK: max_sell_short=%d >= qty=%d", symbol, max_short, size)
            except Exception:
                # max_sell_short 取得失敗時は発注は試行する (moomoo 側で最終判定)
                logger.exception("[%s] max_sell_short 取得失敗 — 発注は試行", symbol)

        # 保護指値: 買い系 (BUY/BUY_BACK) は last × (1 + pct)、 売り系 (SELL_SHORT) は × (1 - pct)
        # pct=0 で従来の成行、 pct=0.02 で +2%/-2% 指値 (実質成行 + 上限/下限保護)
        protective_pct = settings.ORDER_PROTECTIVE_LIMIT_PCT
        is_buy_side = side in ("BUY", "BUY_BACK")
        if protective_pct > 0 and price > 0:
            limit_price = (
                round(price * (1 + protective_pct), 2)
                if is_buy_side
                else round(price * (1 - protective_pct), 2)
            )
            logger.info(
                "[%s] place_order() 呼び出し: side=%s qty=%d limit=$%.2f (last=$%.2f, %s%.0f%% 保護)",
                symbol, side, size, limit_price, price,
                "+" if is_buy_side else "-", protective_pct * 100,
            )
            order_obj = Order(symbol=symbol, side=side, quantity=size, price=limit_price)
        else:
            logger.info(
                "[%s] place_order() 呼び出し: side=%s qty=%d 成行 (last=$%.2f)",
                symbol, side, size, price,
            )
            order_obj = Order(symbol=symbol, side=side, quantity=size)

        t0 = time.monotonic()
        result = self._client.place_order(order_obj)
        elapsed = time.monotonic() - t0
        logger.info(
            "[%s] place_order() 応答: order_id=%s status=%s (%.2fs)",
            symbol, result.order_id, result.status, elapsed,
        )

        if result.status == "FAILED":
            logger.error("[%s] ENTRY failed: %s", symbol, result)
            return result

        # 約定確認: 全量一致を確認（asyncio.sleep で待機）
        logger.info("[%s] 約定確認開始 (最大%.0fs, order_id=%s, qty=%d)", symbol, FILL_CHECK_MAX_WAIT, result.order_id, size)
        filled = await self._wait_for_fill(symbol, size, order_id=result.order_id)

        if filled is None:
            # タイムアウト時、 まず最終 order_status を確認する。
            # moomoo の position_list_query は約定後でも数秒〜数十秒 qty=0 のラグがあるため、
            # FILLED_ALL/FILLED_PART でも _wait_for_fill がタイムアウトすることがある。
            # その場合に cancel_order を呼ぶと「約定済みのため無効」となるが、
            # bot 内部は「キャンセル扱い」 で _positions に登録されないので、
            # 次のスキャンで同銘柄に再発注され重複建てが発生する。
            # 約定済みなら必ず _positions に登録してスロットを消費させる。
            final_status = "?"
            try:
                final_status = self._client.get_order_status(result.order_id)
            except Exception:
                logger.exception("[%s] 最終ステータス取得失敗", symbol)
            if "FILLED" in final_status:
                logger.warning(
                    "[%s] 約定確認タイムアウトしたが order_status=%s — position_list ラグと判断しポジション登録 (order_id=%s)",
                    symbol, final_status, result.order_id,
                )
                order_id = result.order_id
                self._positions[order_id] = Position(
                    order_id=order_id, symbol=symbol,
                    direction=signal.direction, size=size,
                    entry_price=price, levels=levels,
                )
                logger.info(
                    "ENTRY COMPLETE (lag): %s %s %d shares @ $%.2f id=%s (SL=$%.2f TP=$%.2f)",
                    signal.direction, symbol, size, price, order_id,
                    levels.stop_loss if levels else 0,
                    levels.take_profit if levels else 0,
                )
                return OrderResult(
                    order_id=order_id, status="FILLED",
                    filled_price=price, filled_quantity=size,
                )
            # 真に未約定 → キャンセル
            logger.warning(
                "[%s] 約定確認タイムアウト (%.0fs) order_status=%s — 注文キャンセル order_id=%s",
                symbol, FILL_CHECK_MAX_WAIT, final_status, result.order_id,
            )
            self._client.cancel_order(result.order_id)
            return OrderResult(order_id=result.order_id, status="CANCELLED")

        fill_price = filled.get("cost_price", price)
        fill_qty = int(filled.get("qty", size))
        logger.info("[%s] 約定確認OK: qty=%d/%d cost_price=$%.2f", symbol, fill_qty, size, fill_price)

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

    async def _wait_for_fill(self, symbol: str, expected_qty: int, order_id: str = "") -> dict | None:
        """約定確認: 全量約定を確認する (asyncio.sleep で待機)."""
        elapsed = 0.0
        attempt = 0
        while elapsed < FILL_CHECK_MAX_WAIT:
            await asyncio.sleep(FILL_CHECK_INTERVAL)
            elapsed += FILL_CHECK_INTERVAL
            attempt += 1
            t0 = time.monotonic()
            positions = self._client.get_positions()
            api_time = time.monotonic() - t0
            order_status = "?"
            try:
                order_status = self._client.get_order_status(order_id) if order_id else "?"
            except Exception:
                pass
            pos_qty = int(positions.get(symbol, {}).get("qty", 0))
            logger.info(
                "[%s] fill check #%d (%.1fs): order_status=%s pos_qty=%d/%d positions=%s (api %.2fs)",
                symbol, attempt, elapsed, order_status, pos_qty, expected_qty,
                list(positions.keys()), api_time,
            )
            # 全量約定を確認
            if symbol in positions and pos_qty >= expected_qty:
                return positions[symbol]
        return None

    # ------------------------------------------------------------------
    # Exit (async — 待機のみ asyncio.sleep、API は同期で直接呼ぶ)
    # ------------------------------------------------------------------

    async def exit(self, order_id: str, reason: str) -> ExitResult | None:
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
        # 全フォールバック失敗時は entry_price で代用 (PnL=0 として記録)
        if exit_price <= 0:
            logger.error(
                "[%s] EXIT 価格取得 全失敗 → entry_price=$%.2f で代用 (PnL=0 記録)",
                position.symbol, position.entry_price,
            )
            exit_price = position.entry_price
        logger.info("[%s] 現在価格: $%.2f", position.symbol, exit_price)

        # --- DRYRUN モード: 実決済せず仮想クローズ ---
        if not settings.ENABLE_REAL_TRADING:
            if position.direction == "LONG":
                pnl = (exit_price - position.entry_price) * position.size
            else:
                pnl = (position.entry_price - exit_price) * position.size
            logger.info(
                "[%s] DRYRUN EXIT: entry=$%.2f exit=$%.2f pnl=$%.2f reason=%s",
                position.symbol, position.entry_price, exit_price, pnl, reason,
            )
            del self._positions[order_id]
            exit_result = ExitResult(
                order_result=OrderResult(order_id=order_id, status="FILLED",
                                         filled_price=exit_price, filled_quantity=position.size),
                position=position,
                exit_price=exit_price, pnl=pnl, reason=reason,
            )
            if self._on_exit:
                try:
                    self._on_exit(exit_result)
                except Exception:
                    logger.exception("on_exit callback error")
            return exit_result

        # クローズ時の side: LONG ポジションは SELL (現物売却)、 SHORT ポジションは BUY_BACK (信用買い戻し)
        side = "SELL" if position.direction == "LONG" else "BUY_BACK"
        logger.info(
            "[%s] EXIT place_order() 呼び出し: side=%s direction=%s qty=%d",
            position.symbol, side, position.direction, position.size,
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
            logger.error(
                "[%s] EXIT order failed: id=%s direction=%s side=%s",
                position.symbol, order_id, position.direction, side,
            )
            return None

        # 決済確認: asyncio.sleep で待機（イベントループをブロックしない）
        logger.info("[%s] 決済確認開始 (最大%.0fs)", position.symbol, FILL_CHECK_MAX_WAIT)
        closed = await self._wait_for_close_async(position.symbol)

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

    async def _wait_for_close_async(self, symbol: str) -> bool:
        """決済確認 (asyncio.sleep で待機、API は同期で直接呼ぶ)."""
        elapsed = 0.0
        attempt = 0
        while elapsed < FILL_CHECK_MAX_WAIT:
            await asyncio.sleep(FILL_CHECK_INTERVAL)
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

    async def exit_all(self, reason: str) -> list[ExitResult]:
        """全ポジション決済 (async版)."""
        logger.info("exit_all() 開始: reason=%s positions=%d", reason, self.position_count)
        results: list[ExitResult] = []
        for order_id in list(self._positions.keys()):
            result = await self.exit(order_id, reason)
            if result:
                results.append(result)
        logger.info("exit_all() 完了: %d 決済成功", len(results))
        return results

    def exit_all_sync(self, reason: str) -> list[ExitResult]:
        """全ポジション決済 (同期版 — 強制決済用).

        asyncio.sleep() を使わず time.sleep() で待機するため、
        イベントループの状態に関係なく確実に全注文を送信する。
        monitor_positions() を停止してから呼ぶこと。
        """
        logger.info("exit_all_sync() 開始: reason=%s positions=%d", reason, self.position_count)
        results: list[ExitResult] = []
        for order_id in list(self._positions.keys()):
            result = self._exit_sync(order_id, reason)
            if result:
                results.append(result)
        logger.info("exit_all_sync() 完了: %d 決済成功", len(results))
        return results

    def _exit_sync(self, order_id: str, reason: str) -> ExitResult | None:
        """ポジション決済 (同期版 — 強制決済用)."""
        position = self._positions.get(order_id)
        if position is None:
            logger.warning("EXIT target not found: %s", order_id)
            return None

        logger.info(
            "[%s] EXIT_SYNC 開始: id=%s reason=%s size=%d entry=$%.2f",
            position.symbol, order_id, reason, position.size, position.entry_price,
        )

        exit_price = self._get_exit_price(position.symbol)
        # 全フォールバック失敗時は entry_price で代用 (PnL=0 として記録)
        if exit_price <= 0:
            logger.error(
                "[%s] EXIT_SYNC 価格取得 全失敗 → entry_price=$%.2f で代用 (PnL=0 記録)",
                position.symbol, position.entry_price,
            )
            exit_price = position.entry_price
        logger.info("[%s] 現在価格: $%.2f", position.symbol, exit_price)

        # --- DRYRUN モード: 実決済せず仮想クローズ ---
        if not settings.ENABLE_REAL_TRADING:
            if position.direction == "LONG":
                pnl = (exit_price - position.entry_price) * position.size
            else:
                pnl = (position.entry_price - exit_price) * position.size
            logger.info(
                "[%s] DRYRUN EXIT_SYNC: entry=$%.2f exit=$%.2f pnl=$%.2f reason=%s",
                position.symbol, position.entry_price, exit_price, pnl, reason,
            )
            del self._positions[order_id]
            exit_result = ExitResult(
                order_result=OrderResult(order_id=order_id, status="FILLED",
                                         filled_price=exit_price, filled_quantity=position.size),
                position=position,
                exit_price=exit_price, pnl=pnl, reason=reason,
            )
            if self._on_exit:
                try:
                    self._on_exit(exit_result)
                except Exception:
                    logger.exception("on_exit callback error")
            return exit_result

        # 同期版 EXIT (強制決済) — LONG→SELL、 SHORT→BUY_BACK
        side = "SELL" if position.direction == "LONG" else "BUY_BACK"
        logger.info(
            "[%s] EXIT_SYNC place_order() 呼び出し: side=%s direction=%s qty=%d",
            position.symbol, side, position.direction, position.size,
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
            logger.error(
                "[%s] EXIT_SYNC order failed: id=%s direction=%s side=%s",
                position.symbol, order_id, position.direction, side,
            )
            return None

        # 同期で決済確認
        closed = False
        wait_elapsed = 0.0
        while wait_elapsed < FILL_CHECK_MAX_WAIT:
            time.sleep(FILL_CHECK_INTERVAL)
            wait_elapsed += FILL_CHECK_INTERVAL
            if not self._client.has_position(position.symbol):
                logger.info("[%s] 決済確認OK (%.1fs)", position.symbol, wait_elapsed)
                closed = True
                break
        if not closed:
            logger.warning("[%s] 決済確認タイムアウト — 内部管理で続行", position.symbol)

        if position.direction == "LONG":
            pnl = (exit_price - position.entry_price) * position.size
        else:
            pnl = (position.entry_price - exit_price) * position.size

        logger.info(
            "EXIT_SYNC COMPLETE: %s %s entry=$%.2f exit=$%.2f pnl=$%.2f reason=%s",
            order_id, position.symbol, position.entry_price, exit_price, pnl, reason,
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

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_exit_price(self, symbol: str) -> float:
        """決済価格を snapshot から取得する.

        市場クローズ直後など snapshot が一時的に 0 を返すことがあるため、
        最大 2 回リトライする。それでも 0 ならポジションの last_known_price
        を返す (monitor_positions で逐次更新されている)。
        最終フォールバックも 0 なら 0 を返し、呼び出し側で処理する。
        """
        for attempt in range(2):
            try:
                snap = self._client.get_snapshot(symbol)
                if snap.last_price > 0:
                    return snap.last_price
                logger.warning(
                    "[%s] 決済価格 snapshot last_price=0 (試行%d/2)",
                    symbol, attempt + 1,
                )
            except Exception:
                logger.exception("[%s] Failed to get exit price (試行%d/2)", symbol, attempt + 1)
            if attempt == 0:
                time.sleep(0.3)

        # snapshot が 2 回連続で失敗 → monitor_positions が保持している
        # 直近観測価格をフォールバックとして使う
        for pos in self._positions.values():
            if pos.symbol == symbol and pos.last_known_price > 0:
                logger.warning(
                    "[%s] snapshot 取得失敗 → last_known_price=$%.2f を使用",
                    symbol, pos.last_known_price,
                )
                return pos.last_known_price
        return 0.0

    # ------------------------------------------------------------------
    # Monitor
    # ------------------------------------------------------------------

    async def monitor_positions(self) -> None:
        """SL/TP監視ループ（5秒間隔）.

        API は同期で直接呼ぶ (futu SDK はスレッドセーフでないため)。
        ループの合間に await asyncio.sleep() でイベントループに制御を返す。
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

                    # 直近観測価格を更新 (強制決済時のフォールバック用)
                    pos.last_known_price = price

                    # MFE/MAE 更新
                    if pos.direction == "LONG":
                        unrealized = (price - pos.entry_price) * pos.size
                    else:
                        unrealized = (pos.entry_price - price) * pos.size
                    if unrealized > 0:
                        pos.mfe = max(pos.mfe, unrealized)
                    else:
                        pos.mae = max(pos.mae, abs(unrealized))

                    # SL/TP の距離計算（LONG: SL<price<TP, SHORT: TP<price<SL）
                    if pos.direction == "SHORT":
                        sl_dist = (pos.levels.stop_loss - price) / pos.entry_price * 100
                        tp_dist = (price - pos.levels.take_profit) / pos.entry_price * 100
                    else:
                        sl_dist = (price - pos.levels.stop_loss) / pos.entry_price * 100
                        tp_dist = (pos.levels.take_profit - price) / pos.entry_price * 100

                    if loop_count % 100 == 1 or sl_dist < 0.5 or tp_dist < 0.5:
                        logger.info(
                            "[%s] monitor(%s): price=$%.2f pnl=$%+.2f mfe=$%.2f mae=$%.2f "
                            "SL=$%.2f(%.1f%%) TP=$%.2f(%.1f%%)",
                            pos.symbol, pos.direction, price,
                            unrealized, pos.mfe, pos.mae,
                            pos.levels.stop_loss, sl_dist,
                            pos.levels.take_profit, tp_dist,
                        )

                    # LONG: price <= SL → SL, price >= TP → TP
                    # SHORT: price >= SL → SL, price <= TP → TP
                    sl_hit = (
                        price >= pos.levels.stop_loss if pos.direction == "SHORT"
                        else price <= pos.levels.stop_loss
                    )
                    tp_hit = (
                        price <= pos.levels.take_profit if pos.direction == "SHORT"
                        else price >= pos.levels.take_profit
                    )

                    if sl_hit:
                        logger.warning(
                            "SL HIT: %s %s price=$%.2f SL=$%.2f (entry=$%.2f)",
                            pos.symbol, pos.direction, price, pos.levels.stop_loss, pos.entry_price,
                        )
                        await self.exit(order_id, "SL")
                    elif tp_hit:
                        logger.info(
                            "TP HIT: %s %s price=$%.2f TP=$%.2f (entry=$%.2f)",
                            pos.symbol, pos.direction, price, pos.levels.take_profit, pos.entry_price,
                        )
                        await self.exit(order_id, "TP")
                except Exception:
                    logger.exception("[%s] monitor_positions エラー", pos.symbol)

                # 各銘柄の処理後にイベントループに制御を返す
                await asyncio.sleep(0)

            await asyncio.sleep(5)
