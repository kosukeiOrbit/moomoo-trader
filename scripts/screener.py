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

import requests

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
CANDIDATES_PATH = DATA_DIR / "momentum_candidates.json"
# 8/28 追加: Finviz スクリーニング時に銘柄→GICS セクター ETF の動的マップを生成
# main.py が起動時に読み込んで静的 GICS_SECTOR_ETF (settings.py) にマージ、
# 未マップ銘柄をゼロにする恒久対策
SECTOR_MAP_PATH = DATA_DIR / "dynamic_sector_map.json"

# Finviz sector フィルタキー → SPDR セクター ETF のマッピング
# スクリーナーが銘柄取得時に自動でセクターを判定するために使用
FINVIZ_SECTOR_TO_ETF: dict[str, str] = {
    "sec_technology": "XLK",
    "sec_communicationservices": "XLC",
    "sec_healthcare": "XLV",
    "sec_financial": "XLF",
    "sec_consumercyclical": "XLY",       # Consumer Discretionary
    "sec_consumerdefensive": "XLP",      # Consumer Staples (現状 TARGET_SECTORS に無し、 将来用)
    "sec_industrials": "XLI",
    "sec_energy": "XLE",
    "sec_basicmaterials": "XLB",
    "sec_realestate": "XLRE",
    "sec_utilities": "XLU",
}

# 除外リスト（低ボラ・AI無関係・投機的銘柄）
EXCLUDE_SYMBOLS = {
    "RGTI", "VALE",
    "T", "VZ",       # 低ボラ通信株
    "WMT", "KO", "PG",  # 超低ボラ生活必需品
    "JNJ",            # 低ボラヘルスケア
    "SLB",            # エネルギーサービス
}

# スクリーニング対象セクター（Finviz フィルタキー）
# 8/19 再拡張: 2 → 5 セクター (auto-daytrade shadow データ拡充のため)
# 目的: shadow 検証を「業種上昇日 = LONG」 の auto-daytrade 哲学どおり全業種で検証したい。
#   XLK 下落日でも XLV/XLY/XLF が上昇していれば対応銘柄で shadow 発火させたい。
# 実発注リスクの想定: 現行 Filter F (amp>=3.5%) / AND filter (sentiment>0.6) が
#   strict なので、 defensive セクター銘柄の実売すり抜けは限定的な想定。
# revert 基準: 1-2 週間運用で以下発生時に 2 セクターに戻す:
#   - defensive セクター銘柄 (consumer / healthcare / financial の non-tech) で
#     実売 n>=3 の負け発生 → 8/11 削減の再現と判断
#   - shadow の tech/comm 中心の signal 数が有意に減る (対象銘柄が薄まる)
# 8/11 縮小時の根拠 (動的 WL 由来 dryrun+実売 n=217):
#   consumer (communication) : n=10 net -$200 avg -$19.99 ← 主に CMCSA/DIS/WBD 由来
#   healthcare               : n=4 net -$22 (n 小)
#   financial                : 実売 0 件 (低 amp で AND filter 通らず)
# ↑ このパターンが再現するか観察しつつ、 shadow データを蓄積する
TARGET_SECTORS = [
    "sec_technology",
    "sec_communicationservices",
    "sec_healthcare",              # 8/19 再追加 (shadow 用、 XLV 上昇日カバー)
    "sec_financial",               # 8/19 再追加 (shadow 用、 XLF カバー)
    "sec_consumercyclical",        # 8/19 新規 (shadow 用、 XLY = Consumer Discretionary カバー)
]

# 8/28 追加: スクリーニング中に蓄積する銘柄→ETF マップ (fetch_finviz_candidates が書き込み)
_sector_map: dict[str, str] = {}


