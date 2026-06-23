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
from collections import defaultdict, deque
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
    spy_change: float | None = None,
    qqq_change: float | None = None,
    spy_change_open: float | None = None,
    qqq_change_open: float | None = None,
    position_sizer=None,  # 実エントリー時 (ENABLE_SHORT=true) に必要
    pnl_tracker=None,     # 実エントリー時 (ENABLE_SHORT=true) に必要
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
        # 呼び出し元のループ先頭キャッシュを優先（毎回 API を叩かない）
        spy_rt = spy_change if spy_change is not None else client.get_spy_intraday_change()
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
        # SHORT 専用 ATR 乗数 (FINAL-7 推奨: SL/TP 共に 0.7)
        sl_price = entry_price * (1 + atr_pct * settings.ATR_SL_MULTIPLIER_SHORT)
        tp_price = entry_price * (1 - atr_pct * settings.ATR_TP_MULTIPLIER_SHORT)

        # === FINAL-7 フィルタ + 案C 拡張 (amp 下限, vol_ratio 範囲) ===
        # 失敗してもメタ情報は dryrun jsonl に記録される (分析統合用)
        final7_pass = True
        final7_reason = "passed"
        # 1. KLAC など構造的負け銘柄を除外
        if symbol in settings.SHORT_BLOCK_SYMBOLS:
            final7_pass = False
            final7_reason = f"BLOCK_SYMBOL: {symbol}"
        # 2. gap > SHORT_GAP_MIN_PCT 必須 (default -100% で実質無効化、 .env で -2.0 等を指定)
        elif snap.gap_pct is not None and snap.gap_pct <= settings.SHORT_GAP_MIN_PCT:
            final7_pass = False
            final7_reason = f"gap={snap.gap_pct:.2f}% <= {settings.SHORT_GAP_MIN_PCT}%"
        # 3. amp < SHORT_AMP_MAX_PCT (過熱反転リスク)
        elif snap.amplitude is not None and snap.amplitude >= settings.SHORT_AMP_MAX_PCT:
            final7_pass = False
            final7_reason = f"amp={snap.amplitude:.2f}% >= {settings.SHORT_AMP_MAX_PCT}%"
        # 3b. (案C 6/23) amp >= SHORT_AMP_MIN_PCT (値動き不足を除外)
        elif snap.amplitude is not None and snap.amplitude < settings.SHORT_AMP_MIN_PCT:
            final7_pass = False
            final7_reason = f"amp={snap.amplitude:.2f}% < {settings.SHORT_AMP_MIN_PCT}% (下限)"
        # 3c. (案C 6/23) vol_ratio が [MIN, MAX) 範囲外 → 弛緩/過熱を除外
        elif snap.volume_ratio is not None and (
            snap.volume_ratio < settings.SHORT_VOL_RATIO_MIN
            or snap.volume_ratio >= settings.SHORT_VOL_RATIO_MAX
        ):
            final7_pass = False
            final7_reason = (
                f"vol={snap.volume_ratio:.2f} 範囲外 "
                f"[{settings.SHORT_VOL_RATIO_MIN}, {settings.SHORT_VOL_RATIO_MAX})"
            )
        # 4. SPY prev_close < SHORT_SPY_MAX_PC% (弱気相場のみ)
        elif spy_change is None:
            final7_pass = False
            final7_reason = "SPY_PC unavailable"
        elif spy_change >= settings.SHORT_SPY_MAX_PC / 100.0:
            final7_pass = False
            final7_reason = f"SPY_PC={spy_change*100:.2f}% >= {settings.SHORT_SPY_MAX_PC}%"

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

        # 方向情報 (6/17 Idea B: _price_history deque から計算)
        _d5_short, _d15_short, _vel_short = _calc_direction_from_history(symbol)
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
            "qqq_change_realtime": round(qqq_change * 100, 2) if qqq_change is not None else None,
            "spy_change_open": round(spy_change_open * 100, 2) if spy_change_open is not None else None,
            "qqq_change_open": round(qqq_change_open * 100, 2) if qqq_change_open is not None else None,
            "symbol_change_pct": round(symbol_change_pct * 100, 2) if symbol_change_pct is not None else None,
            "individual_would_trigger": individual_would_trigger,
            "direction_5min_pct": round(_d5_short, 3) if _d5_short is not None else None,
            "direction_15min_pct": round(_d15_short, 3) if _d15_short is not None else None,
            "direction_velocity": round(_vel_short, 4) if _vel_short is not None else None,
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
            # FINAL-7 フィルタ評価 (6/19 Phase 1 解禁)
            "final7_pass": final7_pass,
            "final7_reason": final7_reason,
            # 実エントリー追跡 (後の実 + 仮想統合分析用、 _short_real_close で更新)
            "actual_entry_at": None,
            "actual_entry_price": None,
            "actual_order_id": None,
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
            "score=%.3f conf=%.3f flow=%.3f%s final7=%s",
            pattern, symbol, entry_price, sl_price, tp_price,
            score, confidence, flow_strength, spy_str,
            "PASS" if final7_pass else f"FAIL({final7_reason})",
        )

        # === 実エントリー (ENABLE_SHORT=true かつ SHORT_DRY_RUN=false かつ FINAL-7 通過時) ===
        # SHORT_DRY_RUN=true なら dryrun jsonl 記録のみで実発注しない (safety switch)
        # ユーザーが Phase 1 開始時に SHORT_DRY_RUN=false に切り替えで本番化
        if (
            settings.ENABLE_SHORT
            and not settings.SHORT_DRY_RUN
            and final7_pass
            and order_router is not None
            and position_sizer is not None
            and pnl_tracker is not None
        ):
            try:
                from src.signals.and_filter import EntryDecision
                from src.risk.stop_loss import Levels
                # 既に SHORT 枠フルなら skip
                if order_router.short_count >= settings.SHORT_MAX_POSITIONS:
                    logger.info(
                        "[%s] SHORT 枠フル (%d/%d) — 実エントリーは見送り",
                        symbol, order_router.short_count, settings.SHORT_MAX_POSITIONS,
                    )
                else:
                    # SHORT 用サイズ計算 (SHORT_POSITION_SIZE_USD)
                    bp = client.get_account_balance() or 100_000.0
                    size = position_sizer.calculate(symbol, entry_price, bp, direction="SHORT")
                    if size <= 0:
                        logger.info("[%s] SHORT size=0 — skip", symbol)
                    else:
                        # SL/TP levels を構築 (LONG とは方向逆)
                        levels = Levels(
                            stop_loss=sl_price,
                            take_profit=tp_price,
                            trailing_stop=entry_price + (entry_price - tp_price) * 0.5,
                        )
                        decision = EntryDecision(go=True, direction="SHORT")
                        result = await order_router.enter(
                            decision, symbol, size, entry_price, levels,
                        )
                        if result is not None and result.status == "FILLED":
                            actual_price = result.filled_price or entry_price
                            # 実エントリーを dryrun jsonl にも反映 (分析統合)
                            record["actual_entry_at"] = datetime.now().strftime("%H:%M:%S")
                            record["actual_entry_price"] = round(actual_price, 4)
                            record["actual_order_id"] = result.order_id
                            # 末尾レコードを書き換え (シンプル: 該当行を rewriting せず追記で間に合わせる場合は次回 close 処理時に actual_* を埋める設計)
                            # → ここでは新規追記でなく、 後で _short_dryrun_close が actual_* を更新する想定
                            # PnLTracker に登録
                            _d5_r, _d15_r, _vel_r = _calc_direction_from_history(symbol)
                            pnl_tracker.register(
                                result.order_id, symbol, "SHORT",
                                size, actual_price,
                                atr_value=atr_pct * entry_price,
                                atr_pct=atr_pct,
                                vwap_above=vwap_above,
                                vwap_price=vwap_value,
                                spy_rt=spy_rt,
                                qqq_rt=qqq_change,
                                spy_rt_open=spy_change_open,
                                qqq_rt_open=qqq_change_open,
                                sentiment_score=score,
                                sentiment_confidence=confidence,
                                flow_strength=flow_strength,
                                is_dynamic=symbol not in settings.WATCHLIST,
                                symbol_change_pct=symbol_change_pct,
                                vwap_deviation_pct=(
                                    (entry_price - vwap_value) / vwap_value
                                    if vwap_value else None
                                ),
                                sl_price=sl_price,
                                tp_price=tp_price,
                                open_price=snap.open_price if snap.open_price > 0 else None,
                                high_price=snap.high_price if snap.high_price > 0 else None,
                                low_price=snap.low_price if snap.low_price > 0 else None,
                                prev_close=snap.prev_close if snap.prev_close > 0 else None,
                                change_from_open_pct=snap.change_from_open_pct,
                                gap_pct=snap.gap_pct,
                                price_position_in_range=snap.price_position_in_range,
                                amplitude=snap.amplitude if snap.amplitude > 0 else None,
                                pre_change_rate=snap.pre_change_rate,
                                volume_ratio=snap.volume_ratio if snap.volume_ratio > 0 else None,
                                direction_5min_pct=_d5_r,
                                direction_15min_pct=_d15_r,
                                direction_velocity=_vel_r,
                            )
                            logger.info(
                                "[%s] SHORT 実エントリー成功: %d shares @ $%.2f order_id=%s",
                                symbol, size, actual_price, result.order_id,
                            )
                        else:
                            logger.warning(
                                "[%s] SHORT 実エントリー失敗 or 不完全約定: result=%s",
                                symbol, result,
                            )
            except Exception:
                logger.exception("[%s] SHORT 実エントリー処理エラー", symbol)

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

