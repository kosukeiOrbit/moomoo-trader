"""OrderRouter tests: unified moomoo API + position_list_query() based confirmation."""

from __future__ import annotations

import asyncio
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
    # has_position: default False (no existing moomoo position)
    mock_client.has_position.return_value = False
    # get_positions: return position after place_order (simulating fill)
    mock_client.get_positions.return_value = {}
    router = OrderRouter(mock_client, CircuitBreaker(), on_exit=on_exit)
    return router, mock_client


def _make_router_with_fill(on_exit=None, snapshot_price: float = 155.0, fill_price: float = 150.0):
    """place_order → get_positions で約定を返すモック."""
    router, mock_client = _make_router(on_exit=on_exit, snapshot_price=snapshot_price)
    # _wait_for_fill が見つけるポジション
    mock_client.get_positions.return_value = {
        "AAPL": {"qty": 10, "cost_price": fill_price, "market_val": 0, "pl_val": 0},
        "NVDA": {"qty": 5, "cost_price": 170.0, "market_val": 0, "pl_val": 0},
    }
    return router, mock_client


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

@patch("src.execution.order_router.FILL_CHECK_INTERVAL", 0.01)
@patch("src.execution.order_router.FILL_CHECK_MAX_WAIT", 0.05)
class TestEntry:

    def test_entry_calls_api(self) -> None:
        router, mock_client = _make_router_with_fill()
        result = router.enter(_go_long(), "AAPL", 10, 150.0, _levels())
        assert result.status == "FILLED"
        assert result.order_id.startswith("ORD-")
        assert router.position_count == 1
        mock_client.place_order.assert_called_once()

    def test_duplicate_blocked_internal(self) -> None:
        router, _ = _make_router_with_fill()
        router.enter(_go_long(), "AAPL", 10, 150.0)
        assert router.enter(_go_long(), "AAPL", 5, 151.0) is None
        assert router.position_count == 1

    def test_duplicate_blocked_moomoo(self) -> None:
        router, mock_client = _make_router_with_fill()
        mock_client.has_position.return_value = True
        assert router.enter(_go_long(), "AAPL", 10, 150.0) is None
        assert router.position_count == 0

    def test_different_symbols(self) -> None:
        router, _ = _make_router_with_fill()
        router.enter(_go_long(), "AAPL", 10, 150.0)
        router.enter(_go_long(), "NVDA", 5, 170.0)
        assert router.position_count == 2

    def test_no_go(self) -> None:
        router, _ = _make_router()
        assert router.enter(_no_go(), "AAPL", 10, 150.0) is None

    def test_zero_size(self) -> None:
        router, _ = _make_router()
        assert router.enter(_go_long(), "AAPL", 0, 150.0) is None

    def test_entry_failure(self) -> None:
        router, mock_client = _make_router()
        mock_client.place_order.side_effect = None
        mock_client.place_order.return_value = OrderResult(order_id="", status="FAILED")
        result = router.enter(_go_long(), "AAPL", 10, 150.0)
        assert result.status == "FAILED"
        assert router.position_count == 0

    def test_fill_timeout_uses_local_price(self) -> None:
        """position_list に出なくても内部管理で続行する."""
        router, mock_client = _make_router()
        # get_positions は空 → タイムアウト
        mock_client.get_positions.return_value = {}
        result = router.enter(_go_long(), "AAPL", 10, 150.0)
        assert result.status == "FILLED"
        assert result.filled_price == 150.0
        assert router.position_count == 1

    def test_max_positions(self) -> None:
        router, _ = _make_router_with_fill()
        router.enter(_go_long(), "AAPL", 10, 150.0)
        router.enter(_go_long(), "NVDA", 5, 170.0)
        router.enter(_go_long(), "TSLA", 3, 300.0)
        assert router.enter(_go_long(), "META", 5, 400.0) is None
        assert router.position_count == 3


# ---------------------------------------------------------------------------
# Exit
# ---------------------------------------------------------------------------