def save_sector_map() -> None:
    """スクリーニング中に蓄積した銘柄→ETF マップを保存.

    main.py が起動時に読み込んで静的 GICS_SECTOR_ETF (settings.py) にマージする。
    これにより Finviz が新規銘柄を選ぶたびに発生していた「未マップ」 問題を恒久解消。
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "sector_map": _sector_map,
        }
        SECTOR_MAP_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info(
            "[Screener] 動的 sector map 保存: %s (%d 銘柄)",
            SECTOR_MAP_PATH, len(_sector_map),
        )
    except Exception:
        logger.exception("[Screener] sector map 保存失敗")


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
    """Finviz で S&P500 の対象セクターから出来高順に候補を取得する.

    テクノロジー・通信・ヘルスケア・金融の4セクターを個別にフェッチし、
    出来高順で統合して上位N件を返す。

    副作用: 銘柄→GICS セクター ETF の動的マップを `_sector_map` に蓄積し、
    save_results 呼び出し時に data/dynamic_sector_map.json に保存する
    (8/28 追加、 未マップ銘柄ゼロの恒久対策)。
    """
    try:
        from finviz.screener import Screener

        base_filters = [
            "sh_avgvol_o500",   # 平均出来高50万株以上
            "cap_midover",      # 中型株以上
            "sh_price_o10",     # 株価$10以上
            "geo_usa",          # 米国籍企業のみ
            "idx_sp500",        # S&P500構成銘柄
        ]

        all_tickers: list[str] = []
        for sector in TARGET_SECTORS:
            # 8/28 追加: このセクターの ETF を判定 (未定義なら None)
            etf = FINVIZ_SECTOR_TO_ETF.get(sector)
            if not etf:
                logger.warning(
                    "[Screener] Finviz sector %s に ETF マッピングなし、 dynamic_sector_map から除外",
                    sector,
                )
            try:
                stocks = Screener(
                    filters=base_filters + [sector],
                    table="Overview",
                    order="-volume",
                )
                # 7/23 修正: Finviz の HTML カラム構造変更で全カラムが 1 個ずつシフト。
                # 旧: s["Ticker"] が実 Ticker → 現在は先頭 1 文字 (例: SMCI→'S', NVDA→'N')
                # 新: s["Company"] に実 Ticker が入る (7/22 手動確認済み)
                # safety net: 単文字 (旧バグ) や空文字は除外
                tickers = []
                for s in stocks:
                    t = s.get("Company", "").strip()
                    if not t or len(t) < 2:
                        continue  # 1 文字以下は Ticker として無効
                    if not t.replace(".", "").replace("-", "").isalnum():
                        continue  # 記号混入等の異常データ除外
                    if t in EXCLUDE_SYMBOLS:
                        continue
                    tickers.append(t)
                    # 8/28 追加: 動的 sector map に登録 (Finviz 由来の権威データ)
                    if etf:
                        _sector_map[t] = etf
                logger.info("[Screener] Finviz %s (%s): %d銘柄",
                            sector, etf or "no-etf", len(tickers))
                all_tickers.extend(tickers)
            except Exception:
                logger.warning("[Screener] Finviz %s 取得失敗", sector)

        # 重複除去 + 固定WATCHLIST除外（出来高順を維持）
        fixed = set(settings.WATCHLIST)
        seen: set[str] = set()
        candidates: list[str] = []
        for t in all_tickers:
            if t not in seen and t not in fixed:
                seen.add(t)
                candidates.append(t)

        candidates = candidates[:n]
        logger.info(
            "[Screener] Finviz 合計: %d銘柄 (%d セクター、 sector_map %d 銘柄)",
            len(candidates), len(TARGET_SECTORS), len(_sector_map),
        )
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

                # 前日騰落率チェック（急落銘柄を除外）
                ret_kl, kline = ctx.get_cur_kline(code, 2, ktype="K_DAY")
                if ret_kl == RET_OK and len(kline) >= 2:
                    prev_close = float(kline["close"].iloc[-2])
                    last_close = float(kline["close"].iloc[-1])
                    if prev_close > 0:
                        change_pct = (last_close - prev_close) / prev_close * 100
                        if change_pct < settings.SCREENER_MAX_DROP_PCT:
                            logger.info(
                                "[Screener] %s 急落除外: %.1f%%", symbol, change_pct,
                            )
                            time.sleep(1.0)
                            continue

                # 大口フロー取得
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


# ---------------------------------------------------------------------------
# リトライ + Discord アラート (7/25 追加)
# ---------------------------------------------------------------------------

MAX_RETRIES = 3           # Finviz 取得試行回数 (最初 + リトライ 2 回)
RETRY_BACKOFF_SEC = 60    # 60秒 → 120秒 → (打ち止め) の指数バックオフ


def send_alert(msg: str) -> None:
    """screener 失敗時に Discord アラート送信.

    DISCORD_WEBHOOK_ALERT が未設定なら何もしない。
    送信自体の失敗はログに残すのみで screener の処理は継続。
    """
    webhook = settings.DISCORD_WEBHOOK_ALERT
    if not webhook:
        logger.warning("[Screener] DISCORD_WEBHOOK_ALERT 未設定、 通知スキップ")
        return
    payload = {
        "embeds": [{
            "title": "⚠️ Screener 失敗",
            "description": msg,
            "color": 0xE74C3C,  # 赤
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "footer": {"text": "moomoo-trader screener"},
        }],
    }
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        if r.status_code in (200, 204):
            logger.info("[Screener] Discord アラート送信成功")
        else:
            logger.error(
                "[Screener] Discord アラート送信失敗: status=%d body=%s",
                r.status_code, r.text[:200],
            )
    except Exception:
        logger.exception("[Screener] Discord アラート送信例外 (screener は継続)")


def fetch_with_retry(n: int) -> tuple[list[str], str | None]:
    """Finviz 取得をリトライ付きで実行.

    Returns:
        (候補リスト, 最終エラーメッセージ or None)
        成功時: (候補リスト, None)
        全失敗時: ([], エラーメッセージ)
    """
    last_error: str | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            candidates = fetch_finviz_candidates(n=n)
            if candidates:
                if attempt > 1:
                    logger.info("[Screener] リトライ %d/%d で成功", attempt, MAX_RETRIES)
                return candidates, None
            last_error = "Finviz が空リストを返した (dedup 後 0 銘柄)"
            logger.warning(
                "[Screener] 試行 %d/%d: 空リスト — リトライ",
                attempt, MAX_RETRIES,
            )
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(
                "[Screener] 試行 %d/%d 失敗: %s — リトライ",
                attempt, MAX_RETRIES, last_error,
            )

        if attempt < MAX_RETRIES:
            backoff = RETRY_BACKOFF_SEC * (2 ** (attempt - 1))
            logger.info("[Screener] %d 秒待って再試行 (attempt %d/%d)",
                        backoff, attempt + 1, MAX_RETRIES)
            time.sleep(backoff)

    return [], last_error


def main() -> None:
    """メイン処理."""
    logger.info("=" * 50)
    logger.info("[Screener] 動的スクリーニング開始")
    logger.info("=" * 50)

    max_symbols = settings.SCREENER_MAX_SYMBOLS

    # 1) Finviz で候補取得 (最大 MAX_RETRIES 回リトライ)
    candidates, err = fetch_with_retry(n=settings.SCREENER_CANDIDATES)
    if not candidates:
        # 全リトライ失敗: 既存 watchlist_dynamic.json を保持 (空で上書きしない)
        logger.error("[Screener] 全 %d 回リトライ失敗、 既存 watchlist_dynamic.json を保持", MAX_RETRIES)
        existing_info = "(既存 file なし)"
        if OUTPUT_PATH.exists():
            try:
                existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
                existing_n = len(existing.get("symbols", []))
                existing_gen = existing.get("generated_at", "?")
                existing_info = f"{existing_n} 銘柄 (generated_at={existing_gen})"
                logger.info("[Screener] 既存 watchlist_dynamic.json: %s", existing_info)
            except Exception:
                logger.exception("[Screener] 既存 file 読取エラー")
                existing_info = "(既存 file 読取エラー)"
        # Discord アラート送信
        alert_msg = (
            f"**Screener 全 {MAX_RETRIES} 回リトライ失敗**\n"
            f"最終エラー: `{err or 'unknown'}`\n"
            f"既存 watchlist_dynamic.json: {existing_info}\n"
            f"→ 次回 bot 起動時は上記の既存 (or 空) WATCHLIST で稼働"
        )
        send_alert(alert_msg)
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
    # 8/28 追加: 動的 sector map を保存 (未マップ問題の恒久対策)
    save_sector_map()

    # 候補銘柄リスト (絞り込み前の全件) をモメンタム検知用に保存
    candidates_output = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "symbols": candidates,
    }
    CANDIDATES_PATH.write_text(json.dumps(candidates_output, indent=2), encoding="utf-8")
    logger.info("[Screener] 候補保存: %s (%d銘柄)", CANDIDATES_PATH, len(candidates))

    logger.info("[Screener] 結果: %s", " ".join(top_symbols))
    logger.info("[Screener] 完了")


if __name__ == "__main__":
    main()
