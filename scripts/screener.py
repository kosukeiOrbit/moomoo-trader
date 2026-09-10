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
# 9/10 追加: 選抜の判断材料を全件記録する分析用ログ (1 行 = 1 銘柄 x 1 日)。
# 従来 in_flow は logger.debug でしか出しておらず値が残らなかったため、
# 「in_flow の大小と実際のエントリー結果」を後から突き合わせられなかった。
# 採用/不採用に関わらず候補全件を残すので、足切り基準の妥当性を検証できる。
SCORE_LOG_PATH = DATA_DIR / "screener_scores.jsonl"

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


def _calc_atr_pct(kline, period: int) -> float | None:
    """日足 kline から ATR% (ATR / 直近終値) を計算する.

    True Range = max(high-low, |high-prev_close|, |low-prev_close|) の period 平均。
    データ不足・異常値の場合は None を返し、呼び出し側で倍率 1.0 にフォールバックする。
    """
    try:
        n = len(kline)
        if n < 2:
            return None
        highs = [float(x) for x in kline["high"]]
        lows = [float(x) for x in kline["low"]]
        closes = [float(x) for x in kline["close"]]
        trs = []
        for i in range(1, n):
            prev_close = closes[i - 1]
            trs.append(max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            ))
        if not trs:
            return None
        window = trs[-period:] if len(trs) > period else trs
        atr = sum(window) / len(window)
        last_close = closes[-1]
        if last_close <= 0:
            return None
        return atr / last_close
    except Exception:
        return None


def _daily_bar_metrics(kline) -> dict:
    """日足 kline から分析用の指標を抜き出す (記録専用、選抜には影響しない).

    直近バーの amplitude / 出来高比 / 終値位置などを残しておくと、
    「どんな値動きの銘柄が翌日勝ったか」を screener 側の記録だけで追える。
    """
    out: dict = {}
    try:
        n = len(kline)
        if n < 2:
            return out
        h = float(kline["high"].iloc[-1])
        low = float(kline["low"].iloc[-1])
        c = float(kline["close"].iloc[-1])
        o = float(kline["open"].iloc[-1])
        out["prev_close_price"] = round(c, 4)
        if low > 0:
            out["prev_amplitude"] = round((h - low) / low * 100, 3)
        if o > 0:
            out["prev_change_from_open_pct"] = round((c - o) / o * 100, 3)
        if h > low:
            out["prev_close_position"] = round((c - low) / (h - low), 3)
        if "volume" in kline.columns and n >= 6:
            vols = [float(x) for x in kline["volume"]]
            base = vols[-min(20, n - 1) - 1:-1]
            avg = sum(base) / len(base) if base else 0
            if avg > 0:
                out["prev_volume_ratio"] = round(vols[-1] / avg, 3)
    except Exception:
        pass
    return out


def save_score_log(records: list[dict], selected: list[str]) -> None:
    """選抜の判断材料を JSONL に追記する (分析専用、失敗しても screener は継続).

    採用/不採用に関わらず候補全件を残す。後から
    「in_flow がいくつの銘柄を採用し、その日どうだったか」を突き合わせられる。
    """
    if not records:
        return
    try:
        sel = set(selected)
        rank = {s: i + 1 for i, s in enumerate(selected)}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with SCORE_LOG_PATH.open("a", encoding="utf-8") as f:
            for r in records:
                r["selected"] = r["symbol"] in sel
                r["rank_final"] = rank.get(r["symbol"])
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        logger.info(
            "[Screener] スコアログ記録: %s (%d 件、うち採用 %d)",
            SCORE_LOG_PATH, len(records), sum(1 for r in records if r["selected"]),
        )
    except Exception:
        logger.exception("[Screener] スコアログ記録に失敗 (処理は継続)")


