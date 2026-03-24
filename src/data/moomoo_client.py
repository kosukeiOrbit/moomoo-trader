"""moomoo OpenAPIとの接続管理・リアルタイムデータ取得モジュール."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from moomoo import (
    OpenQuoteContext,
    OpenSecTradeContext,
    TrdEnv,
    TrdMarket,
    TrdSide,
    OrderType,
    RET_OK,
)

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class ShortData:
    """空売り比率データ."""
    symbol: str
    short_volume: float
    short_ratio: float


@dataclass
class FlowData:
    """大口投資家フローデータ."""
    symbol: str
    big_buy: float
    big_sell: float
    net_flow: float


@dataclass
class Order:
    """発注情報."""
    symbol: str
    side: str  # "BUY" or "SELL"
    quantity: int
    price: float | None = None  # Noneの場合は成行


@dataclass
class OrderResult:
    """発注結果."""
    order_id: str
    status: str
    filled_price: float = 0.0
    filled_quantity: int = 0


class MoomooClient:
    """moomoo OpenAPIクライアント."""

    def __init__(self) -> None:
        self._quote_ctx: OpenQuoteContext | None = None
        self._trade_ctx: OpenSecTradeContext | None = None

    def connect(self) -> None:
        """moomoo OpenD に接続する."""
        self._quote_ctx = OpenQuoteContext(
            host=settings.MOOMOO_HOST,
            port=settings.MOOMOO_PORT,
        )
        trd_env = TrdEnv.SIMULATE if settings.TRADE_ENV == "SIMULATE" else TrdEnv.REAL
        self._trade_ctx = OpenSecTradeContext(
            host=settings.MOOMOO_HOST,
            port=settings.MOOMOO_PORT,
            trd_env=trd_env,
        )
        if settings.MOOMOO_TRADE_PWD:
            self._trade_ctx.unlock_trade(settings.MOOMOO_TRADE_PWD)
        logger.info("moomoo OpenAPI に接続しました (env=%s)", settings.TRADE_ENV)

    def subscribe_realtime(self, symbols: list[str]) -> None:
        """リアルタイムデータを購読する."""
        assert self._quote_ctx is not None, "connect() を先に呼んでください"
        for symbol in symbols:
            code = f"US.{symbol}"
            ret, data = self._quote_ctx.subscribe([code], ["QUOTE", "ORDER_BOOK", "TICKER"])
            if ret != RET_OK:
                logger.error("購読失敗: %s - %s", code, data)

    def get_short_data(self, symbol: str) -> ShortData:
        """空売り比率データを取得する."""
        assert self._quote_ctx is not None
        code = f"US.{symbol}"
        ret, data = self._quote_ctx.get_capital_flow(code)
        if ret != RET_OK:
            logger.warning("空売りデータ取得失敗: %s", symbol)
            return ShortData(symbol=symbol, short_volume=0.0, short_ratio=0.0)
        return ShortData(
            symbol=symbol,
            short_volume=float(data.get("short_volume", 0)),
            short_ratio=float(data.get("short_ratio", 0)),
        )

    def get_institutional_flow(self, symbol: str) -> FlowData:
        """大口投資家フローデータを取得する."""
        assert self._quote_ctx is not None
        code = f"US.{symbol}"
        ret, data = self._quote_ctx.get_capital_distribution(code)
        if ret != RET_OK:
            logger.warning("大口フローデータ取得失敗: %s", symbol)
            return FlowData(symbol=symbol, big_buy=0.0, big_sell=0.0, net_flow=0.0)
        big_buy = float(data.get("capital_in_big", 0))
        big_sell = float(data.get("capital_out_big", 0))
        return FlowData(
            symbol=symbol,
            big_buy=big_buy,
            big_sell=big_sell,
            net_flow=big_buy - big_sell,
        )

    def place_order(self, order: Order) -> OrderResult:
        """注文を発注する."""
        assert self._trade_ctx is not None
        code = f"US.{order.symbol}"
        side = TrdSide.BUY if order.side == "BUY" else TrdSide.SELL
        order_type = OrderType.MARKET if order.price is None else OrderType.NORMAL
        price = order.price or 0.0

        ret, data = self._trade_ctx.place_order(
            price=price,
            qty=order.quantity,
            code=code,
            trd_side=side,
            order_type=order_type,
            trd_env=(
                TrdEnv.SIMULATE if settings.TRADE_ENV == "SIMULATE" else TrdEnv.REAL
            ),
            trd_market=TrdMarket.US,
        )
        if ret != RET_OK:
            logger.error("発注失敗: %s - %s", order, data)
            return OrderResult(order_id="", status="FAILED")

        order_id = str(data["order_id"].iloc[0])
        return OrderResult(order_id=order_id, status="SUBMITTED")

    def close(self) -> None:
        """接続を閉じる."""
        if self._quote_ctx:
            self._quote_ctx.close()
        if self._trade_ctx:
            self._trade_ctx.close()
        logger.info("moomoo OpenAPI 接続を閉じました")
