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
    AuType,
    KLType,
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
    # 拡張: 高値掴み判別用の追加フィールド
    open_price: float = 0.0          # 当日始値 (ET 9:30)
    high_price: float = 0.0          # 当日高値
    low_price: float = 0.0           # 当日安値
    prev_close: float = 0.0          # 前日終値
    avg_price: float = 0.0           # moomoo 算出の本物のVWAP
    amplitude: float = 0.0           # 当日値幅率 (%)
    pre_change_rate: float = 0.0     # プレマーケット変化率 (%)
    after_change_rate: float = 0.0   # アフターマーケット変化率 (%)
    volume_ratio: float = 0.0        # 普段との出来高比

    # ----- 派生指標（snapshot から計算可能） -----

    @property
    def change_from_open_pct(self) -> float | None:
        """寄りからの変化率 (%). 高値掴み判別の核心指標."""
        if self.open_price <= 0:
            return None
        return (self.last_price - self.open_price) / self.open_price * 100

    @property
    def gap_pct(self) -> float | None:
        """前日終値→当日始値のギャップ率 (%)."""
        if self.prev_close <= 0 or self.open_price <= 0:
            return None
        return (self.open_price - self.prev_close) / self.prev_close * 100

    @property
    def price_position_in_range(self) -> float | None:
        """当日レンジ内位置 (0=安値、1=高値). 高値掴みの直接指標."""
        if self.high_price <= 0 or self.low_price <= 0:
            return None
        rng = self.high_price - self.low_price
        if rng <= 0:
            return None
        return (self.last_price - self.low_price) / rng

    @property
    def best_vwap(self) -> float:
        """利用可能な最良のVWAP値. avg_price (公式) → turnover/volume (近似) の順."""
        if self.avg_price > 0:
            return self.avg_price
        if self.volume > 0 and self.turnover > 0:
            return self.turnover / self.volume
        return 0.0


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
        # 信用取引対応: CASH と MARGIN それぞれの acc_id を保持
        # connect() 時に get_acc_list() で取得
        self._cash_acc_id: int = 0      # 現物口座
        self._margin_acc_id: int = 0    # 信用口座 (未開設なら 0)

    @property
    def is_connected(self) -> bool:
        """接続済みかどうか."""
        return self._connected

    # ------------------------------------------------------------------
    # 接続・切断
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 10.0) -> None:
        """moomoo OpenD に接続する.

        Args:
            timeout: 接続タイムアウト（秒）

        Raises:
            ConnectionError: OpenD が起動していないか接続できない場合
        """
        import socket

        # OpenD のポートに接続できるか事前チェック
        logger.info(
            "OpenD 接続チェック: %s:%d",
            settings.MOOMOO_HOST, settings.MOOMOO_PORT,
        )
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            result = sock.connect_ex((settings.MOOMOO_HOST, settings.MOOMOO_PORT))
            if result != 0:
                raise ConnectionError(
                    f"OpenD に接続できません ({settings.MOOMOO_HOST}:{settings.MOOMOO_PORT})。"
                    f" OpenD が起動しているか確認してください。"
                )
        finally:
            sock.close()

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

        # 口座一覧を取得して CASH / MARGIN それぞれの acc_id を保持
        # 信用買い (USE_MARGIN_LONG=true) 時は MARGIN acc_id を発注で指定する
        try:
            ret, acc_data = self._trade_ctx.get_acc_list()
            if ret == RET_OK and acc_data is not None and not acc_data.empty:
                target_env = "REAL" if self._trd_env == TrdEnv.REAL else "SIMULATE"
                for _, row in acc_data.iterrows():
                    if str(row.get("trd_env", "")) != target_env:
                        continue
                    acc_id = int(row.get("acc_id", 0))
                    acc_type = str(row.get("acc_type", ""))
                    if acc_type == "CASH" and self._cash_acc_id == 0:
                        self._cash_acc_id = acc_id
                    elif acc_type == "MARGIN" and self._margin_acc_id == 0:
                        self._margin_acc_id = acc_id
                logger.info(
                    "acc_list: CASH=%d MARGIN=%d (env=%s)",
                    self._cash_acc_id, self._margin_acc_id, target_env,
                )
            else:
                logger.warning("get_acc_list 失敗 (ret=%s) — acc_id 未取得", ret)
        except Exception:
            logger.exception("get_acc_list 取得エラー — acc_id 未取得 (デフォルト発注に fallback)")

        # USE_MARGIN_LONG が有効なのに MARGIN acc_id が取れない場合は警告
        if settings.USE_MARGIN_LONG and self._margin_acc_id == 0:
            logger.error(
                "USE_MARGIN_LONG=true だが MARGIN 口座 acc_id が取得できません。"
                " 信用口座が開設されているか確認してください。 現物口座にフォールバックします"
            )

        self._connected = True
        logger.info(
            "moomoo OpenAPI 接続完了 (host=%s:%d env=%s USE_MARGIN_LONG=%s active_acc=%d)",
            settings.MOOMOO_HOST, settings.MOOMOO_PORT, settings.TRADE_ENV,
            settings.USE_MARGIN_LONG, self._get_active_acc_id(),
        )

    def _get_active_acc_id(self) -> int:
        """USE_MARGIN_LONG 設定に応じて使用する acc_id を返す.

        信用買い時は MARGIN acc_id、 通常は CASH acc_id。
        いずれも 0 (未取得) なら 0 を返す (futu SDK では 0 = 既定アカウント)。
        """
        if settings.USE_MARGIN_LONG and self._margin_acc_id > 0:
            return self._margin_acc_id
        return self._cash_acc_id

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

        def _safe_float(key: str, default: float = 0.0) -> float:
            try:
                v = float(row.get(key, default))
                # NaN ガード
                if v != v:
                    return default
                return v
            except (TypeError, ValueError):
                return default

        return QuoteSnapshot(
            symbol=symbol,
            last_price=_safe_float("last_price"),
            volume=_safe_float("volume"),
            turnover=_safe_float("turnover"),
            open_price=_safe_float("open_price"),
            high_price=_safe_float("high_price"),
            low_price=_safe_float("low_price"),
            prev_close=_safe_float("prev_close_price"),
            avg_price=_safe_float("avg_price"),
            amplitude=_safe_float("amplitude"),
            pre_change_rate=_safe_float("pre_change_rate"),
            after_change_rate=_safe_float("after_change_rate"),
            volume_ratio=_safe_float("volume_ratio"),
        )

    def get_snapshots(self, codes: list[str]) -> dict[str, "QuoteSnapshot | None"]:
        """複数銘柄のスナップショットを一括取得する.

        Args:
            codes: ['US.AAPL', 'US.NVDA', ...] 形式のコードリスト

        Returns:
            {symbol: QuoteSnapshot or None} の辞書 (取得失敗銘柄は None)
        """
        assert self._quote_ctx is not None
        if not codes:
            return {}
        import time as _t
        _api_start = _t.monotonic()
        ret, data = self._quote_ctx.get_market_snapshot(codes)
        _api_elapsed = _t.monotonic() - _api_start
        if _api_elapsed > 3.0:
            logger.warning(
                "get_market_snapshot 遅延: %.2fs (ret=%s, codes=%d)",
                _api_elapsed, ret, len(codes),
            )
        if ret != RET_OK or data is None or data.empty:
            logger.debug("Batch snapshot unavailable: ret=%s codes=%d", ret, len(codes))
            return {}

        def _safe_float(row, key: str, default: float = 0.0) -> float:
            try:
                v = float(row.get(key, default))
                if v != v:  # NaN
                    return default
                return v
            except (TypeError, ValueError):
                return default

        result: dict[str, QuoteSnapshot | None] = {}
        for _, row in data.iterrows():
            code = row.get("code", "")
            symbol = code.replace("US.", "") if code else ""
            if not symbol:
                continue
            try:
                result[symbol] = QuoteSnapshot(
                    symbol=symbol,
                    last_price=_safe_float(row, "last_price"),
                    volume=_safe_float(row, "volume"),
                    turnover=_safe_float(row, "turnover"),
                    open_price=_safe_float(row, "open_price"),
                    high_price=_safe_float(row, "high_price"),
                    low_price=_safe_float(row, "low_price"),
                    prev_close=_safe_float(row, "prev_close_price"),
                    avg_price=_safe_float(row, "avg_price"),
                    amplitude=_safe_float(row, "amplitude"),
                    pre_change_rate=_safe_float(row, "pre_change_rate"),
                    after_change_rate=_safe_float(row, "after_change_rate"),
                    volume_ratio=_safe_float(row, "volume_ratio"),
                )
            except Exception:
                logger.exception("get_snapshots: parse error for %s", symbol)
                result[symbol] = None
        return result

    # ------------------------------------------------------------------
    # K線データ（ATR 計算用）
    # ------------------------------------------------------------------

    def get_kline(self, symbol: str, num: int = 30) -> "pd.DataFrame | None":
        """過去の確定日足K線データを取得する.

        request_history_kl() を使用するため、サブスクリプション不要で
        市場オープン前でも確定済みのデータを取得できる。

        Args:
            symbol: 銘柄シンボル
            num: 取得本数（デフォルト30）

        Returns:
            high, low, close 列を含む DataFrame（取得失敗時は None）
        """
        from datetime import date as _date, timedelta as _td
        assert self._quote_ctx is not None
        code = f"US.{symbol}"
        end_date = _date.today().strftime("%Y-%m-%d")
        start_date = (_date.today() - _td(days=num * 2)).strftime("%Y-%m-%d")
        try:
            ret, data, _ = self._quote_ctx.request_history_kline(
                code,
                ktype=KLType.K_DAY,
                start=start_date,
                end=end_date,
                autype=AuType.QFQ,
                max_count=num,
            )
            if ret != RET_OK or data is None or data.empty:
                logger.debug("K線データ取得失敗: %s (ret=%s)", symbol, ret)
                return None
            required = {"high", "low", "close"}
            if not required.issubset(data.columns):
                logger.debug("K線データに必要な列がありません: %s", symbol)
                return None
            return data[["high", "low", "close"]].tail(num).copy()
        except Exception:
            logger.exception("K線データ取得エラー: %s", symbol)
            return None

    def get_spy_intraday_change(self) -> float | None:
        """SPY の当日始値からの変化率を返す.

        Returns:
            変化率（例: -0.005 = -0.5%）。取得失敗時は None。
        """
        indices = self.get_market_indices()
        return indices.get("spy")

    def get_market_indices(self) -> dict[str, float | None]:
        """SPY / QQQ の変化率を 2 基準で一括取得する.

        snapshot ベースのため購読不要 (subscribe 済みでなくても動作).

        2 つの基準を両方返す (IF 分析の連続性のため):
          - spy / qqq: **前日終値からの変化率** (= 一般的な「日次変化率」、 .SPX/Yahoo と同じ)
              SPY フィルタの判定はこちらを使う (6/11 修正)。
          - spy_open / qqq_open: **当日始値からの変化率** (= 6/10 までの旧基準)
              当日寄付き後のトレンド (寄り高失速 vs 寄り安戻し) 検知用、 過去 cohort と整合。

        修正履歴 (6/11): 旧 get は spy_open のみ返していた。 gap-down で始まる日
        (例: 6/10 ET = SPY 前日比 -1.6%) に「open から +0.3%」 と誤認する問題があり、
        prev_close 基準を主指標に、 open 基準を補助指標に変更。

        Returns:
            {'spy': -0.016, 'qqq': -0.020, 'spy_open': +0.003, 'qqq_open': +0.005}
            のような辞書。 取得不可ならその値は None。
        """
        import time as _t
        _start = _t.monotonic()
        snaps = self.get_snapshots(["US.SPY", "US.QQQ"])
        _snap_elapsed = _t.monotonic() - _start
        if _snap_elapsed > 2.0:
            logger.warning(
                "get_market_indices: get_snapshots(SPY/QQQ) 遅延 %.2fs",
                _snap_elapsed,
            )
        result: dict[str, float | None] = {
            "spy": None, "qqq": None,
            "spy_open": None, "qqq_open": None,
        }
        for sym, snap in snaps.items():
            if snap is None or snap.last_price <= 0:
                continue
            key = sym.lower()  # us.spy / us.qqq → spy / qqq
            if snap.prev_close > 0:
                result[key] = (snap.last_price - snap.prev_close) / snap.prev_close
            if snap.open_price > 0:
                result[f"{key}_open"] = (snap.last_price - snap.open_price) / snap.open_price
        return result

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
        """口座の現金余力（buying power）を取得する.

        ポジションサイズ計算に使用。ポジション評価額を含まない。
        """
        info = self._query_account_info()
        power = float(info.get("power", 0) or 0)
        if power > 0:
            return power
        cash = float(info.get("cash", 0) or 0)
        if cash > 0:
            return cash
        return float(info.get("total_assets", 0) or 0)

    def get_total_assets(self) -> float:
        """口座の総資産（現金 + ポジション評価額）を取得する.

        ドローダウン計算に使用。

        信用取引時の特殊対応:
          - 信用口座の accinfo_query は total_assets が現金部分のみで、
            株式時価 (market_val) が反映されないラグがある
          - また cash 口座にも資産 (現物保有株) が存在する
          - そのため両口座の total_assets を合算し、 信用口座のポジションの
            時価を別途加算して、 真の純資産を算出する
        """
        assert self._trade_ctx is not None
        total = 0.0
        for acc_id in [self._cash_acc_id, self._margin_acc_id]:
            if acc_id <= 0:
                continue
            try:
                ret, data = self._trade_ctx.accinfo_query(
                    trd_env=self._trd_env, currency="USD", acc_id=acc_id
                )
                if ret == RET_OK and not data.empty:
                    row = data.iloc[0]
                    total += float(row.get("total_assets", 0) or 0)
            except Exception as exc:
                logger.warning("acc_id=%s の accinfo_query 失敗: %s", acc_id, exc)
        # 信用口座のポジションは total_assets に market_val が含まれない場合があるため
        # position_list_query から保有株式のコスト基準額を加算して補正する
        if self._margin_acc_id > 0:
            try:
                ret, pos_data = self._trade_ctx.position_list_query(
                    trd_env=self._trd_env, acc_id=self._margin_acc_id
                )
                if ret == RET_OK and not pos_data.empty:
                    for _, row in pos_data.iterrows():
                        qty = float(row.get("qty", 0) or 0)
                        cost = float(row.get("cost_price", 0) or 0)
                        market_val = float(row.get("market_val", 0) or 0)
                        # market_val が取得できればそれを、 だめなら qty * cost を使う
                        if market_val > 0:
                            total += market_val
                        elif qty > 0 and cost > 0:
                            total += qty * cost
            except Exception as exc:
                logger.warning("信用口座 position 取得失敗: %s", exc)
        return total

    def _query_account_info(self) -> dict:
        """accinfo_query の結果を dict で返す."""
        assert self._trade_ctx is not None
        kwargs = {"trd_env": self._trd_env, "currency": "USD"}
        acc_id = self._get_active_acc_id()
        if acc_id:
            kwargs["acc_id"] = acc_id
        ret, data = self._trade_ctx.accinfo_query(**kwargs)
        if ret != RET_OK or data.empty:
            logger.warning("口座残高取得失敗")
            return {}
        return dict(data.iloc[0])

    # ------------------------------------------------------------------
    # ポジション照会
    # ------------------------------------------------------------------

    def get_positions(self) -> dict[str, dict]:
        """ペーパー/本番口座の保有ポジションを取得する.

        moomoo の position_list_query は以下のレコードも返す:
          - 当日決済済みの建玉 (qty=0)
          - 信用買いで複数回エントリーした際の個別建玉 (同銘柄で複数 position_id)
        これらを単純に dict[symbol] に格納すると以下の問題が発生する:
          - 最後の行が qty=0 だと「保有なし」と誤判定されて重複建てが起きる
          - 同銘柄複数建玉時に最後の 1 件しか認識されない

        対策:
          - qty>0 のレコードのみ採用 (決済済みを除外)
          - 同銘柄の複数 position_id は加重平均で集約

        Returns:
            {symbol: {"qty": float, "cost_price": float, "market_val": float, "pl_val": float, "position_id": str}}
        """
        assert self._trade_ctx is not None
        kwargs = {"trd_env": self._trd_env}
        acc_id = self._get_active_acc_id()
        if acc_id:
            kwargs["acc_id"] = acc_id
        ret, data = self._trade_ctx.position_list_query(**kwargs)
        if ret != RET_OK or data.empty:
            return {}
        aggregates: dict[str, list[dict]] = {}
        for _, row in data.iterrows():
            qty = float(row["qty"])
            if qty <= 0:
                continue
            code = str(row["code"])  # "US.NVDA"
            symbol = code.replace("US.", "")
            aggregates.setdefault(symbol, []).append({
                "qty": qty,
                "cost_price": float(row["cost_price"]),
                "market_val": float(row.get("market_val", 0)),
                "pl_val": float(row.get("pl_val", 0)),
                "position_id": str(row.get("position_id", "")),
            })
        result: dict[str, dict] = {}
        for symbol, pos_list in aggregates.items():
            total_qty = sum(p["qty"] for p in pos_list)
            # 加重平均コスト価格
            weighted_cost = sum(p["qty"] * p["cost_price"] for p in pos_list)
            avg_cost = weighted_cost / total_qty if total_qty > 0 else 0.0
            total_market_val = sum(p["market_val"] for p in pos_list)
            total_pl = sum(p["pl_val"] for p in pos_list)
            # position_id は qty 最大のものを代表として使用
            primary = max(pos_list, key=lambda p: p["qty"])
            result[symbol] = {
                "qty": total_qty,
                "cost_price": avg_cost,
                "market_val": total_market_val,
                "pl_val": total_pl,
                "position_id": primary["position_id"],
                "position_count": len(pos_list),
            }
            if len(pos_list) > 1:
                logger.info(
                    "[%s] 複数建玉を集約: %d件 → total_qty=%.0f avg_cost=$%.2f",
                    symbol, len(pos_list), total_qty, avg_cost,
                )
        return result

    def has_position(self, symbol: str) -> bool:
        """指定銘柄のポジションを保有しているか."""
        positions = self.get_positions()
        return symbol in positions and positions[symbol]["qty"] > 0

    def get_order_status(self, order_id: str) -> str:
        """指定 order_id の注文ステータスを取得する."""
        assert self._trade_ctx is not None
        kwargs = {"order_id": order_id, "trd_env": self._trd_env}
        acc_id = self._get_active_acc_id()
        if acc_id:
            kwargs["acc_id"] = acc_id
        ret, data = self._trade_ctx.order_list_query(**kwargs)
        if ret != RET_OK or data.empty:
            return "UNKNOWN"
        return str(data.iloc[0].get("order_status", "UNKNOWN"))

    # ------------------------------------------------------------------
    # 信用取引（空売り）情報
    # ------------------------------------------------------------------

    def get_margin_balance(self) -> dict[str, float]:
        """信用口座の残高・空売り余力を返す.

        信用口座が開設されていれば MARGIN acc_id で問い合わせる。
        未開設なら CASH acc_id (現物口座) の値を返す。

        Returns:
            {"power": float, "max_power_short": float, "cash": float, ...}
        """
        assert self._trade_ctx is not None
        kwargs = {"trd_env": self._trd_env, "currency": "USD"}
        # 信用残高は必ず MARGIN acc_id (あれば) を使う
        if self._margin_acc_id > 0:
            kwargs["acc_id"] = self._margin_acc_id
        elif self._cash_acc_id > 0:
            kwargs["acc_id"] = self._cash_acc_id
        ret, data = self._trade_ctx.accinfo_query(**kwargs)
        if ret != RET_OK or data.empty:
            logger.warning("信用口座残高取得失敗")
            return {"power": 0.0, "max_power_short": 0.0, "cash": 0.0}
        row = data.iloc[0]
        return {
            "power": float(row.get("power", 0) or 0),
            "max_power_short": float(row.get("max_power_short", 0) or 0),
            "cash": float(row.get("cash", 0) or 0),
            "total_assets": float(row.get("total_assets", 0) or 0),
            "market_val": float(row.get("market_val", 0) or 0),
        }

    def get_max_short_qty(self, symbol: str, price: float) -> int:
        """銘柄ごとの空売り可能株数を返す.

        Args:
            symbol: 銘柄シンボル
            price: 現在の株価

        Returns:
            空売り可能株数（取得失敗時は0）
        """
        assert self._trade_ctx is not None
        code = f"US.{symbol}"
        try:
            kwargs = {
                "order_type": OrderType.MARKET,
                "code": code,
                "price": price,
                "trd_env": self._trd_env,
            }
            # 空売り情報は MARGIN acc_id (あれば) で
            if self._margin_acc_id > 0:
                kwargs["acc_id"] = self._margin_acc_id
            ret, data = self._trade_ctx.acctradinginfo_query(**kwargs)
            if ret != RET_OK or data.empty:
                logger.debug("空売り可能株数取得失敗: %s", symbol)
                return 0
            max_sell_short = int(data.iloc[0].get("max_sell_short", 0) or 0)
            logger.info("[%s] 空売り可能株数: %d", symbol, max_sell_short)
            return max_sell_short
        except Exception:
            logger.exception("空売り可能株数取得エラー: %s", symbol)
            return 0

    # ------------------------------------------------------------------
    # 発注
    # ------------------------------------------------------------------

    def _get_jp_acc_type(self) -> Any:
        """settings.JP_ACC_TYPE から SubAccType を返す."""
        if settings.JP_ACC_TYPE == "SPECIFIC":
            return SubAccType.JP_TOKUTEI
        return SubAccType.JP_GENERAL

    def place_order(self, order: Order) -> OrderResult:
        """注文を発注する.

        BUY: 設定の口座区分 (JP_TOKUTEI) で発注
        SELL: JP_TOKUTEI → 省略(デフォルト) → JP_GENERAL の順でリトライ
              （特定口座・一般口座どちらのポジションも売れるようにする）
        """
        assert self._trade_ctx is not None
        code = f"US.{order.symbol}"
        side = TrdSide.BUY if order.side == "BUY" else TrdSide.SELL
        order_type = OrderType.MARKET if order.price is None else OrderType.NORMAL
        price = order.price or 0.0

        base_kwargs: dict = dict(
            price=price,
            qty=order.quantity,
            code=code,
            trd_side=side,
            order_type=order_type,
            trd_env=self._trd_env,
        )
        # 信用買い (USE_MARGIN_LONG=true) なら MARGIN acc_id、 そうでなければ CASH acc_id
        active_acc_id = self._get_active_acc_id()
        if active_acc_id:
            base_kwargs["acc_id"] = active_acc_id

        if order.side == "BUY":
            # BUY は設定の口座区分で1回だけ
            attempts = [("BUY", {**base_kwargs, "jp_acc_type": self._get_jp_acc_type()})]
        else:
            # SELL はリトライ: 特定口座 → 省略(デフォルト) → 一般口座
            attempts = [
                ("SELL/TOKUTEI", {**base_kwargs, "jp_acc_type": SubAccType.JP_TOKUTEI}),
                ("SELL/DEFAULT", {**base_kwargs}),  # jp_acc_type 省略
                ("SELL/GENERAL", {**base_kwargs, "jp_acc_type": SubAccType.JP_GENERAL}),
            ]

        for label, kwargs in attempts:
            ret, data = self._trade_ctx.place_order(**kwargs)
            if ret == RET_OK and not (hasattr(data, "empty") and data.empty):
                order_id = str(data["order_id"].iloc[0])
                logger.info("発注成功: %s order_id=%s (%s)", order, order_id, label)
                return OrderResult(order_id=order_id, status="SUBMITTED")

            error_msg = str(data)
            if "Insufficient positions" in error_msg and label != attempts[-1][0]:
                logger.info("[%s] %s: Insufficient positions — next acc_type", order.symbol, label)
                continue

            logger.error("発注失敗: %s (%s) — %s", order, label, data)
            return OrderResult(order_id="", status="FAILED")

        logger.error("発注失敗: %s — 全口座区分で失敗", order)
        return OrderResult(order_id="", status="FAILED")

    def cancel_order(self, order_id: str) -> bool:
        """注文をキャンセルする."""
        assert self._trade_ctx is not None
        kwargs = {
            "modify_order_op": ModifyOrderOp.CANCEL,
            "order_id": order_id,
            "qty": 0,
            "price": 0,
            "trd_env": self._trd_env,
        }
        acc_id = self._get_active_acc_id()
        if acc_id:
            kwargs["acc_id"] = acc_id
        ret, data = self._trade_ctx.modify_order(**kwargs)
        if ret != RET_OK:
            logger.warning("キャンセル失敗: order_id=%s — %s", order_id, data)
            return False
        logger.info("キャンセル成功: order_id=%s", order_id)
        return True