# tight_filter で REJECT されたシグナル (枠空き・通常時間帯) を IF 分析用に記録。
# 本番稼働 (ENABLE_REAL_TRADING=true) でフィルタが効いた後も、もしフィルタ
# 無しなら実エントリーされていた銘柄を仮想 PnL で追跡できるようにする。
_long_rejected_dryrun_recorded: dict[str, str] = {}
_LONG_REJECTED_DRYRUN_PATH = Path(_project_root) / "data" / "long_rejected_dryrun.jsonl"

# 押し目待ちキュー: vwap_dev > PULLBACK_VWAP_ENTRY_PCT で発火した銘柄を保持
# {symbol: {fired_at, decision, sentiment, vwap_price, kline, texts_count,
#           entry_price_at_signal, min_vwap_dev}}
# min_vwap_dev: vwap_dev の最小値を追跡し、反転確認 (現在 > min) を取ってからエントリー
# 注: flow/levels/atr_pct/snapshot は queue 処理時に再取得 (古い値で発注しないため)
_pullback_queue: dict[str, dict] = {}

# 直近スキャン時の銘柄価格 (エントリー直前下落チェック用)
# 銘柄ごとに main scan の "AND filter 通過時点での price" を記録
# 次のスキャン時に比較して直近30秒で大きく下落していたら entry skip
_last_scan_prices: dict[str, float] = {}

# 価格履歴 (A1/A3 ルール用) — 銘柄ごとに直近 N 観測の (timestamp, price) を保持。
# maxlen=ENTRY_PRICE_HISTORY_DEPTH (デフォルト 6 = 3 分分)
_price_history: dict[str, deque] = defaultdict(
    lambda: deque(maxlen=settings.ENTRY_PRICE_HISTORY_DEPTH)
)
# スキャン価格を時系列で保存 (A1/A3 ルールの後付け検証用)
_SCAN_PRICE_LOG_PATH = Path(_project_root) / "data" / "scan_price_log.jsonl"
# A1/A3 ブロックイベントを記録 (クールダウン案の事後検証用)
# 後で trades CSV と突合して「エントリー直前 N 分の BLOCK 数 vs MAE」 を分析できる
_A1A3_BLOCK_LOG_PATH = Path(_project_root) / "data" / "a1a3_block_log.jsonl"


def _log_a1a3_block(symbol: str, rule: str, prev: float, cur: float, extra: str = "") -> None:
    """A1/A3 BLOCK イベントを JSONL に記録. 失敗してもサイレント."""
    if not settings.A1A3_BLOCK_LOG_ENABLED:
        return
    try:
        _A1A3_BLOCK_LOG_PATH.parent.mkdir(exist_ok=True)
        with open(_A1A3_BLOCK_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "symbol": symbol,
                "rule": rule,
                "prev_price": round(prev, 4),
                "cur_price": round(cur, 4),
                "extra": extra,
            }) + "\n")
    except Exception:
        pass


