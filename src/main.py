"""エントリーポイント・メインループ.

全モジュールを統合し、市場オープン中にデイトレードを自動実行する。
05:50 JST (= 15:50 ET) に未決済ポジションを全決済して安全に終了する。
Ctrl+C で安全にシャットダウンできる。
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

from config import settings
from src.data.moomoo_client import MoomooClient
from src.data.board_scraper import BoardScraper
from src.data.news_feed import NewsFeed
from src.signal.sentiment_analyzer import SentimentAnalyzer
from src.signal.flow_detector import FlowDetector
from src.signal.and_filter import AndFilter
from src.risk.position_sizer import PositionSizer, TradeResult
from src.risk.stop_loss import StopLossManager
from src.risk.circuit_breaker import CircuitBreaker, AccountState, BreakerAction
from src.execution.order_router import OrderRouter
from src.monitor.pnl_tracker import PnLTracker
from src.monitor.notifier import Notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
JST = ZoneInfo("Asia/Tokyo")

# 市場時間
MARKET_OPEN = time(9, 30)   # ET
MARKET_CLOSE = time(16, 0)  # ET

# 全ポジション強制決済時刻 (JST 05:50 = ET 15:50)
FORCE_EXIT_JST = time(5, 50)


# ---------------------------------------------------------------------------
# 市場判定
# ---------------------------------------------------------------------------

def market_is_open() -> bool:
    """米国市場がオープンしているか判定する."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def should_force_exit() -> bool:
    """全ポジション強制決済の時刻かどうか (JST 05:50)."""
    now_jst = datetime.now(JST)
    return now_jst.time() >= FORCE_EXIT_JST


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
    paper_trade = settings.TRADE_ENV == "SIMULATE"
    order_router = OrderRouter(client, circuit_breaker, paper_trade=paper_trade)
    pnl_tracker = PnLTracker()
    notifier = Notifier()

    # ポジション監視タスク
    monitor_task = asyncio.create_task(order_router.monitor_positions())

    logger.info(
        "=== moomoo AI Daytrade Bot 起動 (env=%s, symbols=%s) ===",
        settings.TRADE_ENV, settings.WATCHLIST,
    )

    try:
        while not _shutdown_requested:
            # --- 05:50 JST 強制決済 ---
            if should_force_exit() and order_router.position_count > 0:
                logger.warning("05:50 JST — 全ポジション強制決済")
                results = order_router.exit_all("05:50 JST 強制決済")
                for r in results:
                    pnl_tracker.close_trade(r.order_id, 0.0, "05:50 JST 強制決済")
                notifier.notify_circuit_breaker("05:50 JST 全ポジション強制決済")
                break

            # --- 市場クローズ中は待機 ---
            if not market_is_open():
                logger.debug("市場クローズ中。60秒後に再チェック...")
                await asyncio.sleep(60)
                continue

            # --- 口座状態を取得してサーキットブレーカーチェック ---
            balance = client.get_account_balance() or 100_000.0
            pnl_tracker.update_peak_balance(balance)

            account_state = AccountState(
                balance=balance,
                daily_pnl=pnl_tracker.daily_pnl,
                peak_balance=pnl_tracker.peak_balance,
                consecutive_losses=0,
            )

            breaker_status = circuit_breaker.check(account_state)
            if not breaker_status.can_trade:
                logger.warning("サーキットブレーカー: %s", breaker_status.reason)
                notifier.notify_circuit_breaker(breaker_status.reason)
                if breaker_status.action == BreakerAction.FORCE_CLOSE_ALL:
                    order_router.exit_all("サーキットブレーカー")
                    break
                await asyncio.sleep(settings.LOOP_INTERVAL_SECONDS)
                continue

            # --- 銘柄ごとのスキャンループ ---
            for symbol in settings.WATCHLIST:
                if _shutdown_requested:
                    break
                try:
                    # データ収集
                    posts = await board_scraper.fetch_posts(symbol)
                    news_articles = await news_feed.get_latest(symbol)
                    texts = [p.text for p in posts] + [
                        f"{a.title} {a.body}" for a in news_articles
                    ]

                    # シグナル生成
                    sentiment = sentiment_analyzer.analyze(texts, symbol)
                    flow = flow_detector.get_flow_signal(symbol)
                    decision = and_filter.should_enter(sentiment, flow)

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
