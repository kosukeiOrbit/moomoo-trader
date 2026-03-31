"""バックテスト実行スクリプト.

過去データを使ってAND条件フィルター戦略の性能を検証する。
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from config import settings
from src.signals.and_filter import AndFilter, EntryDecision
from src.signals.sentiment_analyzer import SentimentResult
from src.signals.flow_detector import FlowSignal
from src.risk.position_sizer import PositionSizer, TradeResult
from src.risk.stop_loss import StopLossManager
from src.execution.paper_trade import PaperTradeEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    """バックテスト設定."""
    symbol: str
    start_date: str
    end_date: str
    initial_balance: float = 100_000.0


def run_backtest(config: BacktestConfig) -> dict:
    """バックテストを実行する.

    Args:
        config: バックテスト設定

    Returns:
        パフォーマンス指標
    """
    and_filter = AndFilter()
    position_sizer = PositionSizer()
    stop_loss_manager = StopLossManager()
    paper_engine = PaperTradeEngine(initial_balance=config.initial_balance)

    logger.info(
        "バックテスト開始: %s (%s ~ %s) 初期資金: $%.2f",
        config.symbol, config.start_date, config.end_date, config.initial_balance,
    )

    # TODO: 過去データの読み込み
    # 以下はバックテストフレームワークの骨格
    # 実際のデータソースに接続して実装する

    # 疑似バックテストループ
    # for date in trading_days:
    #     sentiment = load_historical_sentiment(symbol, date)
    #     flow = load_historical_flow(symbol, date)
    #     decision = and_filter.should_enter(sentiment, flow)
    #     if decision.go:
    #         price = get_price(symbol, date)
    #         size = position_sizer.calculate(symbol, price, paper_engine.balance)
    #         position_id = paper_engine.open_position(symbol, decision.direction, size, price)
    #         levels = stop_loss_manager.calculate_levels(symbol, price)
    #         # SL/TP判定でクローズ

    summary = paper_engine.get_summary()
    logger.info("バックテスト完了: %s", summary)

    # パフォーマンス指標
    total_return = summary["total_pnl"] / config.initial_balance
    trade_count = summary["trade_count"]
    win_rate = summary["win_rate"]

    results = {
        "symbol": config.symbol,
        "period": f"{config.start_date} ~ {config.end_date}",
        "initial_balance": config.initial_balance,
        "final_balance": summary["balance"],
        "total_pnl": summary["total_pnl"],
        "total_return": total_return,
        "trade_count": trade_count,
        "win_rate": win_rate,
    }

    logger.info("=== バックテスト結果 ===")
    for key, value in results.items():
        if isinstance(value, float):
            logger.info("  %s: %.4f", key, value)
        else:
            logger.info("  %s: %s", key, value)

    return results


def main() -> None:
    """バックテストのエントリーポイント."""
    parser = argparse.ArgumentParser(description="バックテスト実行")
    parser.add_argument("--symbol", default="AAPL", help="銘柄シンボル")
    parser.add_argument("--start", default="2025-12-01", help="開始日 (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-03-01", help="終了日 (YYYY-MM-DD)")
    parser.add_argument("--balance", type=float, default=100_000.0, help="初期資金")
    args = parser.parse_args()

    config = BacktestConfig(
        symbol=args.symbol,
        start_date=args.start,
        end_date=args.end,
        initial_balance=args.balance,
    )
    run_backtest(config)


if __name__ == "__main__":
    main()