def _record_scan_price(symbol: str, price: float, timestamp: datetime | None = None) -> None:
    """スキャン時の snapshot 価格を履歴に記録 + JSONL 出力.

    A1/A3 ルールがエントリー直前に参照する。
    """
    if price <= 0:
        return
    ts = timestamp or datetime.now()
    _price_history[symbol].append((ts, price))
    if settings.SCAN_PRICE_LOG_ENABLED:
        try:
            _SCAN_PRICE_LOG_PATH.parent.mkdir(exist_ok=True)
            with open(_SCAN_PRICE_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(_json.dumps({
                    "ts": ts.isoformat(timespec="seconds"),
                    "symbol": symbol,
                    "price": round(price, 4),
                }) + "\n")
        except Exception:
            pass  # ロガー失敗はサイレント (エントリー判定をブロックしない)


def _calc_direction_from_history(symbol: str) -> tuple[float | None, float | None, float | None]:
    """_price_history deque から方向情報を計算する.

    スキャン間隔約 30 秒前提:
      - direction_5min_pct: 直近 10 サンプル前との変化率 (%) ≒ 5 分前比
      - direction_15min_pct: 直近 30 サンプル前との変化率 (%) ≒ 15 分前比
      - direction_velocity: 直近 5 サンプルの平均変化速度 (%/サンプル)

    サンプル不足時は該当する値を None で返す (既存挙動を壊さない)。
    ENTRY_PRICE_HISTORY_DEPTH=30 設定で 15 分分の履歴を保持する必要あり。
    """
    hist = list(_price_history.get(symbol, []))
    if len(hist) < 2:
        return None, None, None
    current = hist[-1][1]
    if current <= 0:
        return None, None, None
    d5 = d15 = vel = None
    # direction_5min_pct: 10 サンプル前比 (約 5 分前 ≒ スキャン 30 秒 × 10)
    if len(hist) >= 11:
        prev_5min = hist[-11][1]
        if prev_5min > 0:
            d5 = (current - prev_5min) / prev_5min * 100
    # direction_15min_pct: 30 サンプル前比 (約 14.5 分前 ≒ deque maxlen=30 の先頭)
    if len(hist) >= 30:
        prev_15min = hist[-30][1]
        if prev_15min > 0:
            d15 = (current - prev_15min) / prev_15min * 100
    # direction_velocity: 直近 5 サンプルの平均変化速度 (%/サンプル)
    if len(hist) >= 6:
        prev_5 = hist[-6][1]
        if prev_5 > 0:
            vel = (current - prev_5) / prev_5 * 100 / 5
    return d5, d15, vel


def _check_price_trend_rules(symbol: str) -> tuple[bool, str]:
    """A1 (直近スキャン下落) / A3 (直近 3 観測の local low) チェック.

    Returns:
        (ok_to_enter, reason): ok_to_enter=False ならエントリー禁止。
        履歴不足や両ルール OFF なら True を返す。
    """
    if not (settings.ENTRY_BLOCK_ON_DECLINE or settings.ENTRY_BLOCK_BELOW_LOCAL_LOW):
        return True, "rules_disabled"
    hist = list(_price_history.get(symbol, []))
    if len(hist) < 2:
        return True, "insufficient_history"
    current = hist[-1][1]
    prev = hist[-2][1]
    # A1: 直近スキャンより下げ
    if settings.ENTRY_BLOCK_ON_DECLINE and current < prev:
        reason = f"A1: {prev:.2f}->{current:.2f}"
        _log_a1a3_block(symbol, "A1", prev, current, reason)
        return False, reason
    # A3: 直近 3 観測 local_low + buffer 未達
    if settings.ENTRY_BLOCK_BELOW_LOCAL_LOW and len(hist) >= 3:
        local_low = min(p for _, p in hist[-3:])
        threshold = local_low * (1 + settings.ENTRY_PRICE_LOCAL_LOW_BUFFER)
        if current < threshold:
            reason = f"A3: low=${local_low:.2f} cur=${current:.2f}"
            _log_a1a3_block(symbol, "A3", local_low, current, reason)
            return False, reason
    return True, "passed"

# 押し目待ちイベント記録 (IF分析用)
_PULLBACK_LOG_PATH = Path(_project_root) / "data" / "pullback_log.jsonl"


def _log_pullback_event(event: dict) -> None:
    """押し目待ちイベントを JSONL に記録. エラーは握りつぶして本処理に影響させない."""
    try:
        event = {"date": date.today().isoformat(), **event}
        _PULLBACK_LOG_PATH.parent.mkdir(exist_ok=True)
        with open(_PULLBACK_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps(event) + "\n")
    except Exception:
        logger.warning("[pullback_log] 記録エラー（無視）", exc_info=True)


# Filter D 候補記録 (log-only 期間中のデータ蓄積用)
_filter_d_recorded: dict[str, str] = {}
_FILTER_D_LOG_PATH = Path(_project_root) / "data" / "filter_d_log.jsonl"

# 保有ポジション中のシグナル状況記録 (Claude API 不要・軽量)
# {symbol: 最後に記録した時刻} で重複/頻度制御
_IN_POSITION_LOG_PATH = Path(_project_root) / "data" / "in_position_signal.jsonl"
_in_position_last_logged: dict[str, datetime] = {}


def _log_in_position_signal(event: dict) -> None:
    """保有中シグナル状況を JSONL に記録. エラーは握りつぶし."""
    try:
        event = {"date": date.today().isoformat(), **event}
        _IN_POSITION_LOG_PATH.parent.mkdir(exist_ok=True)
        with open(_IN_POSITION_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps(event) + "\n")
    except Exception:
        logger.warning("[in_position_log] 記録エラー（無視）", exc_info=True)

# モメンタム検知 (1セッション1回 / Bot再起動でリセット)
_momentum_scan_done: bool = False
_momentum_added_symbols: set[str] = set()
_MOMENTUM_CANDIDATES_PATH = Path(_project_root) / "data" / "momentum_candidates.json"


def _log_filter_d_event(event: dict) -> None:
    """Filter D 候補をJSONLに記録. エラーは握りつぶす."""
    try:
        event = {"date": date.today().isoformat(), **event}
        _FILTER_D_LOG_PATH.parent.mkdir(exist_ok=True)
        with open(_FILTER_D_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps(event) + "\n")
    except Exception:
        logger.warning("[filter_d_log] 記録エラー（無視）", exc_info=True)


async def _filter_d_log_close(client) -> None:
    """Filter D log の close_price/virtual_pnl を更新 (ET 15:50 強制決済時に呼ぶ)."""
    try:
        if not _FILTER_D_LOG_PATH.exists():
            return
        records: list[dict] = []
        with open(_FILTER_D_LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(_json.loads(line))
        updated = False
        for rec in records:
            if rec.get("close_price") is not None:
                continue  # 処理済み
            try:
                snap = client.get_snapshot(rec["symbol"])
            except Exception:
                continue
            if snap is None or snap.last_price <= 0:
                continue
            close_price = snap.last_price
            entry_price = rec["entry_price"]
            sl_price = rec.get("sl_price")
            tp_price = rec.get("tp_price")

            if sl_price is not None and close_price <= sl_price:
                exit_reason = "SL"
                v_pnl = sl_price - entry_price
            elif tp_price is not None and close_price >= tp_price:
                exit_reason = "TP"
                v_pnl = tp_price - entry_price
            else:
                exit_reason = "FORCE_CLOSE"
                v_pnl = close_price - entry_price

            rec["close_price"] = round(close_price, 4)
            rec["close_time"] = datetime.now().strftime("%H:%M:%S")
            rec["exit_reason"] = exit_reason
            rec["virtual_pnl"] = round(v_pnl, 4)
            updated = True
            logger.info(
                "[FILTER-D CLOSE] %s entry=%.2f close=%.2f v_pnl=%+.2f reason=%s",
                rec["symbol"], entry_price, close_price, v_pnl, exit_reason,
            )
        if updated:
            with open(_FILTER_D_LOG_PATH, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(_json.dumps(rec) + "\n")
    except Exception:
        logger.warning("[filter_d_log] close エラー（無視）", exc_info=True)


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
    spy_change: float | None = None,
    qqq_change: float | None = None,
    spy_change_open: float | None = None,
    qqq_change_open: float | None = None,
) -> None:
    """LONG エントリー条件成立を JSONL に記録する (実発注なし).

    dryrun_type:
      - "skip": スキップ期間中 (22:30-23:30) のシグナル → long_skip_dryrun.jsonl
      - "full": 5枠フル時のシグナル → long_full_dryrun.jsonl
      - "rejected": tight_filter で弾かれた通常時間のシグナル → long_rejected_dryrun.jsonl

    同一銘柄は1セッション1回のみ記録（最初に条件成立した時点）。
    """
    try:
        today = date.today().isoformat()
        if dryrun_type == "skip":
            recorded_dict = _long_skip_dryrun_recorded
            output_path = _LONG_SKIP_DRYRUN_PATH
        elif dryrun_type == "rejected":
            recorded_dict = _long_rejected_dryrun_recorded
            output_path = _LONG_REJECTED_DRYRUN_PATH
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

        # 地合いはキャッシュ値を優先 (呼び出し元がループ先頭で取得済み)
        spy_rt = spy_change if spy_change is not None else client.get_spy_intraday_change()

        # 方向情報 (6/17 Idea B: _price_history deque から計算)
        _d5_long, _d15_long, _vel_long = _calc_direction_from_history(symbol)
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
            "qqq_change_realtime": round(qqq_change * 100, 2) if qqq_change is not None else None,
            "spy_change_open": round(spy_change_open * 100, 2) if spy_change_open is not None else None,
            "qqq_change_open": round(qqq_change_open * 100, 2) if qqq_change_open is not None else None,
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
            # 方向情報 (6/17 Idea B: 1-2 ヶ月蓄積後にフィルタ化判定)
            "direction_5min_pct": round(_d5_long, 3) if _d5_long is not None else None,
            "direction_15min_pct": round(_d15_long, 3) if _d15_long is not None else None,
            "direction_velocity": round(_vel_long, 4) if _vel_long is not None else None,
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
        if dryrun_type == "skip":
            output_path = _LONG_SKIP_DRYRUN_PATH
        elif dryrun_type == "rejected":
            output_path = _LONG_REJECTED_DRYRUN_PATH
        else:  # "full"
            output_path = _LONG_FULL_DRYRUN_PATH
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

        # 未 close レコードの銘柄を一括取得 (API レート制限緩和)
        # get_snapshot を 1 件ずつ呼ぶと moomoo の OpenAPI レート制限 (~10req/s) で
        # 失敗が連鎖し、 多くのレコードが close_price=None のまま残る問題があった。
        unique_symbols = list({rec["symbol"] for rec in records if rec.get("close_price") is None})
        snapshot_cache: dict[str, float] = {}
        if unique_symbols:
            try:
                codes = [f"US.{s}" for s in unique_symbols]
                snaps = client.get_snapshots(codes)
                for code, snap in snaps.items():
                    if snap is None or snap.last_price <= 0:
                        continue
                    symbol = code.replace("US.", "")
                    snapshot_cache[symbol] = snap.last_price
            except Exception:
                logger.exception("get_snapshots 一括取得失敗 — 個別取得にフォールバック")
            logger.info(
                "[DRY-RUN %s CLOSE] 一括 snapshot: %d / %d 銘柄取得",
                dryrun_type.upper(), len(snapshot_cache), len(unique_symbols),
            )

        updated = False
        for rec in records:
            if rec.get("close_price") is not None:
                continue  # 処理済み

            # 1) 仮想決済: 一括キャッシュ → fallback で個別取得
            close_price = snapshot_cache.get(rec["symbol"], 0)
            if close_price <= 0:
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
# モメンタム検知 (寄付き直前の急騰銘柄を当日 watchlist に追加)
# ---------------------------------------------------------------------------

async def _scan_momentum_symbols(client, watchlist: list[str]) -> None:
    """待機期間中(22:30-22:45)に pre/after_change_rate が高い銘柄を検知し
    momentum フラグを立てる. 候補プールに無い銘柄は当日 watchlist に追加.

    対象:
      - momentum_candidates.json (Finviz top N、固定WL除外で生成)
      - 既存 watchlist 全銘柄 (固定WL + 動的WL)
      → ユニオンを scan、急騰銘柄全件に momentum フラグを立てる

    MOMENTUM_DETECTION_ENABLED=false で完全無効化される (n=153 分析で損失源と判明)。

    エラーは握りつぶして本処理に影響させない。
    """
    if not settings.MOMENTUM_DETECTION_ENABLED:
        logger.info("[Momentum] MOMENTUM_DETECTION_ENABLED=false → 検知スキップ")
        return
    try:
        # 1) 候補銘柄リスト (Finviz top 100、固定WL除外) を読み込み
        candidate_pool: list[str] = []
        if _MOMENTUM_CANDIDATES_PATH.exists():
            data = _json.loads(_MOMENTUM_CANDIDATES_PATH.read_text(encoding="utf-8"))
            try:
                generated_at = datetime.fromisoformat(data.get("generated_at", ""))
                age_hours = (datetime.now() - generated_at).total_seconds() / 3600
                if age_hours > 24:
                    logger.info("[Momentum] momentum_candidates.json が古い(%.0fh) → 既存watchlistのみscan", age_hours)
                else:
                    candidate_pool = data.get("symbols", [])
            except (ValueError, TypeError):
                logger.warning("[Momentum] generated_at パース失敗")
        else:
            logger.info("[Momentum] momentum_candidates.json なし → 既存watchlistのみscan")

        # 2) scan対象 = 既存watchlist ∪ 候補プール
        scan_set: list[str] = list(dict.fromkeys(list(watchlist) + candidate_pool))
        if not scan_set:
            logger.info("[Momentum] scan対象なし → スキップ")
            return

        logger.info(
            "[Momentum] %d銘柄の pre/after_change_rate を確認中 (既存%d + 候補%d)...",
            len(scan_set), len(watchlist), len(candidate_pool),
        )

        # 3) バッチ取得
        codes = [f"US.{s}" for s in scan_set]
        snapshots = client.get_snapshots(codes)

        # 4) 閾値超え銘柄を全件抽出
        momentum_hits: list[tuple[str, float]] = []
        for symbol, snap in snapshots.items():
            if snap is None:
                continue
            pre_change = snap.pre_change_rate or 0.0
            after_change = snap.after_change_rate or 0.0
            max_change = max(pre_change, after_change)
            if max_change >= settings.MOMENTUM_THRESHOLD_PCT:
                momentum_hits.append((symbol, max_change))
                logger.info(
                    "[Momentum] %s pre=%.2f%% after=%.2f%% → momentum認定%s",
                    symbol, pre_change, after_change,
                    "" if symbol not in watchlist else " (既存WL内)",
                )

        if not momentum_hits:
            logger.info(
                "[Momentum] 閾値超え銘柄なし (threshold=%.1f%%)",
                settings.MOMENTUM_THRESHOLD_PCT,
            )
            return

        # 5) 全件に momentum フラグを立てる (既存 / 新規 問わず)
        momentum_hits.sort(key=lambda x: x[1], reverse=True)
        for symbol, _change in momentum_hits:
            _momentum_added_symbols.add(symbol)

        # 6) watchlist になかった銘柄のみ、上位 MOMENTUM_MAX_SYMBOLS 個まで watchlist 追加
        new_to_watchlist: list[str] = []
        for symbol, _change in momentum_hits:
            if symbol not in watchlist:
                if len(new_to_watchlist) < settings.MOMENTUM_MAX_SYMBOLS:
                    watchlist.append(symbol)
                    new_to_watchlist.append(symbol)

        # 7) リアルタイム購読 (新規追加分のみ)
        if new_to_watchlist:
            try:
                client.subscribe_realtime(new_to_watchlist)
            except Exception:
                logger.warning("[Momentum] subscribe_realtime 失敗（無視）", exc_info=True)

        existing_flagged = [s for s, _ in momentum_hits if s not in new_to_watchlist]
        logger.info(
            "[Momentum] 完了: %d銘柄 momentum認定 (既存WL内%d件にflag, 新規追加%d件: %s)",
            len(momentum_hits),
            len(existing_flagged),
            len(new_to_watchlist),
            " ".join(new_to_watchlist) if new_to_watchlist else "なし",
        )
    except Exception:
        logger.warning("[Momentum] モメンタム検知エラー（無視）", exc_info=True)


# ---------------------------------------------------------------------------
# メインループ
# ---------------------------------------------------------------------------

async def main_loop() -> None:
    """メインループ: データ収集 → シグナル生成 → リスク計算 → 発注."""
    global _shutdown_requested, _momentum_scan_done

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

                # 3) LONG skip/full/rejected dryrun の仮想決済 + 実エントリー紐付け
                if settings.LONG_SKIP_DRY_RUN:
                    await _long_dryrun_close(client, pnl_tracker, dryrun_type="skip")
                if settings.LONG_FULL_DRY_RUN:
                    await _long_dryrun_close(client, pnl_tracker, dryrun_type="full")
                # tight_filter REJECT 分は常時記録 (フラグなしで close も走らせる)
                await _long_dryrun_close(client, pnl_tracker, dryrun_type="rejected")
                # Filter D log の close_price/virtual_pnl 更新
                await _filter_d_log_close(client)

                notifier.notify_circuit_breaker("ET 15:50 all positions force-closed")
                break

            # ポジション無しでも force_exit 時刻なら LONG dryrun を閉じて終了
            if should_force_exit():
                if settings.LONG_SKIP_DRY_RUN:
                    await _long_dryrun_close(client, pnl_tracker, dryrun_type="skip")
                if settings.LONG_FULL_DRY_RUN:
                    await _long_dryrun_close(client, pnl_tracker, dryrun_type="full")
                await _long_dryrun_close(client, pnl_tracker, dryrun_type="rejected")
                await _filter_d_log_close(client)
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
            # 個別タイミング計測 (どちらの API が遅いか切り分け用)
            _t_bp = _time.monotonic()
            buying_power = client.get_account_balance() or 100_000.0
            _bp_elapsed = _time.monotonic() - _t_bp
            if _bp_elapsed > 1.0:
                logger.warning(
                    "get_account_balance 遅延: %.2fs (loop #%d)",
                    _bp_elapsed, _loop_count,
                )
            _t_ta = _time.monotonic()
            total_assets = client.get_total_assets() or buying_power
            _ta_elapsed = _time.monotonic() - _t_ta
            if _ta_elapsed > 1.0:
                logger.warning(
                    "get_total_assets 遅延: %.2fs (loop #%d)",
                    _ta_elapsed, _loop_count,
                )
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
                    # dryrun の close 処理 (ET 15:50 と同様、 累積データを欠損させない)
                    # サーキット発動時に走らないと、 jsonl の close_price=None レコードが
                    # 翌営業日まで持ち越され、 IF 分析データが欠落する。
                    try:
                        if settings.SHORT_DRY_RUN:
                            await _short_dryrun_close(client)
                        if settings.LONG_SKIP_DRY_RUN:
                            await _long_dryrun_close(client, pnl_tracker, dryrun_type="skip")
                        if settings.LONG_FULL_DRY_RUN:
                            await _long_dryrun_close(client, pnl_tracker, dryrun_type="full")
                        await _long_dryrun_close(client, pnl_tracker, dryrun_type="rejected")
                        await _filter_d_log_close(client)
                    except Exception:
                        logger.exception("Circuit Breaker 時の dryrun close 失敗")
                    break
                await asyncio.sleep(settings.LOOP_INTERVAL_SECONDS)
                continue

            # --- 地合い指標 (SPY/QQQ) をループ先頭で1度だけ取得しキャッシュ ---
            # 個別エントリー / in_position_signal で参照される。snapshot ベースなので購読不要。
            # _spy_change / _qqq_change: 前日終値基準 (主指標、 SPY フィルタ判定用)
            # _spy_change_open / _qqq_change_open: 当日始値基準 (補助、 過去 cohort 整合)
            _mi_t = _time.monotonic()
            try:
                _market_indices = client.get_market_indices()
                _mi_elapsed = _time.monotonic() - _mi_t
                if _mi_elapsed > 3.0:
                    logger.warning(
                        "get_market_indices 遅延: %.2fs (loop #%d)",
                        _mi_elapsed, _loop_count,
                    )
            except Exception:
                _mi_elapsed = _time.monotonic() - _mi_t
                logger.warning(
                    "get_market_indices 例外 (%.2fs, loop #%d): フォールバック値使用",
                    _mi_elapsed, _loop_count, exc_info=True,
                )
                _market_indices = {"spy": None, "qqq": None, "spy_open": None, "qqq_open": None}
            _spy_change = _market_indices.get("spy")
            _qqq_change = _market_indices.get("qqq")
            _spy_change_open = _market_indices.get("spy_open")
            _qqq_change_open = _market_indices.get("qqq_open")
            logger.info(
                "Market: SPY=%s (open基準%s) QQQ=%s (open基準%s)",
                f"{_spy_change*100:+.2f}%" if _spy_change is not None else "NA",
                f"{_spy_change_open*100:+.2f}%" if _spy_change_open is not None else "NA",
                f"{_qqq_change*100:+.2f}%" if _qqq_change is not None else "NA",
                f"{_qqq_change_open*100:+.2f}%" if _qqq_change_open is not None else "NA",
            )

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

                # モメンタム検知 (1セッション1回のみ)
                if not _momentum_scan_done:
                    await _scan_momentum_symbols(client=client, watchlist=watchlist)
                    _momentum_scan_done = True

            if slots_full:
                logger.info(
                    "LONG slots full (%d/%d): scanning for IF analysis (no real LONG entries)",
                    order_router.long_count, settings.LONG_MAX_POSITIONS,
                )

            # --- 押し目待ちキュー処理 ---
            # 各銘柄: タイムアウト/重複/枠フル を確認 → 押し目到来なら実エントリー
            # 寄り付きスキップ中はキュー処理もスキップ (ノイズ期間に入らない)
            if settings.PULLBACK_ENABLED and _pullback_queue and not in_open_skip:
                for _pb_symbol in list(_pullback_queue.keys()):
                    _pb = _pullback_queue[_pb_symbol]

                    # タイムアウトチェック
                    _elapsed_min = (datetime.now() - _pb['fired_at']).total_seconds() / 60
                    if _elapsed_min > settings.PULLBACK_TIMEOUT_MINUTES:
                        # 最終価格 + 価格変化率を記録
                        _t_final_price = None
                        try:
                            _t_snap = client.get_snapshot(_pb_symbol)
                            if _t_snap and _t_snap.last_price > 0:
                                _t_final_price = _t_snap.last_price
                        except Exception:
                            pass
                        _t_signal_price = _pb.get('entry_price_at_signal', 0) or 0
                        _t_chg_pct = None
                        if _t_final_price and _t_signal_price > 0:
                            _t_chg_pct = (_t_final_price - _t_signal_price) / _t_signal_price * 100
                        _log_pullback_event({
                            "event": "timeout",
                            "symbol": _pb_symbol,
                            "timeout_at": datetime.now().strftime("%H:%M:%S"),
                            "wait_minutes": round(_elapsed_min, 1),
                            "final_price": round(_t_final_price, 4) if _t_final_price else None,
                            "price_change_pct": round(_t_chg_pct, 3) if _t_chg_pct is not None else None,
                        })
                        logger.info(
                            "[%s] 押し目待ちタイムアウト(%.0f分) → キャンセル",
                            _pb_symbol, _elapsed_min,
                        )
                        del _pullback_queue[_pb_symbol]
                        continue

                    # 既存ポジションチェック
                    if _pb_symbol in {p.symbol for p in order_router.open_positions.values()}:
                        logger.info("[%s] 押し目待ち: 既存ポジションあり → キャンセル", _pb_symbol)
                        del _pullback_queue[_pb_symbol]
                        continue

                    # LONG枠チェック
                    if order_router.long_count >= settings.LONG_MAX_POSITIONS:
                        logger.info("[%s] 押し目待ち: LONG枠フル → スキップ（キューは保持）", _pb_symbol)
                        continue

                    # 現在のVWAP乖離を確認
                    try:
                        _pb_snap = client.get_snapshot(_pb_symbol)
                    except Exception:
                        logger.warning("[%s] 押し目待ち: snapshot取得失敗 → スキップ", _pb_symbol)
                        continue
                    if _pb_snap is None or _pb_snap.last_price <= 0:
                        continue
                    # 価格履歴に記録 (A1/A3 ルール用)
                    _record_scan_price(_pb_symbol, _pb_snap.last_price)
                    _pb_vwap = _pb_snap.best_vwap or _pb['vwap_price']
                    _pb_vwap_dev = (
                        (_pb_snap.last_price - _pb_vwap) / _pb_vwap * 100
                        if _pb_vwap and _pb_vwap > 0 else 999.0
                    )

                    # モメンタム銘柄は緩和閾値で判定
                    _pb_is_momentum_q = _pb_symbol in _momentum_added_symbols
                    _pb_entry_threshold = (
                        settings.MOMENTUM_VWAP_ENTRY_PCT if _pb_is_momentum_q
                        else settings.PULLBACK_VWAP_ENTRY_PCT
                    )

                    # 反転確認ロジック: vwap_dev の最小値を追跡
                    # 同値 (横ばい) も「まだ下げ or 横ばい」扱いとして最小値更新・継続
                    if _pb_vwap_dev <= _pb['min_vwap_dev']:
                        _pb['min_vwap_dev'] = _pb_vwap_dev
                        logger.debug(
                            "[%s] 押し目更新: vwap_dev=%.2f%% (最小値更新/横ばい, 反転待ち)",
                            _pb_symbol, _pb_vwap_dev,
                        )
                        continue  # エントリーしない

                    # ここに到達 = 最小値より上昇している (反転確認済み)
                    # 閾値以下ならエントリー、閾値超えはキューに留まる (次のループで再チェック)
                    if _pb_vwap_dev <= _pb_entry_threshold:
                        # 直近の価格下落チェック (反転確認とは独立した別チェック)
                        _pb_prev_price = _last_scan_prices.get(_pb_symbol, _pb_snap.last_price)
                        _pb_price_change = (
                            (_pb_snap.last_price - _pb_prev_price) / _pb_prev_price * 100
                            if _pb_prev_price > 0 else 0.0
                        )
                        # A1/A3 ルールチェック (押し目キュー解除時)
                        _pb_ok, _pb_rule_reason = _check_price_trend_rules(_pb_symbol)
                        if not _pb_ok:
                            logger.info(
                                "[%s] 押し目到来だが price trend BLOCK: %s → 待機継続",
                                _pb_symbol, _pb_rule_reason,
                            )
                            continue  # エントリーせずキューに残す

                        if _pb_price_change < -settings.ENTRY_PRICE_DROP_THRESHOLD:
                            logger.info(
                                "[%s] 押し目到来だが直近価格下落中(%.2f→%.2f, %+.3f%%) → 待機継続",
                                _pb_symbol, _pb_prev_price, _pb_snap.last_price, _pb_price_change,
                            )
                            continue  # エントリーせずキューに残す

                        logger.info(
                            "[%s] 押し目到来(反転確認): vwap_dev=%.2f%% > min=%.2f%% (閾値%.1f%%)",
                            _pb_symbol, _pb_vwap_dev, _pb['min_vwap_dev'], _pb_entry_threshold,
                        )
                        # フローを再チェック
                        _pb_flow = flow_detector.get_flow_signal(_pb_symbol)
                        if (
                            _pb_flow.direction != "BUY"
                            or _pb_flow.strength <= settings.FLOW_BUY_THRESHOLD
                        ):
                            _log_pullback_event({
                                "event": "cancelled_flow_changed",
                                "symbol": _pb_symbol,
                                "cancelled_at": datetime.now().strftime("%H:%M:%S"),
                                "wait_minutes": round(_elapsed_min, 1),
                            })
                            logger.info(
                                "[%s] 押し目到来したがフロー変化(direction=%s strength=%.2f) → キャンセル",
                                _pb_symbol, _pb_flow.direction, _pb_flow.strength,
                            )
                            del _pullback_queue[_pb_symbol]
                            continue

                        # SPY 地合いフィルタ: 暴落中は押し目キュー解除でもエントリーしない
                        # キューには残して、 SPY 回復後に出れるようにする
                        if (
                            settings.SPY_LONG_BLOCK_THRESHOLD < 0
                            and _spy_change is not None
                            and _spy_change < settings.SPY_LONG_BLOCK_THRESHOLD
                        ):
                            logger.info(
                                "[%s] 押し目到来したが SPY 地合いフィルタ BLOCKED: SPY=%+.2f%% < %+.2f%% → 待機継続",
                                _pb_symbol,
                                _spy_change * 100,
                                settings.SPY_LONG_BLOCK_THRESHOLD * 100,
                            )
                            continue

                        # エントリー実行
                        logger.info(
                            "[%s] 押し目到来(vwap_dev=%.2f%% %.0f分後) → エントリー",
                            _pb_symbol, _pb_vwap_dev, _elapsed_min,
                        )
                        _pb_levels = stop_loss_manager.calculate_levels(
                            _pb_symbol, _pb_snap.last_price,
                            price_history=_pb['kline'],
                            direction="LONG",
                        )
                        _pb_size = position_sizer.calculate(
                            _pb_symbol, _pb_snap.last_price, buying_power,
                        )
                        _pb_result = await order_router.enter(
                            _pb['decision'], _pb_symbol, _pb_size,
                            _pb_snap.last_price, _pb_levels,
                        )
                        if _pb_result and _pb_result.status not in ("FAILED", "CANCELLED"):
                            # エントリー実行ログ
                            _e_signal_price = _pb.get('entry_price_at_signal', 0) or 0
                            _e_chg_pct = None
                            if _e_signal_price > 0:
                                _e_chg_pct = (_pb_snap.last_price - _e_signal_price) / _e_signal_price * 100
                            _e_min_vwap_dev = _pb.get('min_vwap_dev')
                            _log_pullback_event({
                                "event": "executed",
                                "symbol": _pb_symbol,
                                "executed_at": datetime.now().strftime("%H:%M:%S"),
                                "wait_minutes": round(_elapsed_min, 1),
                                "entry_price": round(_pb_snap.last_price, 4),
                                "vwap_dev_at_entry": round(_pb_vwap_dev, 3),
                                "min_vwap_dev": round(_e_min_vwap_dev, 3) if _e_min_vwap_dev is not None else None,
                                "reversal_confirmed": (
                                    _e_min_vwap_dev is not None
                                    and _pb_vwap_dev > _e_min_vwap_dev
                                ),
                                "price_change_pct": round(_e_chg_pct, 3) if _e_chg_pct is not None else None,
                            })

                            _pb_atr_pct = stop_loss_manager.calc_atr_pct(_pb['kline'], _pb_snap.last_price)
                            _pb_atr_val = _pb_snap.last_price * _pb_atr_pct
                            _pb_vwap_above = _pb_snap.last_price > _pb_vwap if _pb_vwap > 0 else None
                            _pb_vwap_dev_ratio = (_pb_snap.last_price - _pb_vwap) / _pb_vwap if _pb_vwap > 0 else None
                            _pb_prev_close = _pb_snap.prev_close
                            if _pb_prev_close <= 0 and _pb['kline'] is not None and len(_pb['kline']) >= 1:
                                _pb_prev_close = float(_pb['kline']["close"].iloc[-1])
                            _pb_sym_change = (
                                (_pb_snap.last_price - _pb_prev_close) / _pb_prev_close
                                if _pb_prev_close > 0 else None
                            )
                            _pb_d5, _pb_d15, _pb_vel = _calc_direction_from_history(_pb_symbol)
                            pnl_tracker.register(
                                _pb_result.order_id, _pb_symbol, "LONG",
                                _pb_size, _pb_snap.last_price,
                                atr_value=_pb_atr_val,
                                atr_pct=_pb_atr_pct,
                                vwap_above=_pb_vwap_above,
                                vwap_price=_pb_vwap,
                                spy_rt=_spy_change,
                                qqq_rt=_qqq_change,
                                spy_rt_open=_spy_change_open,
                                qqq_rt_open=_qqq_change_open,
                                sentiment_score=_pb['sentiment'].score,
                                sentiment_confidence=_pb['sentiment'].confidence,
                                flow_strength=_pb_flow.strength,
                                is_dynamic=_pb_symbol not in settings.WATCHLIST,
                                symbol_change_pct=_pb_sym_change,
                                vwap_deviation_pct=_pb_vwap_dev_ratio,
                                texts_count=_pb.get('texts_count'),
                                sl_price=_pb_levels.stop_loss if _pb_levels else None,
                                tp_price=_pb_levels.take_profit if _pb_levels else None,
                                open_price=_pb_snap.open_price if _pb_snap.open_price > 0 else None,
                                high_price=_pb_snap.high_price if _pb_snap.high_price > 0 else None,
                                low_price=_pb_snap.low_price if _pb_snap.low_price > 0 else None,
                                prev_close=_pb_snap.prev_close if _pb_snap.prev_close > 0 else None,
                                change_from_open_pct=_pb_snap.change_from_open_pct,
                                gap_pct=_pb_snap.gap_pct,
                                price_position_in_range=_pb_snap.price_position_in_range,
                                amplitude=_pb_snap.amplitude if _pb_snap.amplitude > 0 else None,
                                pre_change_rate=_pb_snap.pre_change_rate,
                                volume_ratio=_pb_snap.volume_ratio if _pb_snap.volume_ratio > 0 else None,
                                is_momentum=_pb_symbol in _momentum_added_symbols,
                                direction_5min_pct=_pb_d5,
                                direction_15min_pct=_pb_d15,
                                direction_velocity=_pb_vel,
                            )
                            notifier.notify_entry(
                                _pb_symbol, "LONG", _pb_size, _pb_snap.last_price,
                            )
                        del _pullback_queue[_pb_symbol]

            existing_symbols = {p.symbol for p in order_router.open_positions.values()}

            for symbol in watchlist:
                if _shutdown_requested:
                    break
                try:
                    # 0) 既存ポジションがある銘柄: シグナル状況のみ記録してスキップ
                    if symbol in existing_symbols:
                        # 保有中のシグナル状況を軽量記録 (Claude API 呼ばない)
                        try:
                            _pos = next(
                                (p for p in order_router.open_positions.values()
                                 if p.symbol == symbol), None
                            )
                            if _pos is not None:
                                _ip_flow = flow_detector.get_flow_signal(symbol)
                                _ip_snap = client.get_snapshot(symbol)
                                if _ip_snap and _ip_snap.last_price > 0:
                                    _ip_vwap = _ip_snap.best_vwap or 0.0
                                    _ip_vwap_dev = (
                                        (_ip_snap.last_price - _ip_vwap) / _ip_vwap * 100
                                        if _ip_vwap > 0 else None
                                    )
                                    _ip_minutes = (
                                        datetime.now() - _pos.opened_at
                                    ).total_seconds() / 60
                                    _ip_unrealized = (
                                        (_ip_snap.last_price - _pos.entry_price) * _pos.size
                                        if _pos.direction == "LONG"
                                        else (_pos.entry_price - _ip_snap.last_price) * _pos.size
                                    )
                                    # 当日高安からの乖離
                                    _ip_dist_from_hod = None
                                    _ip_dist_from_lod = None
                                    if _ip_snap.high_price > 0:
                                        _ip_dist_from_hod = (_ip_snap.last_price - _ip_snap.high_price) / _ip_snap.high_price * 100
                                    if _ip_snap.low_price > 0:
                                        _ip_dist_from_lod = (_ip_snap.last_price - _ip_snap.low_price) / _ip_snap.low_price * 100

                                    _log_in_position_signal({
                                        "symbol": symbol,
                                        "time": datetime.now().strftime("%H:%M:%S"),
                                        "in_position_minutes": round(_ip_minutes, 1),
                                        "entry_price": round(_pos.entry_price, 4),
                                        "current_price": round(_ip_snap.last_price, 4),
                                        "unrealized_pnl": round(_ip_unrealized, 2),
                                        # VWAP 関連
                                        "vwap": round(_ip_vwap, 4) if _ip_vwap > 0 else None,
                                        "vwap_above": (
                                            _ip_snap.last_price > _ip_vwap
                                            if _ip_vwap > 0 else None
                                        ),
                                        "vwap_dev_pct": round(_ip_vwap_dev, 3) if _ip_vwap_dev is not None else None,
                                        # 当日レンジ
                                        "amplitude": round(_ip_snap.amplitude, 3) if _ip_snap.amplitude > 0 else None,
                                        "high_price": round(_ip_snap.high_price, 4) if _ip_snap.high_price > 0 else None,
                                        "low_price": round(_ip_snap.low_price, 4) if _ip_snap.low_price > 0 else None,
                                        "dist_from_hod_pct": round(_ip_dist_from_hod, 3) if _ip_dist_from_hod is not None else None,
                                        "dist_from_lod_pct": round(_ip_dist_from_lod, 3) if _ip_dist_from_lod is not None else None,
                                        "change_from_open_pct": (
                                            round(_ip_snap.change_from_open_pct, 3)
                                            if _ip_snap.change_from_open_pct is not None else None
                                        ),
                                        "price_position_in_range": (
                                            round(_ip_snap.price_position_in_range, 3)
                                            if _ip_snap.price_position_in_range is not None else None
                                        ),
                                        # 出来高
                                        "volume_ratio": round(_ip_snap.volume_ratio, 3) if _ip_snap.volume_ratio > 0 else None,
                                        # シグナル
                                        "flow_direction": _ip_flow.direction,
                                        "flow_strength": round(_ip_flow.strength, 3),
                                        # 地合い (ループ先頭でキャッシュ済み)
                                        "spy_change_pct": (
                                            round(_spy_change * 100, 3)
                                            if _spy_change is not None else None
                                        ),
                                        "qqq_change_pct": (
                                            round(_qqq_change * 100, 3)
                                            if _qqq_change is not None else None
                                        ),
                                    })
                        except Exception:
                            logger.debug("[%s] in_position_signal 記録失敗（無視）", symbol, exc_info=True)
                        continue

                    # is_momentum 判定 (シグナル発火後の各フィルタでも参照される)
                    is_momentum = symbol in _momentum_added_symbols

                    # MOMENTUM_ONLY_MODE: モメンタム検知銘柄のみエントリー対象
                    if settings.MOMENTUM_ONLY_MODE and not is_momentum:
                        logger.debug("[%s] MOMENTUM_ONLY_MODE: スキップ", symbol)
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
                    # ENABLE_SHORT=false → dryrun jsonl 記録のみ (分析データ蓄積継続)
                    # ENABLE_SHORT=true + SHORT_DRY_RUN=true → dryrun のみ (実発注なし、 Phase 1 準備)
                    # ENABLE_SHORT=true + SHORT_DRY_RUN=false → 実エントリー + dryrun 並行記録
                    if flow.direction == "SELL":
                        await _short_dryrun(
                            symbol=symbol,
                            flow_strength=flow.strength,
                            board_scraper=board_scraper,
                            news_feed=news_feed,
                            sentiment_analyzer=sentiment_analyzer,
                            client=client,
                            stop_loss=stop_loss_manager,
                            order_router=order_router,
                            spy_change=_spy_change,
                            qqq_change=_qqq_change,
                            spy_change_open=_spy_change_open,
                            qqq_change_open=_qqq_change_open,
                            position_sizer=position_sizer,
                            pnl_tracker=pnl_tracker,
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
                    # 価格履歴に記録 (A1/A3 ルール用 + 後付け検証用 JSONL)
                    if snap.last_price > 0:
                        _record_scan_price(symbol, snap.last_price)
                    if snap.last_price > 0 and snap.last_price > buying_power:
                        logger.info(
                            "[%s] flow=%s(%.2f) price=$%.0f > power=$%.0f -> SKIP(can't afford)",
                            symbol, flow.direction, flow.strength,
                            snap.last_price, buying_power,
                        )
                        continue

                    # vwap事前フィルター (Claude API 呼び出し前にコスト削減)
                    # tight filter A2 と同じ閾値で、API コスト確定で無駄になる銘柄を弾く
                    # モメンタム銘柄は閾値を 2倍 に緩和 (tight_filter と整合)
                    _pre_vwap = snap.best_vwap or (
                        snap.turnover / snap.volume
                        if snap.volume > 0 and snap.turnover > 0 else None
                    )
                    if (
                        settings.TIGHT_FILTER_ENABLED
                        and _pre_vwap
                        and _pre_vwap > 0
                    ):
                        _pre_vwap_dev = (snap.last_price - _pre_vwap) / _pre_vwap * 100
                        _is_momentum_pre = symbol in _momentum_added_symbols
                        _pre_threshold = (
                            settings.TIGHT_VWAP_DEV_PCT * 2 if _is_momentum_pre
                            else settings.TIGHT_VWAP_DEV_PCT
                        )
                        if _pre_vwap_dev > _pre_threshold:
                            logger.info(
                                "[%s] pre-filter: vwap_dev=%.2f%% > %.1f%% → API skip%s",
                                symbol, _pre_vwap_dev, _pre_threshold,
                                " (momentum 緩和)" if _is_momentum_pre else "",
                            )
                            continue

                    # 注: amplitude チェックは tight_filter_long の Filter F に統合
                    # (Claude API 後だが、 dryrun 記録に tight_filter_reason として残るため)

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
                            _is_momentum_for_filter = symbol in _momentum_added_symbols
                            tight_pass, tight_reason = and_filter.tight_filter_long(
                                snapshot, vwap_approx,
                                atr_pct=_atr_pct_for_filter,
                                is_dynamic=_is_dynamic_for_filter,
                                is_momentum=_is_momentum_for_filter,
                            )

                            # Filter D 候補記録 (log-only 期間中のデータ蓄積)
                            # 注: Filter D は実エントリーを阻止しない。記録のみ。
                            today_iso = date.today().isoformat()
                            if (
                                settings.TIGHT_FILTER_ENABLED
                                and _is_dynamic_for_filter
                                and _atr_pct_for_filter is not None
                                and settings.TIGHT_DYN_MID_ATR_LOW <= _atr_pct_for_filter < settings.TIGHT_DYN_MID_ATR_HIGH
                                and _filter_d_recorded.get(symbol) != today_iso
                            ):
                                _filter_d_recorded[symbol] = today_iso
                                _vwap_dev_d = (
                                    (snapshot.last_price - vwap_approx) / vwap_approx
                                    if vwap_approx and vwap_approx > 0 else None
                                )
                                _log_filter_d_event({
                                    "symbol": symbol,
                                    "entry_time": datetime.now().strftime("%H:%M:%S"),
                                    "entry_price": round(snapshot.last_price, 4),
                                    "atr_pct": round(_atr_pct_for_filter, 4),
                                    "vwap_dev": round(_vwap_dev_d, 4) if _vwap_dev_d is not None else None,
                                    "sentiment_score": round(sentiment.score, 3),
                                    "flow_strength": round(flow.strength, 3),
                                    "sl_price": round(levels.stop_loss, 4) if levels else None,
                                    "tp_price": round(levels.take_profit, 4) if levels else None,
                                    "close_price": None,
                                    "close_time": None,
                                    "exit_reason": None,
                                    "virtual_pnl": None,
                                })

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
                                spy_change=_spy_change,
                                qqq_change=_qqq_change,
                                spy_change_open=_spy_change_open,
                                qqq_change_open=_qqq_change_open,
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
                                spy_change=_spy_change,
                                qqq_change=_qqq_change,
                                spy_change_open=_spy_change_open,
                                qqq_change_open=_qqq_change_open,
                            )
                            continue

                        # tight filter 不合格なら実発注せず ログのみ
                        if decision.direction == "LONG" and not tight_pass:
                            logger.info(
                                "[%s] AND pass but TIGHT FILTER REJECTED: %s",
                                symbol, tight_reason,
                            )
                            # IF 分析用: フィルタ無しで実エントリーされていたケースを
                            # 仮想 PnL で追跡 (本番稼働後もフィルタ精度を検証可能)
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
                                dryrun_type="rejected",
                                slot_count_at_signal=order_router.long_count,
                                spy_change=_spy_change,
                                qqq_change=_qqq_change,
                                spy_change_open=_spy_change_open,
                                qqq_change_open=_qqq_change_open,
                            )
                            continue

                        # SPY 地合いフィルタ: 暴落中の LONG エントリーをブロック (案 1)
                        # SPY <-0.5% で SL ヒット率 80%+ (n=62 分析) のため、 暴落中は LONG 控える。
                        # blocked シグナルは rejected dryrun に記録し、 「もし入っていたら」 の
                        # 仮想 pnl を後から評価可能にする (リバウンド取りこぼしの IF 分析)。
                        if (
                            decision.direction == "LONG"
                            and settings.SPY_LONG_BLOCK_THRESHOLD < 0
                            and _spy_change is not None
                            and _spy_change < settings.SPY_LONG_BLOCK_THRESHOLD
                        ):
                            spy_block_reason = (
                                f"SPY_BLOCK: SPY={_spy_change*100:+.2f}% "
                                f"< threshold={settings.SPY_LONG_BLOCK_THRESHOLD*100:+.2f}%"
                            )
                            logger.info(
                                "[%s] SPY 地合いフィルタ BLOCKED: %s",
                                symbol, spy_block_reason,
                            )
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
                                tight_pass=False,
                                tight_reason=spy_block_reason,
                                dryrun_type="rejected",
                                slot_count_at_signal=order_router.long_count,
                                spy_change=_spy_change,
                                qqq_change=_qqq_change,
                                spy_change_open=_spy_change_open,
                                qqq_change_open=_qqq_change_open,
                            )
                            continue

                        # 価格トレンド記録 (毎スキャン更新、 即エントリー時と押し目キュー実行時の参照用)
                        _cur_price = snapshot.last_price
                        _prev_price = _last_scan_prices.get(symbol, _cur_price)
                        _last_scan_prices[symbol] = _cur_price  # 今回の価格を記録

                        # --- 押し目待ち判定 ---
                        # vwap_dev > entry_threshold なら待機キューへ
                        # 通常: 0.5% / モメンタム銘柄: 1.0% (閾値緩和)
                        if (
                            settings.PULLBACK_ENABLED
                            and decision.direction == "LONG"
                            and not in_open_skip
                        ):
                            vwap_dev_pct_entry = (
                                (snapshot.last_price - vwap_approx) / vwap_approx * 100
                                if vwap_approx and vwap_approx > 0 else 0.0
                            )
                            _pb_is_momentum = symbol in _momentum_added_symbols
                            entry_threshold = (
                                settings.MOMENTUM_VWAP_ENTRY_PCT if _pb_is_momentum
                                else settings.PULLBACK_VWAP_ENTRY_PCT
                            )
                            if vwap_dev_pct_entry > entry_threshold:
                                if symbol not in _pullback_queue:
                                    # 注: levels/atr_pct/flow/snapshot は queue 処理時に
                                    # 再取得するため格納しない (古い値で発注しない安全策)
                                    _pullback_queue[symbol] = {
                                        'fired_at': datetime.now(),
                                        'decision': decision,
                                        'sentiment': sentiment,
                                        'vwap_price': vwap_approx,
                                        'kline': kline,
                                        'texts_count': len(texts),
                                        'entry_price_at_signal': snapshot.last_price,
                                        # 反転確認用: vwap_dev の最小値を追跡
                                        'min_vwap_dev': vwap_dev_pct_entry,
                                    }
                                    logger.info(
                                        "[%s] 押し目待ちキュー追加: vwap_dev=%.2f%% > %.1f%% → %d分以内に押し目待ち%s",
                                        symbol, vwap_dev_pct_entry, entry_threshold,
                                        settings.PULLBACK_TIMEOUT_MINUTES,
                                        " (momentum 緩和)" if _pb_is_momentum else "",
                                    )
                                    _log_pullback_event({
                                        "event": "queued",
                                        "symbol": symbol,
                                        "queued_at": datetime.now().strftime("%H:%M:%S"),
                                        "entry_price_at_signal": round(snapshot.last_price, 4),
                                        "vwap_dev_at_signal": round(vwap_dev_pct_entry, 3),
                                        "sentiment_score": round(sentiment.score, 3),
                                        "confidence": round(sentiment.confidence, 3),
                                        "flow_strength": round(flow.strength, 3),
                                        "is_momentum": _pb_is_momentum,
                                        "entry_threshold": entry_threshold,
                                    })
                                continue  # 今は発注しない

                        # --- 即エントリー直前の価格下落チェック ---
                        # vwap_dev は閾値以下 (即エントリーパス) で確定
                        # 直前スキャンから大きく下落していたら見送り (押し目キューにも追加しない)
                        _price_change_pct = (
                            (_cur_price - _prev_price) / _prev_price * 100
                            if _prev_price > 0 else 0.0
                        )
                        if _price_change_pct < -settings.ENTRY_PRICE_DROP_THRESHOLD:
                            logger.info(
                                "[%s] 即エントリー直前 価格下落中(%.2f→%.2f, %+.3f%%) → スキップ",
                                symbol, _prev_price, _cur_price, _price_change_pct,
                            )
                            continue

                        # A1/A3 ルールチェック (即エントリー直前)
                        _ok, _rule_reason = _check_price_trend_rules(symbol)
                        if not _ok:
                            logger.info(
                                "[%s] 即エントリー直前 price trend BLOCK: %s → スキップ",
                                symbol, _rule_reason,
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
                            # ATR を計算（SPY/QQQ はループ先頭でキャッシュ済み）
                            _atr_pct = stop_loss_manager.calc_atr_pct(kline, current_price)
                            _atr_val = current_price * _atr_pct

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

                            _d5, _d15, _vel = _calc_direction_from_history(symbol)
                            pnl_tracker.register(
                                result.order_id, symbol, decision.direction,
                                size, current_price,
                                atr_value=_atr_val,
                                atr_pct=_atr_pct,
                                vwap_above=vwap_above,
                                vwap_price=vwap_approx,
                                spy_rt=_spy_change,
                                qqq_rt=_qqq_change,
                                spy_rt_open=_spy_change_open,
                                qqq_rt_open=_qqq_change_open,
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
                                is_momentum=symbol in _momentum_added_symbols,
                                direction_5min_pct=_d5,
                                direction_15min_pct=_d15,
                                direction_velocity=_vel,
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
