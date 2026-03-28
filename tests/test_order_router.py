"""OrderRouter のユニットテスト."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# moomoo SDK が未インストールでもテスト可能にする
if "moomoo" not in sys.modules:
    sys.modules["moomoo"] = MagicMock()

from src.data.moomoo_client import Order, OrderResult, QuoteSnapshot
from src.execution.order_router import OrderRouter, Position
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.stop_loss import Levels
from src.signal.and_filter import EntryDecision


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _go_long() -> EntryDecision:
    return EntryDecision(go=True, direction="LONG", sentiment_score=0.5, flow_strength=0.8)


def _no_go() -> EntryDecision:
    return EntryDecision(go=False, reason="test")


def _make_router(paper: bool = True) -> tuple[OrderRouter, MagicMock]:
    mock_client = MagicMock()
    cb = CircuitBreaker()
    router = OrderRouter(mock_client, cb, paper_trade=paper)
    return router, mock_client


def _levels() -> Levels:
    return Levels(stop_loss=145.0, take_profit=160.0, trailing_stop=147.0)


# ---------------------------------------------------------------------------
# ペーパートレード
# ---------------------------------------------------------------------------

class TestOrderRouterPaper:
    """ペーパートレードモードのテスト."""

    def test_enter_paper(self) -> None:
        """ペーパーモードでエントリーできる."""
        router, _ = _make_router(paper=True)
        result = router.enter(_go_long(), "AAPL", 10, 150.0, _levels())
        assert result is not None
        assert result.status == "PAPER_FILLED"
        assert router.position_count == 1

    def test_exit_paper(self) -> None:
        """ペーパーモードで決済できる."""
        router, _ = _make_router(paper=True)
        result = router.enter(_go_long(), "AAPL", 10, 150.0)
        assert result is not None
        exit_result = router.exit(result.order_id, "TP")
        assert exit_result is not None
        assert exit_result.status == "PAPER_CLOSED"
        assert router.position_count == 0

    def test_no_go_returns_none(self) -> None:
        """go=False のとき None を返す."""
        router, _ = _make_router()
        assert router.enter(_no_go(), "AAPL", 10, 150.0) is None

    def test_zero_size_returns_none(self) -> None:
        """size=0 のとき None を返す."""
        router, _ = _make_router()
        assert router.enter(_go_long(), "AAPL", 0, 150.0) is None

    def test_exit_unknown_returns_none(self) -> None:
        """存在しないポジションの決済は None."""
        router, _ = _make_router()
        assert router.exit("UNKNOWN", "test") is None

    def test_paper_order_id_increments(self) -> None:
        """ペーパー注文IDが連番になる."""
        router, _ = _make_router()
        r1 = router.enter(_go_long(), "AAPL", 5, 150.0)
        r2 = router.enter(_go_long(), "NVDA", 5, 500.0)
        assert r1.order_id != r2.order_id
        assert router.position_count == 2


# ---------------------------------------------------------------------------
# 実弾モード
# ---------------------------------------------------------------------------

class TestOrderRouterReal:
    """実弾モードのテスト."""

    def test_enter_real_success(self) -> None:
        """実弾エントリー成功."""
        router, mock_client = _make_router(paper=False)
        mock_client.place_order.return_value = OrderResult(
            order_id="REAL-001", status="SUBMITTED",
        )
        result = router.enter(_go_long(), "AAPL", 10, 150.0)
        assert result.order_id == "REAL-001"
        assert router.position_count == 1
        mock_client.place_order.assert_called_once()

    def test_enter_real_failure(self) -> None:
        """実弾エントリー失敗 → ポジション記録なし."""
        router, mock_client = _make_router(paper=False)
        mock_client.place_order.return_value = OrderResult(
            order_id="", status="FAILED",
        )
        result = router.enter(_go_long(), "AAPL", 10, 150.0)
        assert result.status == "FAILED"
        assert router.position_count == 0

    def test_exit_real_success(self) -> None:
        """実弾決済成功."""
        router, mock_client = _make_router(paper=False)
        mock_client.place_order.side_effect = [
            OrderResult(order_id="REAL-001", status="SUBMITTED"),
            OrderResult(order_id="REAL-002", status="SUBMITTED"),
        ]
        router.enter(_go_long(), "AAPL", 10, 150.0)
        result = router.exit("REAL-001", "SL")
        assert result.status == "SUBMITTED"
        assert router.position_count == 0


# ---------------------------------------------------------------------------
# exit_all
# ---------------------------------------------------------------------------

class TestOrderRouterExitAll:
    """exit_all() のテスト."""

    def test_exit_all_closes_all(self) -> None:
        """全ポジションが決済される."""
        router, _ = _make_router(paper=True)
        router.enter(_go_long(), "AAPL", 10, 150.0)
        router.enter(_go_long(), "NVDA", 5, 500.0)
        assert router.position_count == 2

        results = router.exit_all("05:50 JST 強制決済")
        assert len(results) == 2
        assert router.position_count == 0

    def test_exit_all_empty(self) -> None:
        """ポジションなしなら空リスト."""
        router, _ = _make_router()
        assert router.exit_all("test") == []


# ---------------------------------------------------------------------------
# ポジション監視
# ---------------------------------------------------------------------------

class TestOrderRouterMonitor:
    """monitor_positions() のテスト."""

    @pytest.mark.asyncio
    async def test_monitor_triggers_sl(self) -> None:
        """株価がSLを下回ると自動決済する."""
        router, mock_client = _make_router(paper=True)
        levels = Levels(stop_loss=145.0, take_profit=160.0, trailing_stop=147.0)
        router.enter(_go_long(), "AAPL", 10, 150.0, levels)

        # SL以下の株価を返す
        mock_client.get_snapshot.return_value = QuoteSnapshot(
            symbol="AAPL", last_price=144.0, volume=0, turnover=0,
        )

        import asyncio
        task = asyncio.create_task(router.monitor_positions())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # SLで決済されている
        assert router.position_count == 0

    @pytest.mark.asyncio
    async def test_monitor_triggers_tp(self) -> None:
        """株価がTPを上回ると自動決済する."""
        router, mock_client = _make_router(paper=True)
        levels = Levels(stop_loss=145.0, take_profit=160.0, trailing_stop=147.0)
        router.enter(_go_long(), "AAPL", 10, 150.0, levels)

        mock_client.get_snapshot.return_value = QuoteSnapshot(
            symbol="AAPL", last_price=161.0, volume=0, turnover=0,
        )

        import asyncio
        task = asyncio.create_task(router.monitor_positions())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert router.position_count == 0

    @pytest.mark.asyncio
    async def test_monitor_no_exit_in_range(self) -> None:
        """株価がSL/TP範囲内なら決済しない."""
        router, mock_client = _make_router(paper=True)
        levels = Levels(stop_loss=145.0, take_profit=160.0, trailing_stop=147.0)
        router.enter(_go_long(), "AAPL", 10, 150.0, levels)

        mock_client.get_snapshot.return_value = QuoteSnapshot(
            symbol="AAPL", last_price=152.0, volume=0, turnover=0,
        )

        import asyncio
        task = asyncio.create_task(router.monitor_positions())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert router.position_count == 1
