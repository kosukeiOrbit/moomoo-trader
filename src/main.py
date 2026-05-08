"""エントリーポイント・メインループ.

全モジュールを統合し、市場オープン中にデイトレードを自動実行する。
05:50 JST (= 15:50 ET) に未決済ポジションを全決済して安全に終了する。
Ctrl+C で安全にシャットダウンできる。
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import logging.handlers
import os
import signal
import sys
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Ensure project root is in sys.path (for Task Scheduler / direct execution)
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import settings
from src.data.moomoo_client import MoomooClient
from src.data.board_scraper import BoardScraper
from src.data.news_feed import NewsFeed
from src.signals.sentiment_analyzer import SentimentAnalyzer
from src.signals.flow_detector import FlowDetector
from src.signals.and_filter import AndFilter
from src.risk.position_sizer import PositionSizer, TradeResult
from src.risk.stop_loss import StopLossManager
from src.risk.circuit_breaker import CircuitBreaker, AccountState, BreakerAction
from src.execution.order_router import OrderRouter, ExitResult
from src.monitor.pnl_tracker import PnLTracker
from src.monitor.notifier import Notifier


def _setup_logging() -> None:
    """Console + daily rotating file logging."""
    log_dir = Path(_project_root) / "logs"
    log_dir.mkdir(exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)

    # File handler (daily rotation: logs/bot_YYYYMMDD.log)
    today = datetime.now().strftime("%Y%m%d")
    file_handler = logging.FileHandler(
        log_dir / f"bot_{today}.log",
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file_handler)


_setup_logging()
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
JST = ZoneInfo("Asia/Tokyo")

# 市場時間
MARKET_OPEN = time(9, 30)   # ET
MARKET_CLOSE = time(16, 0)  # ET

# 全ポジション強制決済時刻 (ET 15:50 = 市場クローズ10分前)
# ET ベースで判定するため DST/EST を自動処理する
FORCE_EXIT_ET = time(15, 50)


# ---------------------------------------------------------------------------
# 市場判定
# ---------------------------------------------------------------------------

def _is_nyse_trading_day(d: date = None) -> bool:
    """NYSE の営業日かどうか（休場日を考慮）."""
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        if d is None:
            d = datetime.now(ET).date()
        schedule = nyse.schedule(
            start_date=d.strftime("%Y-%m-%d"),
            end_date=d.strftime("%Y-%m-%d"),
        )
        return not schedule.empty
    except ImportError:
        # フォールバック: 土日のみ判定
        if d is None:
            d = datetime.now(ET).date()
        return d.weekday() < 5
    except Exception:
        if d is None:
            d = datetime.now(ET).date()
        return d.weekday() < 5


def market_is_open() -> bool:
    """米国市場がオープンしているか判定する.

    NYSE 休場日・土日・時間外はFalseを返す。
    """
    now = datetime.now(ET)
    if not _is_nyse_trading_day(now.date()):
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def is_market_open_skip() -> bool:
    """寄り付き後のスキップ期間中かどうか."""
    skip_min = settings.MARKET_OPEN_SKIP_MINUTES
    if skip_min <= 0:
        return False
    now_et = datetime.now(ET)
    market_open_dt = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    skip_until = market_open_dt + timedelta(minutes=skip_min)
    return market_open_dt <= now_et < skip_until


def should_force_exit() -> bool:
    """全ポジション強制決済の時刻かどうか (ET 15:50).

    ET ベースで判定するため DST/EST を自動処理する。
    """
    now_et = datetime.now(ET)
    return now_et.time() >= FORCE_EXIT_ET


# ---------------------------------------------------------------------------
# シャットダウンフラグ
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _handle_shutdown(signum: int, frame: object) -> None:
    """Ctrl+C / SIGTERM を受けてフラグを立てる."""
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("シャットダウン要求を受信 (signal=%d)", signum)


# ---------------------------------------------------------------------------
# SHORT ドライラン
# ---------------------------------------------------------------------------

_dryrun_entered: dict[str, str] = {}
_DRYRUN_PATH = Path(_project_root) / "data" / "short_dryrun.jsonl"


async def _short_dryrun(
    symbol: str,
    flow_strength: float,
    board_scraper,
    news_feed,
    sentiment_analyzer,
    client,
    stop_loss,
    order_router=None,
) -> None:
    """SHORT ドライラン: 発注せず仮想PnLをJSONLに記録する.

    条件A（個別悪材料）: sentiment < -0.3 AND confidence > 0.7
    条件B（マクロ連動）: SPY前日比 < -0.5% AND flow=SELL強
    """
    try:
        today = date.today().isoformat()
        if _dryrun_entered.get(symbol) == today:
            return

        # テキスト収集（条件A/B 共通で使用）
        posts = await board_scraper.fetch_posts(symbol)
        news_articles = await news_feed.get_latest(symbol)
        texts = [p.text for p in posts] + [
            f"{a.title} {a.body}" for a in news_articles
        ]

        # sentiment 取得（テキストがあれば）
        score = 0.0
        confidence = 0.0
        filtered_count = len([t for t in texts if t.strip()])
        if filtered_count >= settings.MIN_TEXTS_FOR_ANALYSIS:
            sentiment = sentiment_analyzer.analyze(texts, symbol)
            score = sentiment.score
            confidence = sentiment.confidence

        # 条件A: 個別悪材料ショート
        individual_short = (
            score < settings.SHORT_SENTIMENT_THRESHOLD
            and confidence > settings.CONFIDENCE_MIN
        )

        # 条件B: マクロ連動ショート（当日始値からの SPY 変化率で判定）
        spy_rt = client.get_spy_intraday_change()
        macro_short = (
            spy_rt is not None
            and spy_rt < -0.003  # SPY 当日始値から -0.3% 以下
            and flow_strength > settings.FLOW_BUY_THRESHOLD
        )

        if not (individual_short or macro_short):
            return

        if individual_short and macro_short:
            pattern = "both"
        elif individual_short:
            pattern = "individual"
        else:
            pattern = "macro"

        individual_would_trigger = individual_short if pattern == "macro" else None

        snap = client.get_snapshot(symbol)
        if snap is None or snap.last_price <= 0:
            return
        entry_price = snap.last_price

        # VWAP は snapshot の avg_price を優先 (フォールバックで turnover/volume)
        vwap_value = snap.best_vwap or None
        vwap_above = entry_price > vwap_value if vwap_value else None

        kline = client.get_kline(symbol)
        atr_pct = stop_loss.calc_atr_pct(kline, entry_price)
        sl_price = entry_price * (1 + atr_pct * settings.ATR_SL_MULTIPLIER)
        tp_price = entry_price * (1 - atr_pct * settings.ATR_TP_MULTIPLIER)

        # 銘柄の当日騰落率: snapshot の prev_close 優先、フォールバックで kline
        prev_close_for_calc = snap.prev_close
        if prev_close_for_calc <= 0 and kline is not None and len(kline) >= 1:
            prev_close_for_calc = float(kline["close"].iloc[-1])
        symbol_change_pct = None
        if prev_close_for_calc > 0:
            symbol_change_pct = (entry_price - prev_close_for_calc) / prev_close_for_calc

        # IF分析用: would_pass_short_filter_v1
        # F1+F2: VWAP上 AND (symChg<0 OR symChg>5%)
        sym_chg_pct = (symbol_change_pct * 100) if symbol_change_pct is not None else None
        f1_pass = (vwap_above is True)  # VWAP上のみ
        f2_pass = (sym_chg_pct is not None and (sym_chg_pct < 0 or sym_chg_pct > 5))
        would_pass = f1_pass and f2_pass

        # スロット負荷: 信号発生時の LONG ポジション数
        slot_load = order_router.long_count if order_router is not None else None

        # ET 市場開始からの経過秒数 (9:30 ET = 22:30 JST DST)
        try:
            from zoneinfo import ZoneInfo as _ZI
            now_et = datetime.now(_ZI("America/New_York"))
            mkt_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            sec_since_open = int((now_et - mkt_open).total_seconds())
        except Exception:
            sec_since_open = None

        _dryrun_entered[symbol] = today
        record = {
            "date": today,
            "symbol": symbol,
            "pattern": pattern,
            "entry_time": datetime.now().strftime("%H:%M:%S"),
            "entry_price": round(entry_price, 4),
            "vwap": round(vwap_value, 4) if vwap_value else None,
            "vwap_above": vwap_above,
            "sl_price": round(sl_price, 4),
            "tp_price": round(tp_price, 4),
            "score": round(score, 3),
            "confidence": round(confidence, 3),
            "flow_strength": round(flow_strength, 3),
            "spy_change_realtime": round(spy_rt * 100, 2) if spy_rt is not None else None,
            "symbol_change_pct": round(symbol_change_pct * 100, 2) if symbol_change_pct is not None else None,
            "individual_would_trigger": individual_would_trigger,
            # 高値掴み判別用フィールド
            "open_price": round(snap.open_price, 4) if snap.open_price > 0 else None,
            "high_price": round(snap.high_price, 4) if snap.high_price > 0 else None,
            "low_price": round(snap.low_price, 4) if snap.low_price > 0 else None,
            "prev_close": round(snap.prev_close, 4) if snap.prev_close > 0 else None,
            "change_from_open_pct": round(snap.change_from_open_pct, 3) if snap.change_from_open_pct is not None else None,
            "gap_pct": round(snap.gap_pct, 3) if snap.gap_pct is not None else None,
            "price_position_in_range": round(snap.price_position_in_range, 3) if snap.price_position_in_range is not None else None,
            "amplitude": round(snap.amplitude, 3) if snap.amplitude > 0 else None,
            "pre_change_rate": round(snap.pre_change_rate, 3),
            "volume_ratio": round(snap.volume_ratio, 3) if snap.volume_ratio > 0 else None,
            # IF分析用フィールド
            "would_pass_short_filter_v1": would_pass,
            "filter_v1_reason": (
                "passed" if would_pass
                else (
                    "F1_fail_vwap_below" if not f1_pass
                    else "F2_fail_symchg_0to5"
                )
            ),
            "slot_load_at_signal": slot_load,
            "seconds_since_market_open": sec_since_open,
            "close_price": None,
            "exit_reason": None,
            "virtual_pnl": None,
        }
        _DRYRUN_PATH.parent.mkdir(exist_ok=True)
        with open(_DRYRUN_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps(record) + "\n")

        spy_str = f" spy_rt={spy_rt*100:.2f}%" if spy_rt is not None else ""
        logger.info(
            "[DRY-RUN SHORT/%s] %s entry=%.2f SL=%.2f TP=%.2f "
            "score=%.3f conf=%.3f flow=%.3f%s",
            pattern, symbol, entry_price, sl_price, tp_price,
            score, confidence, flow_strength, spy_str,
        )

    except Exception:
        logger.warning("[DRY-RUN SHORT] %s エラー（無視）", symbol, exc_info=True)


async def _short_dryrun_close(client) -> None:
    """SHORT ドライラン仮想決済: 未決済のエントリーを現在価格でクローズ."""
    try:
        if not _DRYRUN_PATH.exists():
            return

        records: list[dict] = []
        with open(_DRYRUN_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(_json.loads(line))

        updated = False
        for rec in records:
            # 未決済レコードを全て処理（日付に関係なく）
            if rec.get("close_price") is not None:
                continue
            snap = client.get_snapshot(rec["symbol"])
            if snap is None or snap.last_price <= 0:
                continue
            close_price = snap.last_price
            close_time = datetime.now().strftime("%H:%M:%S")

            if close_price >= rec["sl_price"]:
                exit_reason = "SL"
                pnl = rec["entry_price"] - rec["sl_price"]
            elif close_price <= rec["tp_price"]:
                exit_reason = "TP"
                pnl = rec["entry_price"] - rec["tp_price"]
            else:
                exit_reason = "FORCE_CLOSE"
                pnl = rec["entry_price"] - close_price

            rec["close_price"] = round(close_price, 4)
            rec["close_time"] = close_time
            rec["virtual_pnl"] = round(pnl, 4)
            rec["exit_reason"] = exit_reason
            updated = True

            logger.info(
                "[DRY-RUN SHORT CLOSE] %s entry=%.2f close=%.2f "
                "pnl=%+.4f reason=%s",
                rec["symbol"], rec["entry_price"],
                close_price, pnl, exit_reason,
            )

        if updated:
            with open(_DRYRUN_PATH, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(_json.dumps(rec) + "\n")

    except Exception:
        logger.warning("[DRY-RUN SHORT CLOSE] エラー（無視）", exc_info=True)


# ---------------------------------------------------------------------------
# LONG スキップ期間 ドライラン (IF分析用)
# ---------------------------------------------------------------------------

_long_skip_dryrun_recorded: dict[str, str] = {}
_LONG_SKIP_DRYRUN_PATH = Path(_project_root) / "data" / "long_skip_dryrun.jsonl"

_long_full_dryrun_recorded: dict[str, str] = {}
_LONG_FULL_DRYRUN_PATH = Path(_project_root) / "data" / "long_full_dryrun.jsonl"


async def _long_dryrun_record(
    symbol: str,
    sentiment,
    flow,
    snap,
    vwap_approx: float | None,
    vwap_above: bool | None,
    levels,
    kline,
    stop_loss,
    client,
    texts_count: int,
    tight_pass: bool = True,
    tight_reason: str = "",
    dryrun_type: str = "skip",  # "skip" or "full"
    slot_count_at_signal: int | None = None,
) -> None:
    """LONG エントリー条件成立を JSONL に記録する (実発注なし).

    dryrun_type:
      - "skip": スキップ期間中 (22:30-23:30) のシグナル → long_skip_dryrun.jsonl
      - "full": 5枠フル時のシグナル → long_full_dryrun.jsonl

    同一銘柄は1セッション1回のみ記録（最初に条件成立した時点）。
    """
    try:
        today = date.today().isoformat()
        if dryrun_type == "skip":
            recorded_dict = _long_skip_dryrun_recorded
            output_path = _LONG_SKIP_DRYRUN_PATH
        else:  # "full"
            recorded_dict = _long_full_dryrun_recorded
            output_path = _LONG_FULL_DRYRUN_PATH

        if recorded_dict.get(symbol) == today:
            return

        entry_price = snap.last_price
        if entry_price <= 0:
            return

        atr_pct = stop_loss.calc_atr_pct(kline, entry_price)
        sl_price = levels.stop_loss if levels else entry_price * (1 - atr_pct * settings.ATR_SL_MULTIPLIER)
        tp_price = levels.take_profit if levels else entry_price * (1 + atr_pct * settings.ATR_TP_MULTIPLIER)

        # 銘柄の当日騰落率: snapshot の prev_close 優先、フォールバックで kline
        prev_close_for_calc = snap.prev_close
        if prev_close_for_calc <= 0 and kline is not None and len(kline) >= 1:
            prev_close_for_calc = float(kline["close"].iloc[-1])
        symbol_change_pct = None
        if prev_close_for_calc > 0:
            symbol_change_pct = (entry_price - prev_close_for_calc) / prev_close_for_calc

        vwap_dev = None
        if vwap_approx and vwap_approx > 0:
            vwap_dev = (entry_price - vwap_approx) / vwap_approx

        spy_rt = client.get_spy_intraday_change()

        recorded_dict[symbol] = today
        record = {
            "date": today,
            "symbol": symbol,
            "dryrun_type": dryrun_type,
            "slot_count_at_signal": slot_count_at_signal,
            "first_signal_time": datetime.now().strftime("%H:%M:%S"),
            "first_signal_price": round(entry_price, 4),
            "vwap": round(vwap_approx, 4) if vwap_approx else None,
            "vwap_above": vwap_above,
            "vwap_deviation_pct": round(vwap_dev * 100, 2) if vwap_dev is not None else None,
            "atr_value": round(atr_pct * entry_price, 4),
            "atr_pct": round(atr_pct, 4),
            "sl_price": round(sl_price, 4),
            "tp_price": round(tp_price, 4),
            "score": round(sentiment.score, 3),
            "confidence": round(sentiment.confidence, 3),
            "flow_strength": round(flow.strength, 3),
            "spy_change_realtime": round(spy_rt * 100, 2) if spy_rt is not None else None,
            "symbol_change_pct": round(symbol_change_pct * 100, 2) if symbol_change_pct is not None else None,
            "texts_count": texts_count,
            "is_dynamic": symbol not in settings.WATCHLIST,
            # 高値掴み判別用フィールド
            "open_price": round(snap.open_price, 4) if snap.open_price > 0 else None,
            "high_price": round(snap.high_price, 4) if snap.high_price > 0 else None,
            "low_price": round(snap.low_price, 4) if snap.low_price > 0 else None,
            "prev_close": round(snap.prev_close, 4) if snap.prev_close > 0 else None,
            "change_from_open_pct": round(snap.change_from_open_pct, 3) if snap.change_from_open_pct is not None else None,
            "gap_pct": round(snap.gap_pct, 3) if snap.gap_pct is not None else None,
            "price_position_in_range": round(snap.price_position_in_range, 3) if snap.price_position_in_range is not None else None,
            "amplitude": round(snap.amplitude, 3) if snap.amplitude > 0 else None,
            "pre_change_rate": round(snap.pre_change_rate, 3),
            "volume_ratio": round(snap.volume_ratio, 3) if snap.volume_ratio > 0 else None,
            # tight filter 評価結果 (IF分析用)
            "tight_filter_pass": tight_pass,
            "tight_filter_reason": tight_reason,
            # 後で埋める
            "actual_entry_at": None,
            "actual_entry_price": None,
            "actual_pnl": None,
            "close_price": None,
            "close_time": None,
            "exit_reason": None,
            "virtual_pnl": None,
        }
        output_path.parent.mkdir(exist_ok=True)
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(record) + "\n")

        logger.info(
            "[DRY-RUN LONG-%s] %s entry=%.2f SL=%.2f TP=%.2f "
            "score=%.3f conf=%.3f flow=%.3f vwap_dev=%s%% slots=%s tight=%s",
            dryrun_type.upper(), symbol, entry_price, sl_price, tp_price,
            sentiment.score, sentiment.confidence, flow.strength,
            f"{vwap_dev*100:+.2f}" if vwap_dev is not None else "NA",
            slot_count_at_signal if slot_count_at_signal is not None else "?",
            "PASS" if tight_pass else "REJECT",
        )
    except Exception:
        logger.warning("[DRY-RUN LONG-%s] %s エラー（無視）", dryrun_type.upper(), symbol, exc_info=True)


async def _long_dryrun_close(client, pnl_tracker, dryrun_type: str = "skip") -> None:
    """LONG ドライランの仮想決済 + 実エントリー紐付け.

    各レコードについて:
    - close_price: 現在のスナップショット価格
    - virtual_pnl: 仮想エントリー価格→close_price で SL/TP/EOD を判定
    - actual_entry_at/price/pnl: 同じセッションで同銘柄を実エントリーしていれば紐付け
    """
    try:
        output_path = _LONG_SKIP_DRYRUN_PATH if dryrun_type == "skip" else _LONG_FULL_DRYRUN_PATH
        if not output_path.exists():
            return

        records: list[dict] = []
        with open(output_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(_json.loads(line))

        # pnl_tracker から実トレードを引く (open + closed)
        all_trades = list(pnl_tracker._open_trades.values()) + pnl_tracker._closed_trades

        updated = False
        for rec in records:
            if rec.get("close_price") is not None:
                continue  # 処理済み

            # 1) 仮想決済
            try:
                snap = client.get_snapshot(rec["symbol"])
                if snap is None or snap.last_price <= 0:
                    continue
                close_price = snap.last_price
            except Exception:
                continue

            entry_price = rec["first_signal_price"]
            sl_price = rec["sl_price"]
            tp_price = rec["tp_price"]

            if close_price <= sl_price:
                exit_reason = "SL"
                v_pnl = sl_price - entry_price
            elif close_price >= tp_price:
                exit_reason = "TP"
                v_pnl = tp_price - entry_price
            else:
                exit_reason = "FORCE_CLOSE"
                v_pnl = close_price - entry_price

            rec["close_price"] = round(close_price, 4)
            rec["close_time"] = datetime.now().strftime("%H:%M:%S")
            rec["virtual_pnl"] = round(v_pnl, 4)
            rec["exit_reason"] = exit_reason

            # 2) 実エントリー紐付け（同銘柄・direction=LONG・first_signal_time 以降）
            try:
                first_sig_str = f"{rec['date']} {rec['first_signal_time']}"
                first_sig_dt = datetime.fromisoformat(first_sig_str.replace(" ", "T"))
            except Exception:
                first_sig_dt = None

            for t in all_trades:
                if t.symbol != rec["symbol"] or t.direction != "LONG":
                    continue
                if first_sig_dt and t.opened_at < first_sig_dt:
                    continue
                rec["actual_entry_at"] = t.opened_at.strftime("%H:%M:%S")
                rec["actual_entry_price"] = round(t.entry_price, 4)
                # 決済済みなら確定 PnL、未決済なら現在価格ベース
                if t.exit_price is not None:
                    actual_pnl = (t.exit_price - t.entry_price) * t.size
                else:
                    actual_pnl = (close_price - t.entry_price) * t.size
                rec["actual_pnl"] = round(actual_pnl, 4)
                break  # 最初のマッチのみ

            updated = True
            logger.info(
                "[DRY-RUN LONG-%s CLOSE] %s entry=%.2f close=%.2f v_pnl=%+.2f actual=%s",
                dryrun_type.upper(),
                rec["symbol"], entry_price, close_price, v_pnl,
                f"{rec['actual_entry_at']}@{rec['actual_entry_price']}" if rec["actual_entry_at"] else "なし",
            )

        if updated:
            with open(output_path, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(_json.dumps(rec) + "\n")

    except Exception:
        logger.warning("[DRY-RUN LONG-%s CLOSE] エラー（無視）", dryrun_type.upper(), exc_info=True)


# ---------------------------------------------------------------------------
# メインループ
# ---------------------------------------------------------------------------

async def main_loop() -> None:
    """メインループ: データ収集 → シグナル生成 → リスク計算 → 発注."""
    global _shutdown_requested

    # シグナルハンドラ登録
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    # --- モジュール初期化 ---
    client = MoomooClient()
    try:
        client.connect()
        client.subscribe_realtime(settings.WATCHLIST)
    except (ConnectionError, Exception) as e:
        logger.critical("OpenD 接続失敗: %s", e)
        # Discord に通知して終了
        try:
            notifier = Notifier()
            notifier.notify_circuit_breaker(f"Bot 起動失敗: OpenD に接続できません — {e}")
        except Exception:
            pass
        logger.critical("=== Bot 起動失敗 — 終了 ===")
        return

    # --- 動的スクリーニング ---
    watchlist = list(settings.WATCHLIST)  # コピー
    if settings.SCREENER_ENABLED:
        try:
            from src.data.screener import get_dynamic_symbols
            dynamic_symbols = get_dynamic_symbols()
            new_symbols = [s for s in dynamic_symbols if s not in watchlist]
            watchlist = list(dict.fromkeys(watchlist + dynamic_symbols))
            if new_symbols:
                client.subscribe_realtime(new_symbols)
            logger.info(
                "WATCHLIST: %d symbols (fixed=%d + dynamic=%d)",
                len(watchlist), len(settings.WATCHLIST), len(new_symbols),
            )
        except Exception:
            logger.exception("[Screener] Failed — using fixed WATCHLIST only")

    board_scraper = BoardScraper()
    news_feed = NewsFeed()
    sentiment_analyzer = SentimentAnalyzer()
    flow_detector = FlowDetector(client)
    and_filter = AndFilter()
    position_sizer = PositionSizer()
    stop_loss_manager = StopLossManager()
    circuit_breaker = CircuitBreaker()
    pnl_tracker = PnLTracker()
    notifier = Notifier()

    # 決済コールバック: pnl_tracker / notifier / position_sizer を一元更新
    def _on_exit(result: ExitResult) -> None:
        pnl = pnl_tracker.close_trade(
            result.position.order_id, result.exit_price, result.reason,
            mfe=result.position.mfe, mae=result.position.mae,
        )
        is_win = pnl > 0
        position_sizer.update_stats(TradeResult(
            symbol=result.position.symbol, pnl=pnl, is_win=is_win,
        ))
        notifier.notify_exit(result.position.symbol, pnl, result.reason)
        logger.info(
            "[%s] EXIT %s pnl=$%.2f (%s)",
            result.position.symbol, result.reason, pnl,
            "WIN" if is_win else "LOSS",
        )

    order_router = OrderRouter(client, circuit_breaker, on_exit=_on_exit)

    # --- 既存ポジションの復元 ---
    recovered = order_router.recover_positions()
    if recovered > 0:
        # 復元したポジションに SL/TP を再計算して設定
        for order_id, pos in order_router.open_positions.items():
            if pos.levels is None:
                try:
                    kline = client.get_kline(pos.symbol)
                    levels = stop_loss_manager.calculate_levels(
                        pos.symbol, pos.entry_price,
                        price_history=kline, direction=pos.direction,
                    )
                    pos.levels = levels
                    logger.info(
                        "[%s] SL/TP再設定: SL=$%.2f TP=$%.2f",
                        pos.symbol, levels.stop_loss, levels.take_profit,
                    )
                except Exception:
                    logger.exception("[%s] SL/TP再計算エラー", pos.symbol)
        # P&L tracker にも登録
        for order_id, pos in order_router.open_positions.items():
            pnl_tracker.register(
                order_id, pos.symbol, pos.direction, pos.size, pos.entry_price,
            )

    # ポジション監視タスク
    monitor_task = asyncio.create_task(order_router.monitor_positions())

    logger.info(
        "=== moomoo AI Daytrade Bot 起動 (env=%s, symbols=%s, recovered=%d) ===",
        settings.TRADE_ENV, watchlist, recovered,
    )

    try:
        _loop_count = 0
        while not _shutdown_requested:
            _loop_count += 1
            _loop_t0 = _time.monotonic()
            logger.info("=== loop #%d start ===", _loop_count)

            # --- ET 15:50 強制決済 ---
            if should_force_exit():
                # SHORT ドライランの仮想決済を先に記録
                if settings.SHORT_DRY_RUN:
                    await _short_dryrun_close(client)

            if should_force_exit() and order_router.position_count > 0:
                logger.warning("ET 15:50 — Force closing all positions")

                # 1) monitor タスクを先に停止（asyncio.sleep 中の割り込みを防ぐ）
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass
                logger.info("monitor_positions() 停止完了")

                # 2) 全ポジションを同期で決済（確実に全注文を送る）
                order_router.exit_all_sync("ET 15:50 force close")

                # 3) LONG skip/full dryrun の仮想決済 + 実エントリー紐付け
                if settings.LONG_SKIP_DRY_RUN:
                    await _long_dryrun_close(client, pnl_tracker, dryrun_type="skip")
                if settings.LONG_FULL_DRY_RUN:
                    await _long_dryrun_close(client, pnl_tracker, dryrun_type="full")

                notifier.notify_circuit_breaker("ET 15:50 all positions force-closed")
                break

            # ポジション無しでも force_exit 時刻なら LONG dryrun を閉じて終了
            if should_force_exit():
                if settings.LONG_SKIP_DRY_RUN:
                    await _long_dryrun_close(client, pnl_tracker, dryrun_type="skip")
                if settings.LONG_FULL_DRY_RUN:
                    await _long_dryrun_close(client, pnl_tracker, dryrun_type="full")
                break

            # --- 市場クローズ中は待機 ---
            if not market_is_open():
                now_et = datetime.now(ET)
                logger.info(
                    "Market closed (ET %s, weekday=%d). Waiting 60s...",
                    now_et.strftime("%H:%M:%S"), now_et.weekday(),
                )
                await asyncio.sleep(60)
                continue

            # --- 口座状態を取得してサーキットブレーカーチェック ---
            _t = _time.monotonic()
            buying_power = client.get_account_balance() or 100_000.0
            total_assets = client.get_total_assets() or buying_power
            logger.info(
                "Account: buying_power=$%.2f total_assets=$%.2f (%.2fs)",
                buying_power, total_assets, _time.monotonic() - _t,
            )
            # ドローダウン計算には総資産、ポジションサイズには買付余力を使う
            pnl_tracker.update_peak_balance(total_assets)

            account_state = AccountState(
                balance=total_assets,  # サーキットブレーカーは総資産で判定
                daily_pnl=pnl_tracker.daily_pnl,
                peak_balance=pnl_tracker.peak_balance,
                consecutive_losses=position_sizer.consecutive_losses,
            )

            breaker_status = circuit_breaker.check(account_state)
            if not breaker_status.can_trade:
                logger.warning("サーキットブレーカー: %s", breaker_status.reason)
                notifier.notify_circuit_breaker(breaker_status.reason)
                if breaker_status.action == BreakerAction.FORCE_CLOSE_ALL:
                    monitor_task.cancel()
                    try:
                        await monitor_task
                    except asyncio.CancelledError:
                        pass
                    order_router.exit_all_sync("Circuit breaker: force close")
                    break
                await asyncio.sleep(settings.LOOP_INTERVAL_SECONDS)
                continue

            # --- 銘柄ごとのスキャンループ ---
            logger.info(
                "--- scan start (L=%d/%d S=%d/%d assets=$%.0f power=$%.0f pnl=$%.2f) ---",
                order_router.long_count, settings.LONG_MAX_POSITIONS,
                order_router.short_count, settings.SHORT_MAX_POSITIONS,
                total_assets, buying_power, pnl_tracker.daily_pnl,
            )
            # スキャンスキップ判定
            # - LONG_SKIP_DRY_RUN=true なら寄り付き期間もスキャン (実発注なし、JSONL記録のみ)
            # - LONG_FULL_DRY_RUN=true なら枠フル時もスキャン (実発注なし、JSONL記録のみ)
            in_open_skip = is_market_open_skip()
            slots_full = order_router.long_count >= settings.LONG_MAX_POSITIONS
            scan_skip_for_open = in_open_skip and not settings.LONG_SKIP_DRY_RUN
            scan_skip_for_full = slots_full and not settings.LONG_FULL_DRY_RUN
            skip_reason = None
            if scan_skip_for_open:
                now_et = datetime.now(ET)
                skip_until = now_et.replace(hour=9, minute=30, second=0) + timedelta(
                    minutes=settings.MARKET_OPEN_SKIP_MINUTES,
                )
                skip_reason = (
                    f"Opening skip: {settings.MARKET_OPEN_SKIP_MINUTES}min "
                    f"(until ET {skip_until.strftime('%H:%M')})"
                )
            elif scan_skip_for_full:
                skip_reason = f"LONG_MAX_POSITIONS({settings.LONG_MAX_POSITIONS}) reached"
            elif buying_power < settings.MIN_BUYING_POWER:
                skip_reason = f"Insufficient buying power (${buying_power:.0f} < ${settings.MIN_BUYING_POWER})"

            if skip_reason:
                logger.info("%s — scan skipped (saving API cost)", skip_reason)
                logger.info("--- scan end (0.0s) --- next in %ds", settings.LOOP_INTERVAL_SECONDS)
                logger.info("=== loop #%d end === sleeping %ds", _loop_count, settings.LOOP_INTERVAL_SECONDS)
                await asyncio.sleep(settings.LOOP_INTERVAL_SECONDS)
                logger.info("=== loop #%d wake ===", _loop_count)
                continue

            if in_open_skip:
                _skip_total = 30 + settings.MARKET_OPEN_SKIP_MINUTES  # 9:30 + skip分
                _skip_h = 9 + _skip_total // 60
                _skip_m = _skip_total % 60
                logger.info(
                    "Opening skip period: scanning for IF analysis (no real LONG entries until ET %d:%02d)",
                    _skip_h, _skip_m,
                )

            if slots_full:
                logger.info(
                    "LONG slots full (%d/%d): scanning for IF analysis (no real LONG entries)",
                    order_router.long_count, settings.LONG_MAX_POSITIONS,
                )

            existing_symbols = {p.symbol for p in order_router.open_positions.values()}

            for symbol in watchlist:
                if _shutdown_requested:
                    break
                try:
                    # 0) 既存ポジションがある銘柄はスキップ
                    if symbol in existing_symbols:
                        continue

                    # 1) フロー先行取得（API不要・低コスト）
                    flow = flow_detector.get_flow_signal(symbol)

                    # 2) flow=NEUTRAL ならスキップ（BUY/SELL のみ処理）
                    if flow.direction == "NEUTRAL":
                        logger.info(
                            "[%s] flow=NEUTRAL(%.2f) -> SKIP(API skipped)",
                            symbol, flow.strength,
                        )
                        continue

                    # flow=SELL → SHORT処理（必ずcontinue、LONGには流れない）
                    if flow.direction == "SELL":
                        if settings.ENABLE_SHORT:
                            # SHORT 実エントリー処理（将来実装）
                            if settings.SHORT_DRY_RUN:
                                await _short_dryrun(
                                    symbol=symbol,
                                    flow_strength=flow.strength,
                                    board_scraper=board_scraper,
                                    news_feed=news_feed,
                                    sentiment_analyzer=sentiment_analyzer,
                                    client=client,
                                    stop_loss=stop_loss_manager,
                                    order_router=order_router,
                                )
                            logger.info(
                                "[%s] flow=SELL(%.2f) -> SHORT candidate (not yet implemented)",
                                symbol, flow.strength,
                            )
                        else:
                            if settings.SHORT_DRY_RUN:
                                await _short_dryrun(
                                    symbol=symbol,
                                    flow_strength=flow.strength,
                                    board_scraper=board_scraper,
                                    news_feed=news_feed,
                                    sentiment_analyzer=sentiment_analyzer,
                                    client=client,
                                    stop_loss=stop_loss_manager,
                                    order_router=order_router,
                                )
                            logger.info(
                                "[%s] flow=SELL(%.2f) -> SKIP(SHORT disabled)",
                                symbol, flow.strength,
                            )
                        continue  # flow=SELL は必ず continue

                    # flow.strength が閾値未満ならAPIスキップ
                    if flow.strength <= settings.FLOW_BUY_THRESHOLD:
                        logger.info(
                            "[%s] flow=%s(%.2f) -> SKIP(strength too low, API skipped)",
                            symbol, flow.direction, flow.strength,
                        )
                        continue

                    # 買付余力で買えない銘柄はAPIスキップ
                    snap = client.get_snapshot(symbol)
                    if snap.last_price > 0 and snap.last_price > buying_power:
                        logger.info(
                            "[%s] flow=%s(%.2f) price=$%.0f > power=$%.0f -> SKIP(can't afford)",
                            symbol, flow.direction, flow.strength,
                            snap.last_price, buying_power,
                        )
                        continue

                    # 3) テキスト収集
                    posts = await board_scraper.fetch_posts(symbol)
                    news_articles = await news_feed.get_latest(symbol)
                    texts = [p.text for p in posts] + [
                        f"{a.title} {a.body}" for a in news_articles
                    ]

                    # 4) テキスト不足ならClaude APIをスキップ
                    filtered_count = len([t for t in texts if t.strip()])
                    if filtered_count < settings.MIN_TEXTS_FOR_ANALYSIS:
                        logger.info(
                            "[%s] flow=%s(%.2f) texts=%d -> SKIP(texts < %d, API skipped)",
                            symbol, flow.direction, flow.strength,
                            filtered_count, settings.MIN_TEXTS_FOR_ANALYSIS,
                        )
                        continue

                    # 5) Claude APIでセンチメント分析（flow=BUY + texts十分の場合のみ）
                    sentiment = sentiment_analyzer.analyze(texts, symbol)
                    decision = and_filter.should_enter(sentiment, flow)

                    # VWAP は snapshot の avg_price を優先 (フォールバックで turnover/volume)
                    vwap_str = "N/A"
                    vwap_approx = snap.best_vwap or None
                    vwap_above = snap.last_price > vwap_approx if vwap_approx else None
                    if vwap_approx:
                        vwap_str = f"{vwap_approx:.2f}({'上' if vwap_above else '下'})"

                    logger.info(
                        "[%s] texts=%d sentiment=%.2f conf=%.2f flow=%s(%.2f) "
                        "vwap=%s -> %s",
                        symbol, len(texts), sentiment.score, sentiment.confidence,
                        flow.direction, flow.strength,
                        vwap_str,
                        "ENTRY" if decision.go else f"SKIP({decision.reason[:50]})",
                    )

                    if decision.go:
                        snapshot = client.get_snapshot(symbol)
                        current_price = snapshot.last_price
                        if current_price <= 0:
                            continue

                        kline = client.get_kline(symbol)
                        levels = stop_loss_manager.calculate_levels(
                            symbol, current_price,
                            price_history=kline, direction=decision.direction,
                        )

                        # tight filter 評価 (LONG のみ。実エントリーをゲート、dryrun は記録継続)
                        tight_pass = True
                        tight_reason = "n/a"
                        if decision.direction == "LONG":
                            _atr_pct_for_filter = stop_loss_manager.calc_atr_pct(kline, current_price)
                            _is_dynamic_for_filter = symbol not in settings.WATCHLIST
                            tight_pass, tight_reason = and_filter.tight_filter_long(
                                snapshot, vwap_approx,
                                atr_pct=_atr_pct_for_filter,
                                is_dynamic=_is_dynamic_for_filter,
                            )

                        # スキップ期間中の LONG は実発注せず JSONL に記録（IF分析用）
                        if in_open_skip and decision.direction == "LONG" and settings.LONG_SKIP_DRY_RUN:
                            await _long_dryrun_record(
                                symbol=symbol,
                                sentiment=sentiment,
                                flow=flow,
                                snap=snapshot,
                                vwap_approx=vwap_approx,
                                vwap_above=vwap_above,
                                levels=levels,
                                kline=kline,
                                stop_loss=stop_loss_manager,
                                client=client,
                                texts_count=len(texts),
                                tight_pass=tight_pass,
                                tight_reason=tight_reason,
                                dryrun_type="skip",
                                slot_count_at_signal=order_router.long_count,
                            )
                            continue

                        # 枠フル時の LONG は実発注せず JSONL に記録（IF分析用）
                        if slots_full and decision.direction == "LONG" and settings.LONG_FULL_DRY_RUN:
                            await _long_dryrun_record(
                                symbol=symbol,
                                sentiment=sentiment,
                                flow=flow,
                                snap=snapshot,
                                vwap_approx=vwap_approx,
                                vwap_above=vwap_above,
                                levels=levels,
                                kline=kline,
                                stop_loss=stop_loss_manager,
                                client=client,
                                texts_count=len(texts),
                                tight_pass=tight_pass,
                                tight_reason=tight_reason,
                                dryrun_type="full",
                                slot_count_at_signal=order_router.long_count,
                            )
                            continue

                        # tight filter 不合格なら実発注せず ログのみ
                        if decision.direction == "LONG" and not tight_pass:
                            logger.info(
                                "[%s] AND pass but TIGHT FILTER REJECTED: %s",
                                symbol, tight_reason,
                            )
                            continue

                        size = position_sizer.calculate(
                            symbol, current_price, buying_power,
                        )
                        result = await order_router.enter(
                            decision, symbol, size, current_price, levels,
                        )
                        if result and result.status not in ("FAILED", "CANCELLED"):
                            logger.info(
                                "[%s] ENTRY %s %d shares @ $%.2f (order=%s)",
                                symbol, decision.direction, size, current_price, result.order_id,
                            )
                            # ATR/SPY を記録用に取得
                            _atr_pct = stop_loss_manager.calc_atr_pct(kline, current_price)
                            _atr_val = current_price * _atr_pct
                            _spy_rt = client.get_spy_intraday_change()

                            # 銘柄の当日騰落率: snapshot の prev_close 優先、フォールバック kline
                            _prev_close_calc = snapshot.prev_close
                            if _prev_close_calc <= 0 and kline is not None and len(kline) >= 1:
                                _prev_close_calc = float(kline["close"].iloc[-1])
                            _sym_change = None
                            if _prev_close_calc > 0:
                                _sym_change = (current_price - _prev_close_calc) / _prev_close_calc

                            # VWAP 乖離率
                            _vwap_dev = None
                            if vwap_approx and vwap_approx > 0:
                                _vwap_dev = (current_price - vwap_approx) / vwap_approx

                            pnl_tracker.register(
                                result.order_id, symbol, decision.direction,
                                size, current_price,
                                atr_value=_atr_val,
                                atr_pct=_atr_pct,
                                vwap_above=vwap_above,
                                vwap_price=vwap_approx,
                                spy_rt=_spy_rt,
                                sentiment_score=sentiment.score,
                                sentiment_confidence=sentiment.confidence,
                                flow_strength=flow.strength,
                                is_dynamic=symbol not in settings.WATCHLIST,
                                symbol_change_pct=_sym_change,
                                vwap_deviation_pct=_vwap_dev,
                                texts_count=len(texts),
                                sl_price=levels.stop_loss if levels else None,
                                tp_price=levels.take_profit if levels else None,
                                # 高値掴み判別用フィールド (snapshot から)
                                open_price=snapshot.open_price if snapshot.open_price > 0 else None,
                                high_price=snapshot.high_price if snapshot.high_price > 0 else None,
                                low_price=snapshot.low_price if snapshot.low_price > 0 else None,
                                prev_close=snapshot.prev_close if snapshot.prev_close > 0 else None,
                                change_from_open_pct=snapshot.change_from_open_pct,
                                gap_pct=snapshot.gap_pct,
                                price_position_in_range=snapshot.price_position_in_range,
                                amplitude=snapshot.amplitude if snapshot.amplitude > 0 else None,
                                pre_change_rate=snapshot.pre_change_rate,
                                volume_ratio=snapshot.volume_ratio if snapshot.volume_ratio > 0 else None,
                            )
                            notifier.notify_entry(
                                symbol, decision.direction, size, current_price,
                            )
                            notifier.notify_signal(
                                symbol, sentiment.score, flow.strength,
                                current_price, decision.direction,
                            )

                except Exception:
                    logger.exception("銘柄 %s の処理でエラー", symbol)

            _loop_elapsed = _time.monotonic() - _loop_t0
            logger.info(
                "--- scan end (%.1fs) --- next in %ds",
                _loop_elapsed, settings.LOOP_INTERVAL_SECONDS,
            )
            logger.info("=== loop #%d end === sleeping %ds", _loop_count, settings.LOOP_INTERVAL_SECONDS)
            await asyncio.sleep(settings.LOOP_INTERVAL_SECONDS)
            logger.info("=== loop #%d wake ===", _loop_count)

    finally:
        # --- クリーンアップ ---
        if not monitor_task.done():
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

        # 残りのポジションを警告
        if order_router.position_count > 0:
            logger.warning(
                "未決済ポジション %d件が残っています", order_router.position_count,
            )

        # 日次サマリー送信
        summary = pnl_tracker.get_daily_summary()
        notifier.notify_daily_summary(summary)

        # CSV保存
        try:
            pnl_tracker.save_to_csv()
        except Exception:
            logger.exception("CSV保存エラー")

        # 接続クローズ
        await board_scraper.close()
        await news_feed.close()
        client.close()

        logger.info(
            "=== Bot 停止 === 日次PnL: %.2f トレード: %d件",
            pnl_tracker.daily_pnl,
            pnl_tracker.closed_trade_count,
        )


def main() -> None:
    """エントリーポイント."""
    asyncio.run(main_loop())


if __name__ == "__main__":
    main()
