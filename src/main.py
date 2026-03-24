"""エントリーポイント・メインループ."""

from __future__ import annotations

import asyncio
import logging
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
from src.risk.position_sizer import PositionSizer
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


def market_is_open() -> bool:
    """米国市場が開いているかどうかを判定する."""
    now = datetime.now(ET)
    market_open = time(9, 30)
    market_close = time(16, 0)
    # 土日はスキップ
    if now.weekday() >= 5:
        return False
    return market_open <= now.time() <= market_close


async def main_loop() -> None:
    """メインループ: データ収集 → シグナル生成 → リスク計算 → 発注."""
    # モジュール初期化
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

    logger.info("=== moomoo AI Daytrade Bot 起動 (env=%s) ===", settings.TRADE_ENV)

    try:
        while True:
            if not market_is_open():
                logger.info("市場は閉まっています。次のチェックまで待機...")
                await asyncio.sleep(60)
                continue

            # サーキットブレーカーチェック
            account_state = AccountState(
                balance=100_000,  # TODO: 実際の残高を取得
                daily_pnl=pnl_tracker.daily_pnl,
                peak_balance=pnl_tracker.peak_balance,
                consecutive_losses=0,
            )
            breaker_status = circuit_breaker.check(account_state)
            if not breaker_status.can_trade:
                logger.warning("サーキットブレーカー発動: %s", breaker_status.reason)
                await notifier.notify_circuit_breaker(breaker_status.reason)
                if breaker_status.action == BreakerAction.FORCE_CLOSE_ALL:
                    # 全ポジション強制決済
                    for oid in list(order_router.open_positions.keys()):
                        order_router.exit(oid, "サーキットブレーカー")
                    break
                await asyncio.sleep(settings.LOOP_INTERVAL_SECONDS)
                continue

            for symbol in settings.WATCHLIST:
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
                        current_price = 100.0  # TODO: 実際の株価を取得
                        size = position_sizer.calculate(symbol, current_price, account_state.balance)
                        levels = stop_loss_manager.calculate_levels(symbol, current_price)
                        result = order_router.enter(decision, symbol, size, current_price, levels)
                        if result and result.status != "FAILED":
                            pnl_tracker.register(
                                result.order_id, symbol, decision.direction,
                                size, current_price,
                            )
                            await notifier.notify_entry(symbol, decision.direction, size, current_price)
                except Exception:
                    logger.exception("銘柄 %s の処理でエラー", symbol)

            await asyncio.sleep(settings.LOOP_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        logger.info("ユーザーによる停止")
    finally:
        await board_scraper.close()
        await news_feed.close()
        client.close()
        summary = pnl_tracker.get_summary()
        await notifier.notify_daily_summary(summary)
        logger.info("=== Bot 停止 === 日次PnL: %.2f", pnl_tracker.daily_pnl)


def main() -> None:
    """エントリーポイント."""
    asyncio.run(main_loop())


if __name__ == "__main__":
    main()