def score_by_moomoo_flow(candidates: list[str]) -> tuple[list[tuple[str, float]], list[dict]]:
    """moomoo の前日大口フロー × ATR% でスコアリングする.

    9/10 変更: 従来は in_flow 単独だったが、エントリー条件 (amp>=5% & atr>=3%) が
    高ボラ前提なのに選抜がボラを見ておらずミスマッチだった。
    score = in_flow * clamp(atr_pct / SCREENER_ATR_BASE, 上限 SCREENER_ATR_WEIGHT_CAP)。
    SCREENER_ATR_WEIGHT_ENABLED=false で従来の in_flow 単独に戻る。
    """
    import socket

    # OpenD 接続チェック
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        result = sock.connect_ex((settings.MOOMOO_HOST, settings.MOOMOO_PORT))
        if result != 0:
            logger.warning("[Screener] OpenD 未起動 — Finviz 出来高順で代替")
            return [], []
    finally:
        sock.close()

    subscribed_codes: list[str] = []
    ctx = None
    try:
        from futu import OpenQuoteContext, RET_OK, PeriodType, SubType

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

        # 9/10 修正: get_cur_kline は事前購読が必須 (未購読だと
        # "please subscribe to the KL_Day data first" で毎回失敗していた)。
        # これにより急落除外チェックが長期間 no-op になっていたため、日足を一括購読する。
        # 購読枠は 300 なので候補 100 銘柄なら収まる。失敗しても致命ではない
        # (kline が取れないだけで in_flow スコアリングは従来通り動く)。
        all_codes = [f"US.{sym}" for sym in candidates]
        try:
            ret_sub, sub_msg = ctx.subscribe(all_codes, [SubType.K_DAY])
            if ret_sub == RET_OK:
                subscribed_codes = all_codes
                time.sleep(1.0)  # 初回データ配信を待つ
                logger.info("[Screener] 日足 一括購読: %d銘柄", len(all_codes))
            else:
                logger.warning(
                    "[Screener] 日足購読失敗 (%s) — 急落除外と ATR 加重はスキップされます",
                    sub_msg,
                )
        except Exception:
            logger.exception("[Screener] 日足購読で例外 — kline 系はスキップして継続")

        scored: list[tuple[str, float]] = []
        kline_ok = 0
        score_records: list[dict] = []

        for i, symbol in enumerate(candidates):
            rec: dict = {
                "date": date.today().isoformat(),
                "flow_date": yesterday_str,
                "symbol": symbol,
                "rank_finviz": i + 1,   # Finviz 出来高順の元順位
            }
            try:
                code = f"US.{symbol}"

                # 前日騰落率チェック（急落銘柄を除外）+ ATR% 算出 (同じ kline を再利用)
                bars = max(settings.SCREENER_ATR_PERIOD + 1, 2)
                ret_kl, kline = ctx.get_cur_kline(code, bars, ktype="K_DAY")
                atr_pct: float | None = None
                if ret_kl == RET_OK and len(kline) >= 2:
                    kline_ok += 1
                    prev_close = float(kline["close"].iloc[-2])
                    last_close = float(kline["close"].iloc[-1])
                    rec.update(_daily_bar_metrics(kline))
                    if prev_close > 0:
                        change_pct = (last_close - prev_close) / prev_close * 100
                        rec["prev_day_change_pct"] = round(change_pct, 3)
                        if change_pct < settings.SCREENER_MAX_DROP_PCT:
                            logger.info(
                                "[Screener] %s 急落除外: %.1f%%", symbol, change_pct,
                            )
                            rec["excluded_reason"] = "drop"
                            rec["selected"] = False
                            score_records.append(rec)
                            time.sleep(1.0)
                            continue
                    atr_pct = _calc_atr_pct(kline, settings.SCREENER_ATR_PERIOD)
                    rec["atr_pct"] = round(atr_pct, 5) if atr_pct else None

                # 大口フロー取得
                ret, data = ctx.get_capital_flow(
                    code,
                    period_type=PeriodType.DAY,
                    start=yesterday_str,
                    end=yesterday_str,
                )
                if ret == RET_OK and not data.empty:
                    # in_flow は総額。super/big/mid/sml/main の内訳も残す
                    # (総額プラスでも超大口が流出しているケースがあり、
                    #  どの規模の資金が予測力を持つか後から検証するため)
                    for col in ("in_flow", "super_in_flow", "big_in_flow",
                                "mid_in_flow", "sml_in_flow", "main_in_flow"):
                        if col in data.columns:
                            try:
                                rec[col] = round(float(data[col].sum()), 1)
                            except Exception:
                                rec[col] = None
                    in_flow = float(data["in_flow"].sum()) if "in_flow" in data.columns else 0.0
                    if in_flow > 0:
                        weight = 1.0
                        if settings.SCREENER_ATR_WEIGHT_ENABLED and atr_pct:
                            weight = min(
                                atr_pct / settings.SCREENER_ATR_BASE,
                                settings.SCREENER_ATR_WEIGHT_CAP,
                            )
                        scored.append((symbol, in_flow * weight))
                        rec["atr_weight"] = round(weight, 3)
                        rec["score"] = round(in_flow * weight, 1)
                        logger.debug(
                            "[%s] in_flow=%.0f atr=%.2f%% weight=%.2f score=%.0f",
                            symbol, in_flow,
                            (atr_pct or 0) * 100, weight, in_flow * weight,
                        )
                    else:
                        rec["excluded_reason"] = "in_flow<=0"
                else:
                    rec["excluded_reason"] = "no_flow_data"
            except Exception:
                logger.debug("[Screener] フロー取得失敗: %s", symbol)
                rec["excluded_reason"] = "exception"
            score_records.append(rec)

            # レート制限対策: 1秒スリープ
            time.sleep(1.0)

            # 進捗ログ（10銘柄ごと）
            if (i + 1) % 10 == 0:
                logger.info("[Screener] 進捗: %d/%d", i + 1, len(candidates))

        logger.info(
            "[Screener] フロー確認完了: %d/%d銘柄がプラスフロー "
            "(kline取得 %d/%d、 ATR加重=%s base=%.1f%% cap=%.1f)",
            len(scored), len(candidates), kline_ok, len(candidates),
            "ON" if settings.SCREENER_ATR_WEIGHT_ENABLED else "OFF",
            settings.SCREENER_ATR_BASE * 100, settings.SCREENER_ATR_WEIGHT_CAP,
        )
        return scored, score_records

    except ImportError:
        logger.error("[Screener] futu パッケージ未インストール")
        return [], []
    except Exception:
        logger.exception("[Screener] moomoo フロー取得エラー")
        return [], []
    finally:
        if ctx is not None:
            if subscribed_codes:
                try:
                    from futu import SubType as _SubType
                    ctx.unsubscribe(subscribed_codes, [_SubType.K_DAY])
                    logger.info("[Screener] 日足購読解除: %d銘柄", len(subscribed_codes))
                except Exception:
                    logger.warning("[Screener] 購読解除に失敗 (OpenD 側で自然解放されます)")
            ctx.close()


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
    scored, score_records = score_by_moomoo_flow(candidates)

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
    # 9/10 追加: 選抜の判断材料を全件記録 (足切り基準の事後検証用)
    save_score_log(score_records, top_symbols)

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
