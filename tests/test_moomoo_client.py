"""MoomooClient のユニットテスト（moomoo SDKをモック）."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# moomoo SDK が未インストールでもテスト可能にする
_mock_moomoo = MagicMock()
_mock_moomoo.RET_OK = 0
_mock_moomoo.SubType = MagicMock()
_mock_moomoo.TrdEnv = MagicMock()
_mock_moomoo.TrdEnv.SIMULATE = "SIMULATE"
_mock_moomoo.TrdEnv.REAL = "REAL"
_mock_moomoo.TrdMarket = MagicMock()
_mock_moomoo.TrdSide = MagicMock()
_mock_moomoo.OrderType = MagicMock()
sys.modules["futu"] = _mock_moomoo

from src.data.moomoo_client import (
    MoomooClient,
    FlowData,
    ShortData,
    QuoteSnapshot,
    Order,
    OrderResult,
    RET_OK,
)


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _make_client() -> MoomooClient:
    """モック済みクライアントを生成する.

    _quote_ctx と _trade_ctx を直接 MagicMock に差し替える。
    """
    client = MoomooClient()
    client._quote_ctx = MagicMock()
    client._trade_ctx = MagicMock()
    client._trade_ctx.unlock_trade.return_value = (RET_OK, "ok")
    client._trd_env = "SIMULATE"
    client._connected = True
    return client


# ---------------------------------------------------------------------------
# 接続
# ---------------------------------------------------------------------------

class TestMoomooClientConnect:
    """接続関連のテスト."""

    def test_connect_sets_connected(self) -> None:
        client = _make_client()
        assert client.is_connected is True

    def test_close_clears_connected(self) -> None:
        client = _make_client()
        client.close()
        assert client.is_connected is False

    def test_reconnect_success(self) -> None:
        client = _make_client()
        with patch.object(client, "close"), \
             patch.object(client, "connect"):
            assert client.reconnect() is True

    @patch("src.data.moomoo_client.time.sleep")
    def test_reconnect_retries_on_failure(self, mock_sleep: MagicMock) -> None:
        client = _make_client()
        with patch.object(client, "close"), \
             patch.object(client, "connect", side_effect=[Exception("fail"), None]):
            assert client.reconnect() is True

    @patch("src.data.moomoo_client.time.sleep")
    def test_reconnect_all_fail(self, mock_sleep: MagicMock) -> None:
        client = _make_client()
        with patch.object(client, "close"), \
             patch.object(client, "connect", side_effect=Exception("fail")):
            assert client.reconnect() is False


# ---------------------------------------------------------------------------
# 株価取得
# ---------------------------------------------------------------------------

class TestMoomooClientSnapshot:
    """get_snapshot() のテスト."""

    def test_snapshot_success(self) -> None:
        client = _make_client()
        df = pd.DataFrame([{"last_price": 182.5, "volume": 1_000_000, "turnover": 50_000_000}])
        client._quote_ctx.get_market_snapshot.return_value = (RET_OK, df)

        snap = client.get_snapshot("AAPL")
        assert snap.symbol == "AAPL"
        assert snap.last_price == 182.5

    def test_snapshot_failure_returns_zero(self) -> None:
        client = _make_client()
        client._quote_ctx.get_market_snapshot.return_value = (1, "error")

        snap = client.get_snapshot("AAPL")
        assert snap.last_price == 0.0


# ---------------------------------------------------------------------------
# 大口フロー
# ---------------------------------------------------------------------------

class TestMoomooClientFlow:
    """get_institutional_flow() のテスト."""

    def test_flow_success(self) -> None:
        client = _make_client()
        df = pd.DataFrame([{"capital_in_big": 500.0, "capital_out_big": 200.0}])
        client._quote_ctx.get_capital_distribution.return_value = (RET_OK, df)

        flow = client.get_institutional_flow("AAPL")
        assert flow.big_buy == 500.0
        assert flow.big_sell == 200.0
        assert flow.net_flow == 300.0

    def test_flow_failure_returns_zero(self) -> None:
        client = _make_client()
        client._quote_ctx.get_capital_distribution.return_value = (1, "error")

        flow = client.get_institutional_flow("AAPL")
        assert flow.big_buy == 0.0


# ---------------------------------------------------------------------------
# 空売りデータ
# ---------------------------------------------------------------------------

class TestMoomooClientShort:
    """get_short_data() のテスト."""

    def test_short_data_success(self) -> None:
        client = _make_client()
        df = pd.DataFrame([{"short_volume": 50000, "short_ratio": 0.25}])
        client._quote_ctx.get_capital_flow.return_value = (RET_OK, df)

        data = client.get_short_data("AAPL")
        assert data.short_ratio == 0.25

    def test_short_data_failure(self) -> None:
        client = _make_client()
        client._quote_ctx.get_capital_flow.return_value = (1, "error")

        data = client.get_short_data("AAPL")
        assert data.short_ratio == 0.0


# ---------------------------------------------------------------------------
# 口座残高
# ---------------------------------------------------------------------------

class TestMoomooClientBalance:
    """get_account_balance() のテスト."""

    def test_balance_success(self) -> None:
        client = _make_client()
        df = pd.DataFrame([{"total_assets": 100_000.0}])
        client._trade_ctx.accinfo_query.return_value = (RET_OK, df)

        assert client.get_account_balance() == 100_000.0

    def test_balance_failure(self) -> None:
        client = _make_client()
        client._trade_ctx.accinfo_query.return_value = (1, "error")

        assert client.get_account_balance() == 0.0


# ---------------------------------------------------------------------------
# 発注
# ---------------------------------------------------------------------------

class TestMoomooClientOrder:
    """place_order() のテスト."""

    def test_order_success(self) -> None:
        client = _make_client()
        df = pd.DataFrame([{"order_id": "ORD123"}])
        client._trade_ctx.place_order.return_value = (RET_OK, df)

        result = client.place_order(Order(symbol="AAPL", side="BUY", quantity=10))
        assert result.order_id == "ORD123"
        assert result.status == "SUBMITTED"

    def test_order_failure(self) -> None:
        client = _make_client()
        client._trade_ctx.place_order.return_value = (1, "error")

        result = client.place_order(Order(symbol="AAPL", side="BUY", quantity=10))
        assert result.status == "FAILED"

    def test_market_order(self) -> None:
        """price=None で成行注文."""
        client = _make_client()
        df = pd.DataFrame([{"order_id": "ORD456"}])
        client._trade_ctx.place_order.return_value = (RET_OK, df)

        order = Order(symbol="AAPL", side="SELL", quantity=5, price=None)
        result = client.place_order(order)
        assert result.status == "SUBMITTED"
