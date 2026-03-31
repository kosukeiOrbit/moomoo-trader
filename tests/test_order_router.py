"""OrderRouter のユニットテスト."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# futu SDK mock
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
    paper: bool = True,
    on_exit: object = None,
    snapshot_price: float = 155.0,
) -> tuple[OrderRouter, MagicMock]:
    mock_client = MagicMock()
    mock_client.get_snapshot.return_value = QuoteSnapshot(
        symbol="AAPL", last_price=snapshot_price, volume=0, turnover=0,
    )
    cb = CircuitBreaker()
    router = OrderRouter(mock_client, cb, paper_trade=paper, on_exit=on_exit)
    return router, mock_client


# ---------------------------------------------------------------------------
# Paper trade
# ---------------------------------------------------------------------------

class TestOrderRouterPaper:

    def test_enter_paper(self) -> None:
        router, _ = _make_router(paper=True)
        result = router.enter(_go_long(), "AAPL", 10, 150.0, _levels())
        assert result is not None
        assert result.status == "PAPER_FILLED"
        assert router.position_count == 1

    def test_duplicate_entry_blocked(self) -> None:
        """Same symbol cannot have two positions."""
        router, _ = _make_router(paper=True)
        r1 = router.enter(_go_long(), "AAPL", 10, 150.0)
        r2 = router.enter(_go_long(), "AAPL", 5, 151.0)
        assert r1 is not None
        assert r2 is None  # blocked
        assert router.position_count == 1

    def test_different_symbols_allowed(self) -> None:
        """Different symbols can have separate positions."""
        router, _ = _make_router(paper=True)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        router.enter(_go_long(), "NVDA", 5, 170.0)
        assert router.position_count == 2

    def test_reentry_after_exit(self) -> None:
        """Can re-enter same symbol after exiting."""
        router, _ = _make_router(paper=True)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        router.exit(oid, "TP")
        r2 = router.enter(_go_long(), "AAPL", 5, 155.0)
        assert r2 is not None
        assert router.position_count == 1

    def test_exit_paper_returns_exit_result(self) -> None:
        router, _ = _make_router(paper=True)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        result = router.exit(oid, "TP")
        assert isinstance(result, ExitResult)
        assert result.exit_price == 155.0  # from mock snapshot
        assert result.pnl == 50.0  # (155-150)*10
        assert result.reason == "TP"
        assert router.position_count == 0

    def test_no_go_returns_none(self) -> None:
        router, _ = _make_router()
        assert router.enter(_no_go(), "AAPL", 10, 150.0) is None

    def test_zero_size_returns_none(self) -> None:
        router, _ = _make_router()
        assert router.enter(_go_long(), "AAPL", 0, 150.0) is None

    def test_exit_unknown_returns_none(self) -> None:
        router, _ = _make_router()
        assert router.exit("UNKNOWN", "test") is None

    def test_paper_order_id_increments(self) -> None:
        router, _ = _make_router()
        r1 = router.enter(_go_long(), "AAPL", 5, 150.0)
        r2 = router.enter(_go_long(), "NVDA", 5, 500.0)
        assert r1.order_id != r2.order_id
        assert router.position_count == 2


# ---------------------------------------------------------------------------
# Real mode
# ---------------------------------------------------------------------------

class TestOrderRouterReal:

    def test_enter_real_success(self) -> None:
        router, mock_client = _make_router(paper=False)
        mock_client.place_order.return_value = OrderResult(
            order_id="REAL-001", status="SUBMITTED",
        )
        result = router.enter(_go_long(), "AAPL", 10, 150.0)
        assert result.order_id == "REAL-001"
        assert router.position_count == 1

    def test_enter_real_failure(self) -> None:
        router, mock_client = _make_router(paper=False)
        mock_client.place_order.return_value = OrderResult(order_id="", status="FAILED")
        result = router.enter(_go_long(), "AAPL", 10, 150.0)
        assert result.status == "FAILED"
        assert router.position_count == 0

    def test_exit_real_success(self) -> None:
        router, mock_client = _make_router(paper=False)
        mock_client.place_order.side_effect = [
            OrderResult(order_id="REAL-001", status="SUBMITTED"),
            OrderResult(order_id="REAL-002", status="SUBMITTED"),
        ]
        router.enter(_go_long(), "AAPL", 10, 150.0)
        result = router.exit("REAL-001", "SL")
        assert result is not None
        assert result.order_result.status == "SUBMITTED"
        assert router.position_count == 0


# ---------------------------------------------------------------------------
# exit_all
# ---------------------------------------------------------------------------

class TestOrderRouterExitAll:

    def test_exit_all_closes_all(self) -> None:
        router, _ = _make_router(paper=True)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        router.enter(_go_long(), "NVDA", 5, 500.0)

        results = router.exit_all("force close")
        assert len(results) == 2
        assert router.position_count == 0
        assert all(isinstance(r, ExitResult) for r in results)

    def test_exit_all_empty(self) -> None:
        router, _ = _make_router()
        assert router.exit_all("test") == []


# ---------------------------------------------------------------------------
# on_exit callback
# ---------------------------------------------------------------------------

class TestOrderRouterOnExit:

    def test_on_exit_called_on_exit(self) -> None:
        """exit() で on_exit コールバックが呼ばれる."""
        callback = MagicMock()
        router, _ = _make_router(paper=True, on_exit=callback)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        router.exit(oid, "TP")

        callback.assert_called_once()
        result: ExitResult = callback.call_args[0][0]
        assert result.exit_price == 155.0
        assert result.pnl == 50.0
        assert result.reason == "TP"

    def test_on_exit_called_on_exit_all(self) -> None:
        """exit_all() で各ポジションに on_exit が呼ばれる."""
        callback = MagicMock()
        router, _ = _make_router(paper=True, on_exit=callback)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        router.enter(_go_long(), "NVDA", 5, 500.0)

        router.exit_all("force close")
        assert callback.call_count == 2

    def test_on_exit_loss(self) -> None:
        """損失の場合 pnl が負になる."""
        callback = MagicMock()
        # exit price 140 < entry 150 → loss
        router, _ = _make_router(paper=True, on_exit=callback, snapshot_price=140.0)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        router.exit(oid, "SL")

        result: ExitResult = callback.call_args[0][0]
        assert result.pnl == -100.0  # (140-150)*10
        assert result.reason == "SL"

    def test_on_exit_exception_does_not_crash(self) -> None:
        """on_exit がエラーを投げてもクラッシュしない."""
        callback = MagicMock(side_effect=RuntimeError("boom"))
        router, _ = _make_router(paper=True, on_exit=callback)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        # Should not raise
        result = router.exit(oid, "TP")
        assert result is not None

    def test_no_callback_is_ok(self) -> None:
        """on_exit 未設定でも正常動作."""
        router, _ = _make_router(paper=True, on_exit=None)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        result = router.exit(oid, "TP")
        assert result is not None


# ---------------------------------------------------------------------------
# PnL calculation
# ---------------------------------------------------------------------------

class TestOrderRouterPnL:

    def test_long_profit(self) -> None:
        """LONG: exit > entry → profit."""
        router, _ = _make_router(paper=True, snapshot_price=160.0)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        result = router.exit(oid, "TP")
        assert result.pnl == 100.0  # (160-150)*10

    def test_long_loss(self) -> None:
        """LONG: exit < entry → loss."""
        router, _ = _make_router(paper=True, snapshot_price=145.0)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        result = router.exit(oid, "SL")
        assert result.pnl == -50.0  # (145-150)*10


# ---------------------------------------------------------------------------
# Monitor positions
# ---------------------------------------------------------------------------

class TestOrderRouterMonitor:

    @pytest.mark.asyncio
    async def test_monitor_triggers_sl(self) -> None:
        callback = MagicMock()
        router, mock_client = _make_router(paper=True, on_exit=callback, snapshot_price=144.0)
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
        callback.assert_called_once()
        assert callback.call_args[0][0].reason == "SL"

    @pytest.mark.asyncio
    async def test_monitor_triggers_tp(self) -> None:
        callback = MagicMock()
        router, mock_client = _make_router(paper=True, on_exit=callback, snapshot_price=161.0)
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
        callback.assert_called_once()
        assert callback.call_args[0][0].reason == "TP"

    @pytest.mark.asyncio
    async def test_monitor_no_exit_in_range(self) -> None:
        callback = MagicMock()
        router, _ = _make_router(paper=True, on_exit=callback, snapshot_price=152.0)
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
