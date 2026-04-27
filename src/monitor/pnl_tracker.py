"""リアルタイムP&L記録モジュール.

トレードごとの記録・日次サマリー計算・CSV保存を担当する。
PostgreSQL未接続でもCSVファイルのみで動作する。
"""

from __future__ import annotations

import csv
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# CSV保存先のデフォルトディレクトリ
DEFAULT_CSV_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "trades"


@dataclass
class TradeRecord:
    """トレード記録."""

    order_id: str
    symbol: str
    direction: str  # "LONG" or "SHORT"
    size: int
    entry_price: float
    exit_price: float | None = None
    pnl: float = 0.0
    reason: str = ""
    opened_at: datetime = field(default_factory=datetime.now)
    closed_at: datetime | None = None
    atr_value: float | None = None
    atr_pct: float | None = None
    vwap_above: bool | None = None
    vwap_price: float | None = None
    spy_rt: float | None = None
    mfe: float = 0.0
    mae: float = 0.0
    sentiment_score: float | None = None
    sentiment_confidence: float | None = None
    flow_strength: float | None = None
    commission: float = 0.0         # 往復手数料（ドル）
    is_dynamic: bool | None = None  # True=スクリーナー由来, False=固定WATCHLIST


class PnLTracker:
    """P&Lトラッカー: トレードの記録・集計・CSV保存を行う."""

    CSV_HEADER = [
        "order_id", "symbol", "direction", "size",
        "entry_price", "exit_price", "pnl", "reason",
        "opened_at", "closed_at", "hold_minutes",
        "atr_value", "atr_pct",
        "vwap_above", "vwap_price",
        "spy_rt",
        "mfe", "mae",
        "sentiment_score", "sentiment_confidence",
        "flow_strength", "commission", "net_pnl", "is_dynamic",
    ]

    def __init__(self, csv_dir: Path | str | None = None) -> None:
        self._open_trades: dict[str, TradeRecord] = {}
        self._closed_trades: list[TradeRecord] = []
        self._closed_ids: set[str] = set()  # 決済済み order_id（重複決済防止）
        self._daily_pnl: float = 0.0
        self._peak_balance: float = 0.0
        self._csv_dir = Path(csv_dir) if csv_dir else DEFAULT_CSV_DIR

    # ------------------------------------------------------------------
    # トレード登録・決済
    # ------------------------------------------------------------------

    def register(
        self,
        order_id: str,
        symbol: str,
        direction: str,
        size: int,
        entry_price: float,
        atr_value: float | None = None,
        atr_pct: float | None = None,
        vwap_above: bool | None = None,
        vwap_price: float | None = None,
        spy_rt: float | None = None,
        sentiment_score: float | None = None,
        sentiment_confidence: float | None = None,
        flow_strength: float | None = None,
        is_dynamic: bool | None = None,
    ) -> None:
        """新規トレードを記録する（重複登録は無視）."""
        if order_id in self._open_trades:
            logger.debug("トレード登録スキップ（既にオープン中）: %s", order_id)
            return
        if order_id in self._closed_ids:
            logger.debug("トレード登録スキップ（決済済み）: %s", order_id)
            return
        self._open_trades[order_id] = TradeRecord(
            order_id=order_id,
            symbol=symbol,
            direction=direction,
            size=size,
            entry_price=entry_price,
            atr_value=atr_value,
            atr_pct=atr_pct,
            vwap_above=vwap_above,
            vwap_price=vwap_price,
            spy_rt=spy_rt,
            sentiment_score=sentiment_score,
            sentiment_confidence=sentiment_confidence,
            flow_strength=flow_strength,
            is_dynamic=is_dynamic,
        )
        logger.info(
            "トレード記録: %s %s %s %d株 @ %.2f",
            order_id, direction, symbol, size, entry_price,
        )

    def close_trade(
        self,
        order_id: str,
        exit_price: float,
        reason: str = "",
        mfe: float = 0.0,
        mae: float = 0.0,
    ) -> float:
        """トレードを決済記録する.

        Args:
            order_id: 注文ID
            exit_price: 決済価格
            reason: 決済理由 (SL / TP / センチメント反転 等)

        Returns:
            損益
        """
        # 重複決済防止
        if order_id in self._closed_ids:
            logger.warning("重複決済スキップ（既に決済済み）: %s", order_id)
            return 0.0

        trade = self._open_trades.pop(order_id, None)
        if trade is None:
            logger.warning("オープントレードが見つかりません: %s", order_id)
            return 0.0

        if trade.direction == "LONG":
            pnl = (exit_price - trade.entry_price) * trade.size
        else:
            pnl = (trade.entry_price - exit_price) * trade.size

        trade.exit_price = exit_price
        trade.pnl = pnl
        trade.reason = reason
        trade.closed_at = datetime.now()
        trade.mfe = mfe

        # 手数料計算（moomoo日本: 約定代金の0.132%(税込), 上限$22, 最低$0.01, 往復）
        entry_comm = min(trade.entry_price * trade.size * 0.00132, 22.0)
        exit_comm = min(exit_price * trade.size * 0.00132, 22.0)
        entry_comm = max(entry_comm, 0.01)
        exit_comm = max(exit_comm, 0.01)
        trade.commission = round(entry_comm + exit_comm, 2)
        trade.mae = mae
        self._closed_trades.append(trade)
        self._closed_ids.add(order_id)
        self._daily_pnl += pnl

        logger.info(
            "トレード決済: %s PnL=%.2f reason=%s 日次合計=%.2f",
            order_id, pnl, reason, self._daily_pnl,
        )
        return pnl

    # ------------------------------------------------------------------
    # プロパティ
    # ------------------------------------------------------------------

    @property
    def daily_pnl(self) -> float:
        """当日の累計損益."""
        return self._daily_pnl

    @property
    def total_pnl(self) -> float:
        """全期間の累計損益."""
        return sum(t.pnl for t in self._closed_trades)

    @property
    def open_trade_count(self) -> int:
        """オープンポジション数."""
        return len(self._open_trades)

    @property
    def peak_balance(self) -> float:
        """ピーク残高."""
        return self._peak_balance

    @property
    def closed_trade_count(self) -> int:
        """決済済みトレード数."""
        return len(self._closed_trades)

    # ------------------------------------------------------------------
    # ピーク残高
    # ------------------------------------------------------------------

    def update_peak_balance(self, current_balance: float) -> None:
        """ピーク残高を更新する."""
        if current_balance > self._peak_balance:
            self._peak_balance = current_balance

    # ------------------------------------------------------------------
    # 勝率
    # ------------------------------------------------------------------

    def get_win_rate(self, last_n: int | None = None) -> float:
        """勝率を返す.

        Args:
            last_n: 直近Nトレードで計算（Noneなら全期間）

        Returns:
            勝率 (0.0〜1.0)
        """
        trades = self._closed_trades
        if last_n is not None and last_n > 0:
            trades = trades[-last_n:]
        if not trades:
            return 0.0
        wins = sum(1 for t in trades if t.pnl > 0)
        return wins / len(trades)

    # ------------------------------------------------------------------
    # 最大ドローダウン
    # ------------------------------------------------------------------

    def get_max_drawdown(self) -> float:
        """決済済みトレードの累積損益から最大ドローダウン率を計算する.

        Returns:
            最大ドローダウン率 (0.0〜1.0)
        """
        if not self._closed_trades:
            return 0.0

        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0

        for trade in self._closed_trades:
            cumulative += trade.pnl
            if cumulative > peak:
                peak = cumulative
            if peak > 0:
                dd = (peak - cumulative) / peak
                if dd > max_dd:
                    max_dd = dd

        return max_dd

    # ------------------------------------------------------------------
    # シャープレシオ
    # ------------------------------------------------------------------

    def get_sharpe_ratio(self) -> float:
        """決済済みトレードのシャープレシオを計算する.

        Sharpe = mean(pnl) / std(pnl)  (リスクフリーレート=0と仮定)

        Returns:
            シャープレシオ（トレード2件未満は0.0）
        """
        if len(self._closed_trades) < 2:
            return 0.0

        pnls = [t.pnl for t in self._closed_trades]
        mean_pnl = sum(pnls) / len(pnls)
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
        std_pnl = math.sqrt(variance)

        if std_pnl == 0:
            return 0.0
        return mean_pnl / std_pnl

    # ------------------------------------------------------------------
    # 日次サマリー
    # ------------------------------------------------------------------

    def get_daily_summary(self) -> dict:
        """当日のサマリーを返す."""
        today = date.today()
        today_trades = [
            t for t in self._closed_trades
            if t.closed_at is not None and t.closed_at.date() == today
        ]
        wins = sum(1 for t in today_trades if t.pnl > 0)
        total = len(today_trades)

        return {
            "date": today.isoformat(),
            "daily_pnl": self._daily_pnl,
            "total_trades": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": wins / max(total, 1),
            "max_drawdown": self.get_max_drawdown(),
            "sharpe_ratio": self.get_sharpe_ratio(),
            "open_positions": self.open_trade_count,
        }

    # ------------------------------------------------------------------
    # リセット
    # ------------------------------------------------------------------

    def reset_daily(self) -> None:
        """日次リセット."""
        self._daily_pnl = 0.0
        logger.info("日次P&Lをリセットしました")

    # ------------------------------------------------------------------
    # CSV保存・読み込み
    # ------------------------------------------------------------------

    def save_to_csv(self, filename: str | None = None) -> Path:
        """決済済みトレードをCSVファイルに保存する.

        Args:
            filename: ファイル名（Noneなら trades_YYYY-MM-DD.csv）

        Returns:
            保存先のパス
        """
        self._csv_dir.mkdir(parents=True, exist_ok=True)
        if filename is None:
            filename = f"trades_{date.today().isoformat()}.csv"
        filepath = self._csv_dir / filename

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(self.CSV_HEADER)
            for t in self._closed_trades:
                hold_minutes = None
                if t.opened_at and t.closed_at:
                    hold_minutes = round(
                        (t.closed_at - t.opened_at).total_seconds() / 60, 1,
                    )
                writer.writerow([
                    t.order_id,
                    t.symbol,
                    t.direction,
                    t.size,
                    f"{t.entry_price:.4f}",
                    f"{t.exit_price:.4f}" if t.exit_price is not None else "",
                    f"{t.pnl:.4f}",
                    t.reason,
                    t.opened_at.isoformat(),
                    t.closed_at.isoformat() if t.closed_at else "",
                    hold_minutes,
                    f"{t.atr_value:.4f}" if t.atr_value else "",
                    f"{t.atr_pct:.4f}" if t.atr_pct else "",
                    t.vwap_above if t.vwap_above is not None else "",
                    f"{t.vwap_price:.4f}" if t.vwap_price else "",
                    f"{t.spy_rt:.4f}" if t.spy_rt is not None else "",
                    f"{t.mfe:.2f}" if t.mfe else "",
                    f"{t.mae:.2f}" if t.mae else "",
                    f"{t.sentiment_score:.2f}" if t.sentiment_score is not None else "",
                    f"{t.sentiment_confidence:.2f}" if t.sentiment_confidence is not None else "",
                    f"{t.flow_strength:.2f}" if t.flow_strength is not None else "",
                    f"{t.commission:.2f}" if t.commission else "",
                    f"{t.pnl - t.commission:.2f}" if t.commission else "",
                    t.is_dynamic if t.is_dynamic is not None else "",
                ])

        logger.info("CSVに保存: %s (%d件)", filepath, len(self._closed_trades))
        return filepath

    def load_from_csv(self, filepath: Path | str) -> int:
        """CSVファイルからトレード履歴を読み込む.

        Args:
            filepath: CSVファイルパス

        Returns:
            読み込んだレコード数
        """
        filepath = Path(filepath)
        if not filepath.exists():
            logger.warning("CSVファイルが見つかりません: %s", filepath)
            return 0

        count = 0
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trade = TradeRecord(
                    order_id=row["order_id"],
                    symbol=row["symbol"],
                    direction=row["direction"],
                    size=int(row["size"]),
                    entry_price=float(row["entry_price"]),
                    exit_price=float(row["exit_price"]) if row["exit_price"] else None,
                    pnl=float(row["pnl"]),
                    reason=row.get("reason", ""),
                    opened_at=datetime.fromisoformat(row["opened_at"]),
                    closed_at=(
                        datetime.fromisoformat(row["closed_at"])
                        if row.get("closed_at") else None
                    ),
                )
                self._closed_trades.append(trade)
                count += 1

        logger.info("CSVから読み込み: %s (%d件)", filepath, count)
        return count
