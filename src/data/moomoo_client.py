"""moomoo OpenAPIとの接続管理・リアルタイムデータ取得モジュール.

OpenQuoteContext / OpenSecTradeContext のライフサイクル管理、
リアルタイム購読、大口フロー・空売りデータ取得、発注を担当する。
接続断時は自動再接続を試みる。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from futu import (
    OpenQuoteContext,
    OpenSecTradeContext,
    RET_OK,
    OrderType,
    SubType,
    TrdEnv,
    TrdMarket,
    TrdSide,
)

from config import settings

logger = logging.getLogger(__name__)

RECONNECT_MAX_RETRIES = 3
RECONNECT_DELAY = 2.0  # 秒


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

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
class QuoteSnapshot:
    """株価スナップショット."""

    symbol: str
    last_price: float
    volume: float
    turnover: float


@dataclass
class Order:
    """発注情報."""

    symbol: str
    side: str  # "BUY" or "SELL"
    quantity: int
    price: float | None = None  # None = 成行


@dataclass
class OrderResult:
    """発注結果."""

    order_id: str
    status: str
    filled_price: float = 0.0
    filled_quantity: int = 0


# ---------------------------------------------------------------------------
# クライアント
# ---------------------------------------------------------------------------

class MoomooClient:
    """moomoo OpenAPI クライアント.

    TRADE_ENV=SIMULATE の場合はペーパートレード環境に接続し、
    TRADE_ENV=REAL の場合は本番環境に接続する。
    """

    def __init__(self) -> None:
        self._quote_ctx: OpenQuoteContext | None = None
        self._trade_ctx: OpenSecTradeContext | None = None
        self._trd_env: Any = None
        self._connected: bool = False

    @property
    def is_connected(self) -> bool:
        """接続済みかどうか."""
        return self._connected

    # ------------------------------------------------------------------
    # 接続・切断
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """moomoo OpenD に接続する."""
        self._trd_env = TrdEnv.SIMULATE if settings.TRADE_ENV == "SIMULATE" else TrdEnv.REAL

        self._quote_ctx = OpenQuoteContext(
            host=settings.MOOMOO_HOST,
            port=settings.MOOMOO_PORT,
        )
        self._trade_ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=settings.MOOMOO_HOST,
            port=settings.MOOMOO_PORT,
        )

        # トレードパスワードでアンロック（本番のみ。ペーパートレードは不要）
        if self._trd_env != TrdEnv.SIMULATE and settings.MOOMOO_TRADE_PWD:
            ret, data = self._trade_ctx.unlock_trade(settings.MOOMOO_TRADE_PWD)
            if ret != RET_OK:
                logger.error("トレードアンロック失敗: %s", data)

        self._connected = True
        logger.info(
            "moomoo OpenAPI 接続完了 (host=%s:%d env=%s)",
            settings.MOOMOO_HOST, settings.MOOMOO_PORT, settings.TRADE_ENV,
        )

    def reconnect(self) -> bool:
        """接続断時に自動再接続を試みる.

        Returns:
            再接続成功なら True
        """
        for attempt in range(1, RECONNECT_MAX_RETRIES + 1):
            logger.warning("再接続試行 %d/%d ...", attempt, RECONNECT_MAX_RETRIES)
            try:
                self.close()
                self.connect()
                logger.info("再接続成功")
                return True
            except Exception as e:
                logger.error("再接続失敗: %s", e)
                time.sleep(RECONNECT_DELAY * attempt)
        logger.critical("再接続上限到達 — 全試行失敗")
        return False

    def close(self) -> None:
        """接続を閉じる."""
        if self._quote_ctx:
            self._quote_ctx.close()
            self._quote_ctx = None
        if self._trade_ctx:
            self._trade_ctx.close()
            self._trade_ctx = None
        self._connected = False
        logger.info("moomoo OpenAPI 接続を閉じました")

    # ------------------------------------------------------------------
    # 購読
    # ------------------------------------------------------------------

    def subscribe_realtime(self, symbols: list[str]) -> None:
        """リアルタイムデータ（株価・板情報・約定）を購読する.

        Args:
            symbols: 銘柄シンボルのリスト (例: ["AAPL", "NVDA"])
        """
        assert self._quote_ctx is not None, "connect() を先に呼んでください"
        codes = [f"US.{s}" for s in symbols]
        sub_types = [SubType.QUOTE, SubType.ORDER_BOOK, SubType.TICKER]
        ret, data = self._quote_ctx.subscribe(codes, sub_types)
        if ret != RET_OK:
            logger.error("購読失敗: %s", data)
        else:
            logger.info("購読開始: %s", codes)

    # ------------------------------------------------------------------
    # 株価取得
    # ------------------------------------------------------------------

    def get_snapshot(self, symbol: str) -> QuoteSnapshot:
        """指定銘柄の株価スナップショットを取得する.

        Args:
            symbol: 銘柄シンボル (例: "AAPL")

        Returns:
            株価スナップショット
        """
        assert self._quote_ctx is not None
        code = f"US.{symbol}"
        ret, data = self._quote_ctx.get_market_snapshot([code])
        if ret != RET_OK or data.empty:
            logger.warning("株価取得失敗: %s", symbol)
            return QuoteSnapshot(symbol=symbol, last_price=0.0, volume=0.0, turnover=0.0)

        row = data.iloc[0]
        return QuoteSnapshot(
            symbol=symbol,
            last_price=float(row.get("last_price", 0)),
            volume=float(row.get("volume", 0)),
            turnover=float(row.get("turnover", 0)),
        )

    # ------------------------------------------------------------------
    # 大口フロー
    # ------------------------------------------------------------------

    def get_institutional_flow(self, symbol: str) -> FlowData:
        """大口投資家フローデータを取得する.

        Args:
            symbol: 銘柄シンボル

        Returns:
            大口フローデータ
        """
        assert self._quote_ctx is not None
        code = f"US.{symbol}"
        ret, data = self._quote_ctx.get_capital_distribution(code)
        if ret != RET_OK:
            logger.warning("大口フローデータ取得失敗: %s", symbol)
            return FlowData(symbol=symbol, big_buy=0.0, big_sell=0.0, net_flow=0.0)

        big_buy = float(data.iloc[0].get("capital_in_big", 0))
        big_sell = float(data.iloc[0].get("capital_out_big", 0))
        return FlowData(
            symbol=symbol,
            big_buy=big_buy,
            big_sell=big_sell,
            net_flow=big_buy - big_sell,
        )

    # ------------------------------------------------------------------
    # 空売りデータ
    # ------------------------------------------------------------------

    def get_short_data(self, symbol: str) -> ShortData:
        """空売り比率データを取得する.

        Args:
            symbol: 銘柄シンボル

        Returns:
            空売り比率データ
        """
        assert self._quote_ctx is not None
        code = f"US.{symbol}"
        ret, data = self._quote_ctx.get_capital_flow(code)
        if ret != RET_OK:
            logger.warning("空売りデータ取得失敗: %s", symbol)
            return ShortData(symbol=symbol, short_volume=0.0, short_ratio=0.0)

        return ShortData(
            symbol=symbol,
            short_volume=float(data.iloc[0].get("short_volume", 0)),
            short_ratio=float(data.iloc[0].get("short_ratio", 0)),
        )

    # ------------------------------------------------------------------
    # 口座残高
    # ------------------------------------------------------------------

    def get_account_balance(self) -> float:
        """口座の総資産を取得する.

        Returns:
            総資産額（取得失敗時は0.0）
        """
        assert self._trade_ctx is not None
        ret, data = self._trade_ctx.accinfo_query(
            trd_env=self._trd_env,
            currency="USD",
        )
        if ret != RET_OK or data.empty:
            logger.warning("口座残高取得失敗")
            return 0.0
        return float(data.iloc[0].get("total_assets", 0))

    # ------------------------------------------------------------------
    # 発注
    # ------------------------------------------------------------------

    def place_order(self, order: Order) -> OrderResult:
        """注文を発注する.

        Args:
            order: 発注情報

        Returns:
            発注結果
        """
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
            trd_env=self._trd_env,
        )
        if ret != RET_OK:
            logger.error("発注失敗: %s — %s", order, data)
            return OrderResult(order_id="", status="FAILED")

        order_id = str(data["order_id"].iloc[0])
        logger.info("発注成功: %s order_id=%s", order, order_id)
        return OrderResult(order_id=order_id, status="SUBMITTED")