@patch("src.execution.order_router.FILL_CHECK_INTERVAL", 0.01)
@patch("src.execution.order_router.FILL_CHECK_MAX_WAIT", 0.05)
class TestExit:

    def _enter_one(self, router, mock_client) -> str:
        mock_client.get_positions.return_value = {
            "AAPL": {"qty": 10, "cost_price": 150.0, "market_val": 0, "pl_val": 0},
        }
        router.enter(_go_long(), "AAPL", 10, 150.0, _levels())
        return list(router.open_positions.keys())[0]

    def test_exit_pnl_profit(self) -> None:
        router, mock_client = _make_router(snapshot_price=155.0)
        oid = self._enter_one(router, mock_client)
        # 決済後はポジション消滅
        mock_client.has_position.return_value = False
        mock_client.get_positions.return_value = {}
        result = router.exit(oid, "TP")
        assert isinstance(result, ExitResult)
        assert result.pnl == 50.0  # (155-150)*10
        assert router.position_count == 0

    def test_exit_pnl_loss(self) -> None:
        router, mock_client = _make_router(snapshot_price=140.0)
        oid = self._enter_one(router, mock_client)
        mock_client.has_position.return_value = False
        mock_client.get_positions.return_value = {}
        result = router.exit(oid, "SL")
        assert result.pnl == -100.0  # (140-150)*10

    def test_exit_unknown(self) -> None:
        router, _ = _make_router()
        assert router.exit("UNKNOWN", "test") is None

    def test_exit_calls_api(self) -> None:
        router, mock_client = _make_router(snapshot_price=155.0)
        oid = self._enter_one(router, mock_client)
        mock_client.has_position.return_value = False
        mock_client.get_positions.return_value = {}
        router.exit(oid, "SL")
        # enter で1回 + exit で1回
        assert mock_client.place_order.call_count == 2

    def test_exit_failure_keeps_position(self) -> None:
        router, mock_client = _make_router()
        oid = self._enter_one(router, mock_client)
        mock_client.place_order.side_effect = None
        mock_client.place_order.return_value = OrderResult(order_id="", status="FAILED")
        assert router.exit(oid, "SL") is None
        assert router.position_count == 1

    def test_exit_all(self) -> None:
        router, mock_client = _make_router(snapshot_price=155.0)
        mock_client.get_positions.return_value = {
            "AAPL": {"qty": 10, "cost_price": 150.0, "market_val": 0, "pl_val": 0},
            "NVDA": {"qty": 5, "cost_price": 170.0, "market_val": 0, "pl_val": 0},
        }
        router.enter(_go_long(), "AAPL", 10, 150.0)
        router.enter(_go_long(), "NVDA", 5, 170.0)
        mock_client.has_position.return_value = False
        mock_client.get_positions.return_value = {}
        results = router.exit_all("force close")
        assert len(results) == 2
        assert router.position_count == 0


# ---------------------------------------------------------------------------
# on_exit callback
# ---------------------------------------------------------------------------

@patch("src.execution.order_router.FILL_CHECK_INTERVAL", 0.01)
@patch("src.execution.order_router.FILL_CHECK_MAX_WAIT", 0.05)
class TestOnExit:

    def _enter_one(self, router, mock_client) -> str:
        mock_client.get_positions.return_value = {
            "AAPL": {"qty": 10, "cost_price": 150.0, "market_val": 0, "pl_val": 0},
        }
        router.enter(_go_long(), "AAPL", 10, 150.0, _levels())
        return list(router.open_positions.keys())[0]

    def test_callback_called(self) -> None:
        cb = MagicMock()
        router, mock_client = _make_router(on_exit=cb, snapshot_price=155.0)
        oid = self._enter_one(router, mock_client)
        mock_client.has_position.return_value = False
        mock_client.get_positions.return_value = {}
        router.exit(oid, "TP")
        cb.assert_called_once()
        assert cb.call_args[0][0].pnl == 50.0

    def test_callback_exception_safe(self) -> None:
        cb = MagicMock(side_effect=RuntimeError("boom"))
        router, mock_client = _make_router(on_exit=cb, snapshot_price=155.0)
        oid = self._enter_one(router, mock_client)
        mock_client.has_position.return_value = False
        mock_client.get_positions.return_value = {}
        assert router.exit(oid, "TP") is not None

    def test_no_callback_ok(self) -> None:
        router, mock_client = _make_router(on_exit=None, snapshot_price=155.0)
        oid = self._enter_one(router, mock_client)
        mock_client.has_position.return_value = False
        mock_client.get_positions.return_value = {}
        assert router.exit(oid, "TP") is not None


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

@patch("src.execution.order_router.FILL_CHECK_INTERVAL", 0.01)
@patch("src.execution.order_router.FILL_CHECK_MAX_WAIT", 0.05)
class TestMonitor:

    def _enter_one(self, router, mock_client, symbol="AAPL", price=150.0) -> str:
        mock_client.get_positions.return_value = {
            symbol: {"qty": 10, "cost_price": price, "market_val": 0, "pl_val": 0},
        }
        router.enter(_go_long(), symbol, 10, price, _levels())
        return list(router.open_positions.keys())[0]

    @pytest.mark.asyncio
    async def test_sl(self) -> None:
        cb = MagicMock()
        router, mock_client = _make_router(on_exit=cb, snapshot_price=144.0)
        self._enter_one(router, mock_client)
        mock_client.has_position.return_value = False
        mock_client.get_positions.return_value = {}
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
        router, mock_client = _make_router(on_exit=cb, snapshot_price=161.0)
        self._enter_one(router, mock_client)
        mock_client.has_position.return_value = False
        mock_client.get_positions.return_value = {}
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
        router, mock_client = _make_router(on_exit=cb, snapshot_price=152.0)
        self._enter_one(router, mock_client)
        task = asyncio.create_task(router.monitor_positions())
        await asyncio.sleep(0.1)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        assert router.position_count == 1
        cb.assert_not_called()
