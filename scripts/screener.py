"""動的スクリーニングスクリプト（引け後に実行）.

毎日 JST 6:30 にタスクスケジューラから実行する。
Finviz で出来高急増銘柄を取得し、moomoo の前日大口フローでスコアリング。
結果を data/watchlist_dynamic.json に保存する。

前提:
    pip install finviz moomoo-openapi python-dotenv
    OpenD が起動していること（大口フロー取得に必要）

使い方:
    python scripts/screener.py
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# プロジェクトルートを sys.path に追加
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import settings

# ログ設定
log_dir = Path(_project_root) / "logs"
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            log_dir / f"screener_{date.today().strftime('%Y%m%d')}.log",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)

# 出力先
DATA_DIR = Path(_project_root) / "data"
OUTPUT_PATH = DATA_DIR / "watchlist_dynamic.json"


def get_previous_trading_day() -> date:
    """前営業日を返す（NYSE休場日・土日を考慮）."""
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        today = date.today()
        schedule = nyse.schedule(
            start_date=(today - timedelta(days=30)).strftime("%Y-%m-%d"),
            end_date=today.strftime("%Y-%m-%d"),
        )
        past_days = [d for d in schedule.index.date.tolist() if d < today]
        if past_days:
            return past_days[-1]
    except ImportError:
        logger.warning("[Screener] pandas-market-calendars 未インストール — 簡易計算にフォールバック")
    except Exception:
        logger.exception("[Screener] 前営業日計算エラー — 簡易計算にフォールバック")

    # フォールバック: 土日のみ考慮
    today = date.today()
    if today.weekday() == 0:
        return today - timedelta(days=3)
    elif today.weekday() == 6:
        return today - timedelta(days=2)
    return today - timedelta(days=1)


def fetch_finviz_candidates(n: int = 50) -> list[str]:
    """Finviz で出来高急増の大型株を取得する."""
    try:
        from finviz.screener import Screener

        filters = [
            "sh_relvol_o2",     # 相対出来高 2倍以上
            "cap_largeover",    # 大型株（NASDAQ + NYSE）
        ]
        stocks = Screener(
            filters=filters,
            table="Overview",
            order="-volume",
        )
        candidates = [s["Ticker"] for s in stocks[:n]]
        logger.info("[Screener] Finviz: %d銘柄取得", len(candidates))
        return candidates

    except ImportError:
        logger.error("[Screener] finviz 未インストール: pip install finviz")
        return []
    except Exception:
        logger.exception("[Screener] Finviz 取得エラー")
        return []


def score_by_moomoo_flow(candidates: list[str]) -> list[tuple[str, float]]:
    """moomoo の前日大口フローでスコアリングする."""
    import socket

    # OpenD 接続チェック
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        result = sock.connect_ex((settings.MOOMOO_HOST, settings.MOOMOO_PORT))
        if result != 0:
            logger.warning("[Screener] OpenD 未起動 — Finviz 出来高順で代替")
            return []
    finally:
        sock.close()

    try:
        from futu import OpenQuoteContext, RET_OK, PeriodType

        ctx = OpenQuoteContext(
            host=settings.MOOMOO_HOST,
            port=settings.MOOMOO_PORT,
        )

        yesterday = get_previous_trading_day()
        yesterday_str = yesterday.strftime("%Y-%m-%d")
        logger.info(
            "[Screener] moomoo flow確認: %d銘柄 対象日=%s (約%d秒)",
            len(candidates), yesterday_str, len(candidates),
        )

        scored: list[tuple[str, float]] = []

        for i, symbol in enumerate(candidates):
            try:
                code = f"US.{symbol}"
                ret, data = ctx.get_capital_flow(
                    code,
                    period_type=PeriodType.DAY,
                    start=yesterday_str,
                    end=yesterday_str,
                )
                if ret == RET_OK and not data.empty:
                    in_flow = float(data["in_flow"].sum()) if "in_flow" in data.columns else 0.0
                    if in_flow > 0:
                        scored.append((symbol, in_flow))
                        logger.debug("[%s] in_flow=%.0f", symbol, in_flow)
            except Exception:
                logger.debug("[Screener] フロー取得失敗: %s", symbol)

            # レート制限対策: 1秒スリープ
            time.sleep(1.0)

            # 進捗ログ（10銘柄ごと）
            if (i + 1) % 10 == 0:
                logger.info("[Screener] 進捗: %d/%d", i + 1, len(candidates))

        ctx.close()

        logger.info(
            "[Screener] フロー確認完了: %d/%d銘柄がプラスフロー",
            len(scored), len(candidates),
        )
        return scored

    except ImportError:
        logger.error("[Screener] futu パッケージ未インストール")
        return []
    except Exception:
        logger.exception("[Screener] moomoo フロー取得エラー")
        return []


def save_results(symbols: list[str]) -> None:
    """結果を JSON に保存する."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "symbols": symbols,
    }
    OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
    logger.info("[Screener] 保存: %s (%d銘柄)", OUTPUT_PATH, len(symbols))


def main() -> None:
    """メイン処理."""
    logger.info("=" * 50)
    logger.info("[Screener] 動的スクリーニング開始")
    logger.info("=" * 50)

    max_symbols = settings.SCREENER_MAX_SYMBOLS

    # 1) Finviz で候補取得
    candidates = fetch_finviz_candidates(n=settings.SCREENER_CANDIDATES)
    if not candidates:
        logger.warning("[Screener] Finviz 取得失敗 — 空の結果を保存")
        save_results([])
        return

    # 2) moomoo でスコアリング
    scored = score_by_moomoo_flow(candidates)

    if scored:
        # フロースコア上位
        scored.sort(key=lambda x: x[1], reverse=True)
        top_symbols = [sym for sym, _ in scored[:max_symbols]]
    else:
        # moomoo 接続失敗時は Finviz 出来高順
        logger.info("[Screener] moomoo フローなし — Finviz 出来高順を使用")
        top_symbols = candidates[:max_symbols]

    # 3) 保存
    save_results(top_symbols)

    logger.info("[Screener] 結果: %s", " ".join(top_symbols))
    logger.info("[Screener] 完了")


if __name__ == "__main__":
    main()
