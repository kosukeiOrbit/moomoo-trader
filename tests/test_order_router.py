"""OrderRouter のユニットテスト.

SIMULATE/REAL 両方で moomoo API (place_order) を呼び出す。
テストではモックで place_order の戻り値を制御する。
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

if "futu" not in sys.modules:
    sys.modules["futu"] = MagicMock()

from src.data.moomoo_client import Order, OrderResult, QuoteSnapshot
from src.execution.order_router import OrderRouter, Position, ExitResult
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.stop_loss import Levels
from src.signals.and_filter import EntryDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _go_long() -> EntryDecision:
    return EntryDecision(go=True, direction="LONG", sentiment_score=0.5, flow_strength=0.8)


def _no_go() -> EntryDecision:
    return EntryDecision(go=False, reason="test")


def _levels() -> Levels:
    return Levels(stop_loss=145.0, take_profit=160.0, trailing_stop=147.0)


def _make_router(
    on_exit: object = None,
    snapshot_price: float = 155.0,
) -> tuple[OrderRouter, MagicMock]:
    mock_client = MagicMock()
    mock_client.get_snapshot.return_value = QuoteSnapshot(
        symbol="AAPL", last_price=snapshot_price, volume=0, turnover=0,
    )
    _seq = iter(range(1, 100))
    mock_client.place_order.side_effect = lambda order: OrderResult(
        order_id=f"ORD-{next(_seq)}", status="SUBMITTED",
    )
    cb = CircuitBreaker()
    router = OrderRouter(mock_client, cb, on_exit=on_exit)
    return router, mock_client


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

class TestOrderRouterEntry:

    def test_enter_calls_place_order(self) -> None:
        """enter() が moomoo API を呼ぶ."""
        router, mock_client = _make_router()
        result = router.enter(_go_long(), "AAPL", 10, 150.0, _levels())
        assert result is not None
        assert result.status == "SUBMITTED"
        assert router.position_count == 1
        mock_client.place_order.assert_called_once()

    def test_enter_failure(self) -> None:
        """place_order 失敗時はポジション記録なし."""
        router, mock_client = _make_router()
        mock_client.place_order.side_effect = None
        mock_client.place_order.return_value = OrderResult(order_id="", status="FAILED")
        result = router.enter(_go_long(), "AAPL", 10, 150.0)
        assert result.status == "FAILED"
        assert router.position_count == 0

    def test_duplicate_entry_blocked(self) -> None:
        router, _ = _make_router()
        r1 = router.enter(_go_long(), "AAPL", 10, 150.0)
        r2 = router.enter(_go_long(), "AAPL", 5, 151.0)
        assert r1 is not None
        assert r2 is None
        assert router.position_count == 1

    def test_different_symbols_allowed(self) -> None:
        router, _ = _make_router()
        router.enter(_go_long(), "AAPL", 10, 150.0)
        router.enter(_go_long(), "NVDA", 5, 170.0)
        assert router.position_count == 2

    def test_reentry_after_exit(self) -> None:
        router, _ = _make_router()
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        router.exit(oid, "TP")
        r2 = router.enter(_go_long(), "AAPL", 5, 155.0)
        assert r2 is not None
        assert router.position_count == 1

    def test_no_go_returns_none(self) -> None:
        router, _ = _make_router()
        assert router.enter(_no_go(), "AAPL", 10, 150.0) is None

    def test_zero_size_returns_none(self) -> None:
        router, _ = _make_router()
        assert router.enter(_go_long(), "AAPL", 0, 150.0) is None

    def test_order_ids_from_api(self) -> None:
        """order_id は moomoo API の戻り値を使う."""
        router, _ = _make_router()
        r1 = router.enter(_go_long(), "AAPL", 5, 150.0)
        r2 = router.enter(_go_long(), "NVDA", 5, 500.0)
        assert r1.order_id.startswith("ORD-")
        assert r1.order_id != r2.order_id


# ---------------------------------------------------------------------------
# Exit
# ---------------------------------------------------------------------------

class TestOrderRouterExit:

    def test_exit_calls_place_order(self) -> None:
        """exit() が反対売買を API に送る."""
        router, mock_client = _make_router()
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        result = router.exit(oid, "TP")
        assert isinstance(result, ExitResult)
        assert result.exit_price == 155.0
        assert result.pnl == 50.0  # (155-150)*10
        assert router.position_count == 0
        assert mock_client.place_order.call_count == 2  # entry + exit

    def test_exit_unknown_returns_none(self) -> None:
        router, _ = _make_router()
        assert router.exit("UNKNOWN", "test") is None

    def test_exit_failure_keeps_position(self) -> None:
        """exit の place_order 失敗時はポジション保持."""
        router, mock_client = _make_router()
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        # exit の place_order を失敗させる
        mock_client.place_order.side_effect = None
        mock_client.place_order.return_value = OrderResult(order_id="", status="FAILED")
        result = router.exit(oid, "SL")
        assert result is None
        assert router.position_count == 1  # still open


# ---------------------------------------------------------------------------
# exit_all
# ---------------------------------------------------------------------------

class TestOrderRouterExitAll:

    def test_exit_all_closes_all(self) -> None:
        router, _ = _make_router()
        router.enter(_go_long(), "AAPL", 10, 150.0)
        router.enter(_go_long(), "NVDA", 5, 500.0)
        results = router.exit_all("force close")
        assert len(results) == 2
        assert router.position_count == 0

    def test_exit_all_empty(self) -> None:
        router, _ = _make_router()
        assert router.exit_all("test") == []


# ---------------------------------------------------------------------------
# on_exit callback
# ---------------------------------------------------------------------------

class TestOrderRouterOnExit:

    def test_on_exit_called(self) -> None:
        callback = MagicMock()
        router, _ = _make_router(on_exit=callback)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        router.exit(oid, "TP")
        callback.assert_called_once()
        result: ExitResult = callback.call_args[0][0]
        assert result.pnl == 50.0

    def test_on_exit_called_for_each_in_exit_all(self) -> None:
        callback = MagicMock()
        router, _ = _make_router(on_exit=callback)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        router.enter(_go_long(), "NVDA", 5, 500.0)
        router.exit_all("force close")
        assert callback.call_count == 2

    def test_on_exit_loss(self) -> None:
        callback = MagicMock()
        router, _ = _make_router(on_exit=callback, snapshot_price=140.0)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        router.exit(oid, "SL")
        result: ExitResult = callback.call_args[0][0]
        assert result.pnl == -100.0

    def test_on_exit_exception_safe(self) -> None:
        callback = MagicMock(side_effect=RuntimeError("boom"))
        router, _ = _make_router(on_exit=callback)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        result = router.exit(oid, "TP")
        assert result is not None  # doesn't crash

    def test_no_callback_ok(self) -> None:
        router, _ = _make_router(on_exit=None)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        result = router.exit(oid, "TP")
        assert result is not None


# ---------------------------------------------------------------------------
# PnL
# ---------------------------------------------------------------------------

class TestOrderRouterPnL:

    def test_long_profit(self) -> None:
        router, _ = _make_router(snapshot_price=160.0)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        result = router.exit(oid, "TP")
        assert result.pnl == 100.0

    def test_long_loss(self) -> None:
        router, _ = _make_router(snapshot_price=145.0)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        result = router.exit(oid, "SL")
        assert result.pnl == -50.0


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class TestOrderRouterMonitor:

    @pytest.mark.asyncio
    async def test_monitor_triggers_sl(self) -> None:
        callback = MagicMock()
        router, _ = _make_router(on_exit=callback, snapshot_price=144.0)
        router.enter(_go_long(), "AAPL", 10, 150.0, _levels())

        import asyncio
        task = asyncio.create_task(router.monitor_positions())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert router.position_count == 0
        assert callback.call_args[0][0].reason == "SL"

    @pytest.mark.asyncio
    async def test_monitor_triggers_tp(self) -> None:
        callback = MagicMock()
        router, _ = _make_router(on_exit=callback, snapshot_price=161.0)
        router.enter(_go_long(), "AAPL", 10, 150.0, _levels())

        import asyncio
        task = asyncio.create_task(router.monitor_positions())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert router.position_count == 0
        assert callback.call_args[0][0].reason == "TP"

    @pytest.mark.asyncio
    async def test_monitor_no_exit_in_range(self) -> None:
        callback = MagicMock()
        router, _ = _make_router(on_exit=callback, snapshot_price=152.0)
        router.enter(_go_long(), "AAPL", 10, 150.0, _levels())

        import asyncio
        task = asyncio.create_task(router.monitor_positions())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert router.position_count == 1
        callback.assert_not_called()
