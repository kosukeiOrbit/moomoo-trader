"""OrderRouter tests: SIMULATE (local) and REAL (API) modes."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

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

def _make_router(on_exit=None, snapshot_price: float = 155.0):
    mock_client = MagicMock()
    mock_client.get_snapshot.return_value = QuoteSnapshot(
        symbol="AAPL", last_price=snapshot_price, volume=0, turnover=0,
    )
    _seq = iter(range(1, 100))
    mock_client.place_order.side_effect = lambda order: OrderResult(
        order_id=f"ORD-{next(_seq)}", status="SUBMITTED",
    )
    router = OrderRouter(mock_client, CircuitBreaker(), on_exit=on_exit)
    return router, mock_client


# ---------------------------------------------------------------------------
# SIMULATE mode
# ---------------------------------------------------------------------------

@patch("src.execution.order_router._is_simulate", True)
class TestSimulateEntry:

    def test_entry_does_not_call_api(self) -> None:
        router, mock_client = _make_router()
        result = router.enter(_go_long(), "AAPL", 10, 150.0, _levels())
        assert result.status == "FILLED"
        assert result.order_id.startswith("PAPER-")
        assert router.position_count == 1
        mock_client.place_order.assert_not_called()

    def test_duplicate_blocked(self) -> None:
        router, _ = _make_router()
        router.enter(_go_long(), "AAPL", 10, 150.0)
        assert router.enter(_go_long(), "AAPL", 5, 151.0) is None
        assert router.position_count == 1

    def test_different_symbols(self) -> None:
        router, _ = _make_router()
        router.enter(_go_long(), "AAPL", 10, 150.0)
        router.enter(_go_long(), "NVDA", 5, 170.0)
        assert router.position_count == 2

    def test_reentry_after_exit(self) -> None:
        router, _ = _make_router()
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        router.exit(oid, "TP")
        assert router.enter(_go_long(), "AAPL", 5, 155.0) is not None

    def test_no_go(self) -> None:
        router, _ = _make_router()
        assert router.enter(_no_go(), "AAPL", 10, 150.0) is None

    def test_zero_size(self) -> None:
        router, _ = _make_router()
        assert router.enter(_go_long(), "AAPL", 0, 150.0) is None

    def test_order_ids_increment(self) -> None:
        router, _ = _make_router()
        r1 = router.enter(_go_long(), "AAPL", 5, 150.0)
        r2 = router.enter(_go_long(), "NVDA", 5, 170.0)
        assert r1.order_id != r2.order_id


@patch("src.execution.order_router._is_simulate", True)
class TestSimulateExit:

    def test_exit_pnl(self) -> None:
        router, _ = _make_router(snapshot_price=155.0)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        result = router.exit(oid, "TP")
        assert isinstance(result, ExitResult)
        assert result.pnl == 50.0
        assert router.position_count == 0

    def test_exit_unknown(self) -> None:
        router, _ = _make_router()
        assert router.exit("UNKNOWN", "test") is None

    def test_exit_all(self) -> None:
        router, _ = _make_router()
        router.enter(_go_long(), "AAPL", 10, 150.0)
        router.enter(_go_long(), "NVDA", 5, 170.0)
        results = router.exit_all("force close")
        assert len(results) == 2
        assert router.position_count == 0


# ---------------------------------------------------------------------------
# REAL mode
# ---------------------------------------------------------------------------

@patch("src.execution.order_router._is_simulate", False)
class TestRealEntry:

    def test_entry_calls_api(self) -> None:
        router, mock_client = _make_router()
        result = router.enter(_go_long(), "AAPL", 10, 150.0)
        assert result.order_id.startswith("ORD-")
        assert router.position_count == 1
        mock_client.place_order.assert_called_once()

    def test_entry_failure(self) -> None:
        router, mock_client = _make_router()
        mock_client.place_order.side_effect = None
        mock_client.place_order.return_value = OrderResult(order_id="", status="FAILED")
        result = router.enter(_go_long(), "AAPL", 10, 150.0)
        assert result.status == "FAILED"
        assert router.position_count == 0


@patch("src.execution.order_router._is_simulate", False)
class TestRealExit:

    def test_exit_calls_api(self) -> None:
        router, mock_client = _make_router()
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        router.exit(oid, "SL")
        assert mock_client.place_order.call_count == 2

    def test_exit_failure_keeps_position(self) -> None:
        router, mock_client = _make_router()
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        mock_client.place_order.side_effect = None
        mock_client.place_order.return_value = OrderResult(order_id="", status="FAILED")
        assert router.exit(oid, "SL") is None
        assert router.position_count == 1


# ---------------------------------------------------------------------------
# on_exit callback (both modes)
# ---------------------------------------------------------------------------

@patch("src.execution.order_router._is_simulate", True)
class TestOnExit:

    def test_callback_called(self) -> None:
        cb = MagicMock()
        router, _ = _make_router(on_exit=cb)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        router.exit(oid, "TP")
        cb.assert_called_once()
        assert cb.call_args[0][0].pnl == 50.0

    def test_callback_on_exit_all(self) -> None:
        cb = MagicMock()
        router, _ = _make_router(on_exit=cb)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        router.enter(_go_long(), "NVDA", 5, 170.0)
        router.exit_all("close")
        assert cb.call_count == 2

    def test_callback_loss(self) -> None:
        cb = MagicMock()
        router, _ = _make_router(on_exit=cb, snapshot_price=140.0)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        router.exit(oid, "SL")
        assert cb.call_args[0][0].pnl == -100.0

    def test_callback_exception_safe(self) -> None:
        cb = MagicMock(side_effect=RuntimeError("boom"))
        router, _ = _make_router(on_exit=cb)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        assert router.exit(oid, "TP") is not None

    def test_no_callback_ok(self) -> None:
        router, _ = _make_router(on_exit=None)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        assert router.exit(oid, "TP") is not None


# ---------------------------------------------------------------------------
# Monitor (SIMULATE)
# ---------------------------------------------------------------------------

@patch("src.execution.order_router._is_simulate", True)
class TestMonitor:

    @pytest.mark.asyncio
    async def test_sl(self) -> None:
        cb = MagicMock()
        router, _ = _make_router(on_exit=cb, snapshot_price=144.0)
        router.enter(_go_long(), "AAPL", 10, 150.0, _levels())
        import asyncio
        task = asyncio.create_task(router.monitor_positions())
        await asyncio.sleep(0.1)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        assert router.position_count == 0
        assert cb.call_args[0][0].reason == "SL"

    @pytest.mark.asyncio
    async def test_tp(self) -> None:
        cb = MagicMock()
        router, _ = _make_router(on_exit=cb, snapshot_price=161.0)
        router.enter(_go_long(), "AAPL", 10, 150.0, _levels())
        import asyncio
        task = asyncio.create_task(router.monitor_positions())
        await asyncio.sleep(0.1)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        assert router.position_count == 0
        assert cb.call_args[0][0].reason == "TP"

    @pytest.mark.asyncio
    async def test_no_exit_in_range(self) -> None:
        cb = MagicMock()
        router, _ = _make_router(on_exit=cb, snapshot_price=152.0)
        router.enter(_go_long(), "AAPL", 10, 150.0, _levels())
        import asyncio
        task = asyncio.create_task(router.monitor_positions())
        await asyncio.sleep(0.1)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        assert router.position_count == 1
        cb.assert_not_called()
