"""Integration tests: exit flow triggers pnl_tracker, position_sizer, notifier."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

if "futu" not in sys.modules:
    sys.modules["futu"] = MagicMock()

from src.data.moomoo_client import QuoteSnapshot, OrderResult
from src.execution.order_router import OrderRouter, ExitResult
from src.risk.circuit_breaker import CircuitBreaker, AccountState
from src.risk.position_sizer import PositionSizer, TradeResult
from src.risk.stop_loss import Levels
from src.monitor.pnl_tracker import PnLTracker
from src.monitor.notifier import Notifier
from src.signals.and_filter import EntryDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _go_long() -> EntryDecision:
    return EntryDecision(go=True, direction="LONG")


def _setup(exit_price: float = 155.0):
    """Full pipeline: client + router + pnl + sizer + notifier wired together."""
    mock_client = MagicMock()
    mock_client.get_snapshot.return_value = QuoteSnapshot(
        symbol="AAPL", last_price=exit_price, volume=0, turnover=0,
    )
    _seq = iter(range(1, 100))
    mock_client.place_order.side_effect = lambda order: OrderResult(
        order_id=f"ORD-{next(_seq)}", status="SUBMITTED",
    )

    cb = CircuitBreaker()
    pnl_tracker = PnLTracker()
    position_sizer = PositionSizer()
    notifier = Notifier(webhook_signal="", webhook_alert="", webhook_summary="")

    def _on_exit(result: ExitResult) -> None:
        pnl = pnl_tracker.close_trade(
            result.position.order_id, result.exit_price, result.reason,
        )
        is_win = pnl > 0
        position_sizer.update_stats(TradeResult(
            symbol=result.position.symbol, pnl=pnl, is_win=is_win,
        ))

    router = OrderRouter(mock_client, cb, on_exit=_on_exit)
    return router, pnl_tracker, position_sizer, mock_client


# ---------------------------------------------------------------------------
# Integration: exit triggers all downstream updates
# ---------------------------------------------------------------------------

class TestExitIntegration:

    def test_exit_updates_pnl_tracker(self) -> None:
        """exit() triggers pnl_tracker.close_trade()."""
        router, pnl, sizer, _ = _setup(exit_price=155.0)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        pnl.register(oid, "AAPL", "LONG", 10, 150.0)

        router.exit(oid, "TP")
        assert pnl.daily_pnl == pytest.approx(50.0)  # (155-150)*10
        assert pnl.closed_trade_count == 1

    def test_exit_updates_position_sizer_win(self) -> None:
        """Profitable exit updates win_rate in position_sizer."""
        router, pnl, sizer, _ = _setup(exit_price=160.0)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        pnl.register(oid, "AAPL", "LONG", 10, 150.0)

        router.exit(oid, "TP")
        assert sizer.win_rate == 1.0
        assert sizer.consecutive_losses == 0

    def test_exit_updates_position_sizer_loss(self) -> None:
        """Losing exit increments consecutive_losses."""
        router, pnl, sizer, _ = _setup(exit_price=145.0)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        pnl.register(oid, "AAPL", "LONG", 10, 150.0)

        router.exit(oid, "SL")
        assert sizer.win_rate == 0.0
        assert sizer.consecutive_losses == 1

    def test_exit_all_updates_all_trackers(self) -> None:
        """exit_all() updates pnl_tracker and position_sizer for each position."""
        router, pnl, sizer, mock_client = _setup(exit_price=155.0)

        # Two positions
        router.enter(_go_long(), "AAPL", 10, 150.0)
        router.enter(_go_long(), "NVDA", 5, 500.0)
        for oid, pos in router.open_positions.items():
            pnl.register(oid, pos.symbol, "LONG", pos.size, pos.entry_price)

        # NVDA: exit_price=155, entry=500 → loss
        # AAPL: exit_price=155, entry=150 → profit
        router.exit_all("force close")

        assert pnl.closed_trade_count == 2
        assert sizer.trade_count == 2

    def test_consecutive_losses_accumulate(self) -> None:
        """Multiple losing trades accumulate consecutive_losses."""
        router, pnl, sizer, _ = _setup(exit_price=145.0)

        for i in range(3):
            router.enter(_go_long(), "AAPL", 10, 150.0)
            oid = list(router.open_positions.keys())[0]
            pnl.register(oid, "AAPL", "LONG", 10, 150.0)
            router.exit(oid, "SL")

        assert sizer.consecutive_losses == 3
        assert sizer.trade_count == 3

    def test_win_resets_consecutive_losses(self) -> None:
        """A win after losses resets consecutive_losses to 0."""
        router_loss, pnl_loss, sizer, _ = _setup(exit_price=145.0)

        # 2 losses
        for i in range(2):
            router_loss.enter(_go_long(), "AAPL", 10, 150.0)
            oid = list(router_loss.open_positions.keys())[0]
            pnl_loss.register(oid, "AAPL", "LONG", 10, 150.0)
            router_loss.exit(oid, "SL")

        assert sizer.consecutive_losses == 2

        # 1 win (need new router with higher exit price, same sizer)
        mock_client2 = MagicMock()
        mock_client2.get_snapshot.return_value = QuoteSnapshot(
            symbol="AAPL", last_price=160.0, volume=0, turnover=0,
        )
        _seq2 = iter(range(100, 200))
        mock_client2.place_order.side_effect = lambda order: OrderResult(
            order_id=f"ORD-{next(_seq2)}", status="SUBMITTED",
        )

        def _on_exit2(result: ExitResult) -> None:
            p = pnl_loss.close_trade(result.position.order_id, result.exit_price, result.reason)
            sizer.update_stats(TradeResult(symbol="AAPL", pnl=p, is_win=p > 0))

        router_win = OrderRouter(mock_client2, CircuitBreaker(), on_exit=_on_exit2)
        router_win.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router_win.open_positions.keys())[0]
        pnl_loss.register(oid, "AAPL", "LONG", 10, 150.0)
        router_win.exit(oid, "TP")

        assert sizer.consecutive_losses == 0

    def test_consecutive_losses_passed_to_circuit_breaker(self) -> None:
        """AccountState receives actual consecutive_losses from position_sizer."""
        _, _, sizer, _ = _setup()

        # Simulate 3 losses
        for _ in range(3):
            sizer.update_stats(TradeResult(symbol="X", pnl=-50.0, is_win=False))

        state = AccountState(
            balance=100_000,
            daily_pnl=-150,
            peak_balance=100_000,
            consecutive_losses=sizer.consecutive_losses,
        )
        assert state.consecutive_losses == 3

    def test_force_exit_uses_real_price(self) -> None:
        """exit_all fetches current price from API, not 0.0."""
        router, pnl, sizer, _ = _setup(exit_price=152.0)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        oid = list(router.open_positions.keys())[0]
        pnl.register(oid, "AAPL", "LONG", 10, 150.0)

        results = router.exit_all("ET 15:50 force close")
        assert len(results) == 1
        assert results[0].exit_price == 152.0  # Real price, not 0.0
        assert results[0].pnl == pytest.approx(20.0)  # (152-150)*10
