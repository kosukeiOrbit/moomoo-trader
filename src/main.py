"""エントリーポイント・メインループ.

全モジュールを統合し、市場オープン中にデイトレードを自動実行する。
05:50 JST (= 15:50 ET) に未決済ポジションを全決済して安全に終了する。
Ctrl+C で安全にシャットダウンできる。
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from datetime import datetime, time
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
    log_dir = Path("logs")
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

def market_is_open() -> bool:
    """米国市場がオープンしているか判定する.

    ZoneInfo("America/New_York") は DST/EST を自動判定するため
    サマータイム切り替え時もコード変更は不要。
    """
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


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
    client.connect()
    client.subscribe_realtime(settings.WATCHLIST)

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

    # ポジション監視タスク
    monitor_task = asyncio.create_task(order_router.monitor_positions())

    logger.info(
        "=== moomoo AI Daytrade Bot 起動 (env=%s, symbols=%s) ===",
        settings.TRADE_ENV, settings.WATCHLIST,
    )

    try:
        while not _shutdown_requested:
            # --- ET 15:50 強制決済 ---
            if should_force_exit() and order_router.position_count > 0:
                logger.warning("ET 15:50 — Force closing all positions")
                order_router.exit_all("ET 15:50 force close")
                notifier.notify_circuit_breaker("ET 15:50 all positions force-closed")
                break

            # --- 市場クローズ中は待機 ---
            if not market_is_open():
                logger.info("Market closed. Waiting 60s...")
                await asyncio.sleep(60)
                continue

            # --- 口座状態を取得してサーキットブレーカーチェック ---
            balance = client.get_account_balance() or 100_000.0
            pnl_tracker.update_peak_balance(balance)

            account_state = AccountState(
                balance=balance,
                daily_pnl=pnl_tracker.daily_pnl,
                peak_balance=pnl_tracker.peak_balance,
                consecutive_losses=position_sizer.consecutive_losses,
            )

            breaker_status = circuit_breaker.check(account_state)
            if not breaker_status.can_trade:
                logger.warning("サーキットブレーカー: %s", breaker_status.reason)
                notifier.notify_circuit_breaker(breaker_status.reason)
                if breaker_status.action == BreakerAction.FORCE_CLOSE_ALL:
                    order_router.exit_all("Circuit breaker: force close")
                    break
                await asyncio.sleep(settings.LOOP_INTERVAL_SECONDS)
                continue

            # --- 銘柄ごとのスキャンループ ---
            for symbol in settings.WATCHLIST:
                if _shutdown_requested:
                    break
                try:
                    # 1) フロー先行取得（API不要・低コスト）
                    flow = flow_detector.get_flow_signal(symbol)

                    # 2) flow=SELL ならClaude APIをスキップ
                    if flow.direction != "BUY":
                        logger.info(
                            "[%s] flow=%s(%.2f) -> SKIP(flow not BUY, API skipped)",
                            symbol, flow.direction, flow.strength,
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

                    logger.info(
                        "[%s] texts=%d sentiment=%.2f conf=%.2f flow=%s(%.2f) -> %s",
                        symbol, len(texts), sentiment.score, sentiment.confidence,
                        flow.direction, flow.strength,
                        "ENTRY" if decision.go else f"SKIP({decision.reason[:50]})",
                    )

                    if decision.go:
                        snapshot = client.get_snapshot(symbol)
                        current_price = snapshot.last_price
                        if current_price <= 0:
                            continue

                        size = position_sizer.calculate(
                            symbol, current_price, account_state.balance,
                        )
                        levels = stop_loss_manager.calculate_levels(
                            symbol, current_price,
                        )
                        result = order_router.enter(
                            decision, symbol, size, current_price, levels,
                        )
                        if result and result.status != "FAILED":
                            logger.info(
                                "[%s] ENTRY %s %d shares @ $%.2f (order=%s)",
                                symbol, decision.direction, size, current_price, result.order_id,
                            )
                            pnl_tracker.register(
                                result.order_id, symbol, decision.direction,
                                size, current_price,
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

            await asyncio.sleep(settings.LOOP_INTERVAL_SECONDS)

    finally:
        # --- クリーンアップ ---
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
