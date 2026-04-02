"""moomoo OpenAPIとの接続管理・リアルタイムデータ取得モジュール.

OpenQuoteContext / OpenSecTradeContext のライフサイクル管理、
リアルタイム購読、大口フロー・空売りデータ取得、発注を担当する。
接続断時は自動再接続を試みる。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from futu import (
    ModifyOrderOp,
    OpenQuoteContext,
    OpenSecTradeContext,
    RET_OK,
    OrderType,
    SubAccType,
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
    """大口投資家フローデータ（分足ベース）."""

    symbol: str
    big_buy: float       # super + big の買いフロー
    big_sell: float      # super + big の売りフロー
    net_flow: float      # big_buy - big_sell
    timestamp: str = ""  # データの時刻


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
            security_firm=settings.SECURITY_FIRM,
        )

        # トレードパスワードでアンロック（SIMULATE / REAL 両方で必要）
        if settings.MOOMOO_TRADE_PWD:
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
        """リアルタイムデータ（株価・板情報・約定）を購読する."""
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
        """指定銘柄の株価スナップショットを取得する."""
        assert self._quote_ctx is not None
        code = f"US.{symbol}"
        ret, data = self._quote_ctx.get_market_snapshot([code])
        if ret != RET_OK or data.empty:
            logger.debug("Snapshot unavailable: %s (ret=%s)", symbol, ret)
            return QuoteSnapshot(symbol=symbol, last_price=0.0, volume=0.0, turnover=0.0)

        row = data.iloc[0]
        return QuoteSnapshot(
            symbol=symbol,
            last_price=float(row.get("last_price", 0)),
            volume=float(row.get("volume", 0)),
            turnover=float(row.get("turnover", 0)),
        )

    # ------------------------------------------------------------------
    # 大口フロー (get_capital_flow — 分足時系列)
    # ------------------------------------------------------------------

    def get_institutional_flow(self, symbol: str) -> FlowData:
        """大口投資家フローデータを取得する.

        get_capital_flow() は分足の時系列データを返す。
        直近データの super_in_flow + big_in_flow を大口買いフローとして使う。
        net flow が正なら買い超過、負なら売り超過。

        Args:
            symbol: 銘柄シンボル

        Returns:
            大口フローデータ
        """
        assert self._quote_ctx is not None
        code = f"US.{symbol}"
        ret, data = self._quote_ctx.get_capital_flow(code)
        if ret != RET_OK or data.empty:
            logger.warning("大口フローデータ取得失敗: %s (ret=%s)", symbol, ret)
            return FlowData(symbol=symbol, big_buy=0.0, big_sell=0.0, net_flow=0.0)

        # 直近の行を取得 (最新の分足データ)
        latest = data.iloc[-1]
        in_flow = float(latest.get("in_flow", 0))
        super_in = float(latest.get("super_in_flow", 0))
        big_in = float(latest.get("big_in_flow", 0))
        ts = str(latest.get("capital_flow_item_time", ""))

        # in_flow = net flow (買い - 売り の累積)
        # super + big のフローを大口として扱う
        big_net = super_in + big_in

        # big_net > 0 → 大口が買い超過、big_net < 0 → 大口が売り超過
        if big_net >= 0:
            big_buy = big_net
            big_sell = 0.0
        else:
            big_buy = 0.0
            big_sell = abs(big_net)

        logger.debug(
            "[%s] capital_flow: in_flow=%.0f super=%.0f big=%.0f big_net=%.0f ts=%s rows=%d",
            symbol, in_flow, super_in, big_in, big_net, ts, len(data),
        )

        return FlowData(
            symbol=symbol,
            big_buy=big_buy,
            big_sell=big_sell,
            net_flow=big_net,
            timestamp=ts,
        )

    # ------------------------------------------------------------------
    # 空売りデータ (get_capital_distribution — 日次スナップショット)
    # ------------------------------------------------------------------

    def get_short_data(self, symbol: str) -> ShortData:
        """大口の売り超過比率を空売り指標として取得する.

        get_capital_distribution() の super + big の out/in 比率を使う。
        (futu-api に直接の short interest API がないため代用)

        Args:
            symbol: 銘柄シンボル

        Returns:
            空売り比率データ
        """
        assert self._quote_ctx is not None
        code = f"US.{symbol}"
        ret, data = self._quote_ctx.get_capital_distribution(code)
        if ret != RET_OK or data.empty:
            logger.warning("空売りデータ取得失敗: %s", symbol)
            return ShortData(symbol=symbol, short_volume=0.0, short_ratio=0.0)

        row = data.iloc[0]
        cap_in_super = float(row.get("capital_in_super", 0))
        cap_in_big = float(row.get("capital_in_big", 0))
        cap_out_super = float(row.get("capital_out_super", 0))
        cap_out_big = float(row.get("capital_out_big", 0))

        total_big_in = cap_in_super + cap_in_big
        total_big_out = cap_out_super + cap_out_big
        total = total_big_in + total_big_out

        # 売り超過比率: 大口の売り / (大口の買い + 売り)
        short_ratio = total_big_out / total if total > 0 else 0.0

        logger.debug(
            "[%s] capital_dist: big_in=%.0f big_out=%.0f ratio=%.3f",
            symbol, total_big_in, total_big_out, short_ratio,
        )

        return ShortData(
            symbol=symbol,
            short_volume=total_big_out,
            short_ratio=short_ratio,
        )

    # ------------------------------------------------------------------
    # 口座残高
    # ------------------------------------------------------------------

    def get_account_balance(self) -> float:
        """口座の総資産を取得する."""
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
    # ポジション照会
    # ------------------------------------------------------------------

    def get_positions(self) -> dict[str, dict]:
        """ペーパー/本番口座の保有ポジションを取得する.

        Returns:
            {symbol: {"qty": float, "cost_price": float, "market_val": float, "pl_val": float}}
        """
        assert self._trade_ctx is not None
        ret, data = self._trade_ctx.position_list_query(trd_env=self._trd_env)
        if ret != RET_OK or data.empty:
            return {}
        result: dict[str, dict] = {}
        for _, row in data.iterrows():
            code = str(row["code"])  # "US.NVDA"
            symbol = code.replace("US.", "")
            result[symbol] = {
                "qty": float(row["qty"]),
                "cost_price": float(row["cost_price"]),
                "market_val": float(row.get("market_val", 0)),
                "pl_val": float(row.get("pl_val", 0)),
            }
        return result

    def has_position(self, symbol: str) -> bool:
        """指定銘柄のポジションを保有しているか."""
        positions = self.get_positions()
        return symbol in positions and positions[symbol]["qty"] > 0

    def get_order_status(self, order_id: str) -> str:
        """指定 order_id の注文ステータスを取得する."""
        assert self._trade_ctx is not None
        ret, data = self._trade_ctx.order_list_query(
            order_id=order_id, trd_env=self._trd_env,
        )
        if ret != RET_OK or data.empty:
            return "UNKNOWN"
        return str(data.iloc[0].get("order_status", "UNKNOWN"))

    # ------------------------------------------------------------------
    # 発注
    # ------------------------------------------------------------------

    def _get_sub_acc_type(self) -> Any:
        """settings.JP_ACC_TYPE から SubAccType を返す."""
        if settings.JP_ACC_TYPE == "SPECIFIC":
            return SubAccType.JP_GAIKOKU_TOKUTEI
        return SubAccType.JP_GAIKOKU_GENERAL

    def place_order(self, order: Order) -> OrderResult:
        """注文を発注する.

        SELL 時はポジションの口座区分と一致しない場合があるため、
        設定の口座区分で失敗したらもう一方でリトライする。
        """
        assert self._trade_ctx is not None
        code = f"US.{order.symbol}"
        side = TrdSide.BUY if order.side == "BUY" else TrdSide.SELL
        order_type = OrderType.MARKET if order.price is None else OrderType.NORMAL
        price = order.price or 0.0

        # 試行する口座区分リスト: 設定値 → もう一方
        primary = self._get_sub_acc_type()
        fallback = (
            SubAccType.JP_GAIKOKU_GENERAL
            if primary == SubAccType.JP_GAIKOKU_TOKUTEI
            else SubAccType.JP_GAIKOKU_TOKUTEI
        )
        acc_types = [primary] if order.side == "BUY" else [primary, fallback]

        for acc_type in acc_types:
            ret, data = self._trade_ctx.place_order(
                price=price,
                qty=order.quantity,
                code=code,
                trd_side=side,
                order_type=order_type,
                trd_env=self._trd_env,
                jp_acc_type=acc_type,
            )
            if ret == RET_OK and not data.empty:
                order_id = str(data["order_id"].iloc[0])
                logger.info("発注成功: %s order_id=%s (acc_type=%s)", order, order_id, acc_type)
                return OrderResult(order_id=order_id, status="SUBMITTED")

            error_msg = str(data) if not isinstance(data, str) else data
            if order.side == "SELL" and "Sub account type" in error_msg and acc_type != fallback:
                logger.warning(
                    "SELL 口座区分不一致 — %s でリトライ: %s", fallback, order.symbol,
                )
                continue

            logger.error("発注失敗: %s (acc_type=%s) — %s", order, acc_type, data)
            return OrderResult(order_id="", status="FAILED")

        logger.error("発注失敗: %s — 全口座区分で失敗", order)
        return OrderResult(order_id="", status="FAILED")

    def cancel_order(self, order_id: str) -> bool:
        """注文をキャンセルする."""
        assert self._trade_ctx is not None
        ret, data = self._trade_ctx.modify_order(
            modify_order_op=ModifyOrderOp.CANCEL,
            order_id=order_id,
            qty=0,
            price=0,
            trd_env=self._trd_env,
        )
        if ret != RET_OK:
            logger.warning("キャンセル失敗: order_id=%s — %s", order_id, data)
            return False
        logger.info("キャンセル成功: order_id=%s", order_id)
        return True
