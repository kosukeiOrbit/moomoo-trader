"""全設定値の一元管理モジュール."""

import os
from pathlib import Path
from dotenv import load_dotenv

# プロジェクトルートの .env を絶対パスで読み込む（S4U スケジューラ対応）
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")

# --- moomoo OpenAPI ---
MOOMOO_HOST: str = os.getenv("MOOMOO_HOST", "127.0.0.1")
MOOMOO_PORT: int = int(os.getenv("MOOMOO_PORT", "11111"))
MOOMOO_TRADE_PWD: str = os.getenv("MOOMOO_TRADE_PWD", "")
TRADE_ENV: str = os.getenv("TRADE_ENV", "SIMULATE")  # "SIMULATE" or "REAL"
SECURITY_FIRM: str = os.getenv("SECURITY_FIRM", "FUTUJP")  # moomoo証券（日本）
JP_ACC_TYPE: str = os.getenv("JP_ACC_TYPE", "SPECIFIC")  # "GENERAL" or "SPECIFIC"

# --- 手数料プラン (moomoo Japan、 1 日 1 回変更可能) ---
# "basic":   約定代金 × 0.132% / 最低 $0.01 / 上限 $22 (small size 向き)
# "advance": 株数ベース、 取引 max($0.00539/株, $1.08) + システム max($0.0055/株, $1.10)
#            + 清算 $0.006/株、 上限は取引/システム各 約定代金×0.55% (large size 向き)
# size $2500+ ではほぼ全銘柄で advance 有利。 size $1500 以下は basic 有利
FEE_PLAN: str = os.getenv("FEE_PLAN", "basic").lower()

# --- 信用取引 (margin) ---
# True: LONG エントリーを信用買いで発注 (取引額の最大 ~2 倍まで利用可)
# False: 現物買いで発注 (デフォルト)
# 切替時は bot 再起動が必要。 同一セッション中に切り替えると既存ポジションの決済で口座不整合が起きる
USE_MARGIN_LONG: bool = os.getenv("USE_MARGIN_LONG", "false").lower() == "true"

# --- Anthropic Claude API ---
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
# 6/16 修正: claude-sonnet-4-20250514 が 404 (Anthropic 側で廃止) → claude-sonnet-4-6 に更新
# env で上書き可能 (将来 Opus 等への切替や、 新モデルリリース時の柔軟性のため)
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MIN_TEXTS_FOR_ANALYSIS: int = 1  # これ未満のテキスト数ではAPI呼び出しをスキップ

# --- 監視銘柄 ---
# WATCHLIST: list[str] = [
#     # ハイテク・グロース
#     "AAPL", "NVDA", "TSLA", "META", "MSFT",
#     # 金融（セクターローテーション対応）
#     "JPM", "GS",
#     # エネルギー（原油・地政学リスク対応）
#     "XOM",
#     # 景気敏感（ダウ牽引役）
#     "CAT",
#     # ヘルスケア（ディフェンシブ）
#     "UNH",
# ]
WATCHLIST = [
    # 8/11 大幅縮小: 12 → 5 銘柄。
    # 根拠: 実売 n=49 (6/11-8/10) の分析で
    #   固定 WL: n=17 / net -$53 (実質ゼロ)
    #   動的 WL: n=32 / net +$558 (圧勝)
    #   8月除外でも 固定 -$1 vs 動的 +$495 で構造的差
    # 削除銘柄の理由:
    #   PLTR (n=3 -$69), TSM (n=3 -$36), AVGO (n=1 -$31), INTC 固定経由 (n=4 -$57) = 実売負け組
    #   JPM/XOM/UNH = 実売 0 件 (低 amp セクターで対象外)
    # 残す 5 銘柄の役割:
    "TSLA",  # 実売 n=3 net +$130 WR 100% = 固定 WL 唯一の稼ぎ頭
    "NVDA",  # 市場代表、 高流動性、 動く日は必ず動く
    "META",  # メガテック代表、 ノイズ源にならず
    "MSFT",  # メガテック代表、 保険的枠 (Finviz で選ばれない日のバックアップ)
    "PANW",  # 実売 n=2 net+ (+$8)、 サイバーセキュリティで動的 WL に入りにくい
    # 削除銘柄は動的 WL (Finviz) で拾われれば取引される (INTC は動的経由 n=4 +$115 の実績あり)
]

# --- シグナル閾値 ---
# --- Tight Filter (高値掴み・動的小型中ボラ・低値幅除外) ---
TIGHT_FILTER_ENABLED: bool = os.getenv("TIGHT_FILTER_ENABLED", "true").lower() == "true"
TIGHT_VWAP_DEV_PCT: float = float(os.getenv("TIGHT_VWAP_DEV_PCT", "1.0"))    # VWAPからの乖離率 (%) これ超えで除外 (R2: 強トレンド例外なし)
# Filter D (R1): dynamic 銘柄かつ atr_pct がこの範囲内なら除外（中ボラ罠）
# n=10 で統計不十分のためログのみ・通過に格下げ (データ蓄積中)。設定は維持。
TIGHT_DYN_MID_ATR_LOW: float = float(os.getenv("TIGHT_DYN_MID_ATR_LOW", "0.04"))
TIGHT_DYN_MID_ATR_HIGH: float = float(os.getenv("TIGHT_DYN_MID_ATR_HIGH", "0.05"))
# Filter E: 当日値幅率 (amplitude%) これ未満ならスキップ (低値幅日 SL whipsaw 防止)
# 0 で実質無効 (推奨: .env 経由で 0 に設定)
# 理由: 寄付き直後 (ET 10:00) は全銘柄が低 amplitude のため誤射する。
# 「動かない日」リスクは押し目待ち + Filter A2 (vwap_dev>1%) で代替できているため
# 現在は無効化中。データ蓄積後に再評価。
TIGHT_AMPLITUDE_MIN: float = float(os.getenv("TIGHT_AMPLITUDE_MIN", "3.0"))
# Filter G: ATR% (推定ボラ) これ未満ならスキップ
# 5/12-5/18 ドライラン IF 分析: amp>=3% AND atr>=2.5% で n=20 / WR=90% / avg=+$10.19
# 0 で実質無効
TIGHT_ATR_PCT_MIN: float = float(os.getenv("TIGHT_ATR_PCT_MIN", "0.025"))

# Filter I: volume_ratio (普段との出来高比) これ未満ならスキップ
# 全期間 LONG 累計 n=296 分析 (6/26 確定): vol<1.0 cohort n=19 / net -$191 / WR 21%
# 内訳: vol 0.85-1.0 が n=9 / net -$168 / WR 11% で異常な損失帯
# 一方 vol 1.0-1.2 は n=15 / net +$158 / WR 87% で最強帯 → 閾値 1.0 で明確に分岐
# 0 で実質無効
TIGHT_VOL_RATIO_MIN: float = float(os.getenv("TIGHT_VOL_RATIO_MIN", "0.0"))

# Filter H: 過熱ガード (寄付き直後の gap/pre 過熱銘柄をブロック)
# n=72 分析: gap or pre >= +5% かつ ET 9:30-10:30 = n=13 / WR 46% / net -$199
# 同じ過熱でも ET 10:30 以降は n=6 / WR 83% / net +$57 → 午前序盤のみブロック
# 5/19 NOW(-$74), 5/22 WDAY(-$53), 5/26 APP(-$44), 5/29 DELL×2(-$57) を防ぐのが目的
# 0 で各値を無効化
TIGHT_GAP_MAX_PCT: float = float(os.getenv("TIGHT_GAP_MAX_PCT", "5.0"))
TIGHT_PRE_MAX_PCT: float = float(os.getenv("TIGHT_PRE_MAX_PCT", "5.0"))
TIGHT_OVERHEAT_GUARD_MINUTES: int = int(os.getenv("TIGHT_OVERHEAT_GUARD_MINUTES", "60"))
# 適用範囲: ET 9:30 から N 分間。 60=10:30まで、 90=11:00まで、 120=11:30まで、 999=常時ON
# 0 は Filter H を **完全無効化** (過熱ガードなし) 。 常時ON にしたい場合は 999 (or 400 以上) を指定
# 7/10 修正: 旧コメントで「0で常時ON」 と誤記載していた (実装は `if > 0`)

# --- 暴落日 LONG エントリーブロック (SPY 地合いフィルタ) ---
# SPY のリアルタイム変動率がこの閾値を下回ったら LONG エントリーをブロック。
# n=62 (5/19〜6/8) 分析: SPY<-0.5% cohort は WR 17% / avg -$23 と圧倒的に負け、
# SPY 0〜+0.2% は WR 68% / avg +$9.44。 大暴落日の LRCX (SPY=-0.85% 入り) -$48
# のような典型損失を防ぐ目的。 blocked された LONG シグナルは
# long_rejected_dryrun.jsonl に SPY_BLOCK 理由で記録され、 「取りこぼしリバウンド」 を
# IF 分析で後から測定可能 (tight_reason="SPY_BLOCK:...")。
# 0 (or 未設定) で無効化。
SPY_LONG_BLOCK_THRESHOLD: float = float(os.getenv("SPY_LONG_BLOCK_THRESHOLD", "0"))

# --- 暴落日 LONG エントリーブロック (QQQ 地合いフィルタ、 7/8 追加) ---
# QQQ (テック指数) のリアルタイム変動率がこの閾値を下回ったら LONG エントリーをブロック。
# n=30 分析 (6/12-7/6 実売): QQQ_rt<-0.5% cohort は WR 0%、 3/3 全敗、 net -$94。
# うち 1 件 (TSM 7/2) は SL タッチで -$66.55、 この 3 週間の単発最大負け。
# SPY_BLOCK と AND ではなく OR で判定 (SPY か QQQ どちらか閾値割れば block)。
# blocked シグナルは long_rejected_dryrun.jsonl に QQQ_BLOCK 理由で記録し、
# IF 分析で「もし取っていたら」の仮想 pnl を後から評価可能。
# 0 (or 未設定) で無効化。 -0.005 = -0.5%
QQQ_LONG_BLOCK_THRESHOLD: float = float(os.getenv("QQQ_LONG_BLOCK_THRESHOLD", "0"))

# --- Shadow: sentiment 撤廃案 の検証用 (8/4 追加) ---
# true にすると、 AND filter で reject された signal のうち
# 「flow=BUY & strength>0.65 & tight_filter 通過」 する signal を
# data/long_sentiment_shadow_dryrun.jsonl に shadow 記録する。
# 実発注 (実運用) には**一切影響しない** (read-only 追加)。
# 蓄積後、 「sentiment 撤廃した場合の想定 net」 を Phase 2 で検証する。
# false で機能無効化 (デフォルト false = 安全側)。
SHADOW_SENTIMENT_ENABLED: bool = os.getenv("SHADOW_SENTIMENT_ENABLED", "false").lower() == "true"

# --- Phase 2: Sentiment Bypass Final 案 (8/28 実装) ---
# shadow n=80 分析で発見した勝ちパターンを実売化する追加 layer。
# 現行 AND filter (sentiment>0.6 & confidence>0.7) で reject された signal のうち、
# 以下の 3 条件をすべて満たす場合は sentiment 判定を bypass して実売する:
#   1) amp >= AMP_MIN (default 4.0%) — 十分な値動き
#   2) gap < GAP_MAX (default 3.0%) — 過熱 gap up 除外
#   3) cfo < CFO_MAX (default 3.0%) — 寄からの過熱除外
# 現行実売 (sentiment 通過) はそのまま並列動作、 shadow 記録も継続。
# revert: `SENTIMENT_BYPASS_FINAL_ENABLED=false` で即座に無効化 (default false = 安全)。
# 実測データ (shadow n=18 で WR 72% avg +$51、 現行実売 avg +$8.75 の 6 倍)。
SENTIMENT_BYPASS_FINAL_ENABLED: bool = os.getenv("SENTIMENT_BYPASS_FINAL_ENABLED", "false").lower() == "true"
SENTIMENT_BYPASS_AMP_MIN: float = float(os.getenv("SENTIMENT_BYPASS_AMP_MIN", "4.0"))
SENTIMENT_BYPASS_GAP_MAX: float = float(os.getenv("SENTIMENT_BYPASS_GAP_MAX", "3.0"))
SENTIMENT_BYPASS_CFO_MAX: float = float(os.getenv("SENTIMENT_BYPASS_CFO_MAX", "3.0"))

# --- auto-daytrade 方針 shadow (8/13 追加、 別プロジェクト検証案) ---
# 別プロジェクト (auto-daytrade、 日本株 kabu 経由) の Pattern B ロング L1 が
# WR 79% で成功しているため、 その判定条件を米国株に移植して shadow 記録する。
# 判定条件 (auto-daytrade §5.1 + §5.2):
#   1) 現在値 > VWAP × 1.002 (VWAP 直上・押し目後の反発)
#   2) 直近5サンプル (~2.5分) で +0.3% 上昇 (モメンタム)
#   3) volume_ratio >= 2.0 (出来高急増)
#   4) VWAP 乖離 < +1.0% (押し目〜ちょい高)
#   5) セクター ETF (XLK/XLC 等) 当日変動 >= +1.0% (業種上昇日のみ)
# 現行の flow / sentiment / SPY_BLOCK / QQQ_BLOCK / AND filter / tight_filter は
# **すべて無視** (auto-daytrade 準拠)、 全銘柄で毎スキャン判定。
# 実発注ロジックには**一切影響しない** (read-only 追加、 完全独立 shadow)。
# 蓄積目標: n=30-50 (1-2 週間)、 avg net が現行実売 (avg +$10) を超えるか検証。
# false で機能無効化 (デフォルト false = 安全側)。
AUTODAY_SHADOW_ENABLED: bool = os.getenv("AUTODAY_SHADOW_ENABLED", "false").lower() == "true"
AUTODAY_SECTOR_MIN_CHG_PCT: float = float(os.getenv("AUTODAY_SECTOR_MIN_CHG_PCT", "1.0"))    # セクター ETF 上昇率 >= この値
AUTODAY_VWAP_MAX_DEV_PCT: float = float(os.getenv("AUTODAY_VWAP_MAX_DEV_PCT", "1.0"))         # VWAP 乖離 < この値
AUTODAY_VWAP_MIN_MULT: float = float(os.getenv("AUTODAY_VWAP_MIN_MULT", "1.002"))             # 現在値 > VWAP × この倍率
AUTODAY_MOMENTUM_MIN_PCT: float = float(os.getenv("AUTODAY_MOMENTUM_MIN_PCT", "0.3"))         # 直近5サンプル差 >= この値
AUTODAY_VOL_RATIO_MIN: float = float(os.getenv("AUTODAY_VOL_RATIO_MIN", "2.0"))               # volume_ratio >= この値
AUTODAY_SECTOR_ETF_CACHE_SEC: int = int(os.getenv("AUTODAY_SECTOR_ETF_CACHE_SEC", "600"))     # セクター ETF キャッシュ秒数 (default 10 分)

# 銘柄 → GICS セクター ETF マッピング
# 主要な現行 WL 銘柄 + 過去 dryrun 出現銘柄をカバー
# 未マッピング銘柄は shadow 判定不能扱いでスキップされる
GICS_SECTOR_ETF: dict[str, str] = {
    # Technology (XLK)
    "NVDA": "XLK", "MSFT": "XLK", "AVGO": "XLK", "PANW": "XLK", "CRWD": "XLK",
    "DELL": "XLK", "AMD": "XLK", "MU": "XLK", "INTC": "XLK", "ADBE": "XLK",
    "SMCI": "XLK", "IBM": "XLK", "ORCL": "XLK", "SNPS": "XLK", "CDNS": "XLK",
    "PTC": "XLK", "AMAT": "XLK", "KLAC": "XLK", "LRCX": "XLK", "TSM": "XLK",
    "QCOM": "XLK", "DDOG": "XLK", "FTNT": "XLK", "INTU": "XLK", "WDAY": "XLK",
    "NOW": "XLK", "ADSK": "XLK", "GLW": "XLK", "WDC": "XLK", "STX": "XLK",
    "SNDK": "XLK", "LITE": "XLK", "APH": "XLK", "TXN": "XLK", "CSCO": "XLK",
    "COHR": "XLK", "NTAP": "XLK", "FSLR": "XLK", "MRVL": "XLK", "APP": "XLK",
    "TER": "XLK", "KEYS": "XLK", "JBL": "XLK", "FLEX": "XLK", "ANET": "XLK",
    "ZBRA": "XLK", "MPWR": "XLK", "MSI": "XLK", "MCHP": "XLK", "HPE": "XLK",
    "TYL": "XLK",
    # 8/19 追加 (今日の動的 WL の未マップ分)
    "AAPL": "XLK",  # Apple, Info Tech
    "PLTR": "XLK",  # Palantir, IT/Software
    "CIEN": "XLK",  # Ciena, Communications Equipment (GICS IT)
    # 8/21 追加 (5 セクター拡張後の動的 WL の未マップ分)
    "CRM": "XLK",   # Salesforce, Software
    "ADI": "XLK",   # Analog Devices, 半導体
    "CDW": "XLK",   # CDW Corp, Tech distribution
    "AKAM": "XLK",  # Akamai, CDN/Security
    # 8/28 追加 (新スクリーナー動的 WL の未マップ分)
    "HPQ": "XLK",   # HP Inc, Tech Hardware
    "CTSH": "XLK",  # Cognizant Tech Solutions, IT Services
    # Communication Services (XLC)
    "META": "XLC", "GOOG": "XLC", "GOOGL": "XLC", "NFLX": "XLC", "DIS": "XLC",
    "CMCSA": "XLC", "TMUS": "XLC", "WBD": "XLC", "CHTR": "XLC", "TTD": "XLC",
    "TTWO": "XLC", "EA": "XLC", "LYV": "XLC", "OMC": "XLC", "MELI": "XLC",
    "SATS": "XLC",
    "FOX": "XLC",  # 8/19 追加: Fox Corp, Communication Services
    # Consumer Discretionary (XLY)
    "TSLA": "XLY", "UBER": "XLY", "MRNA": "XLY",
    # Financials (XLF)
    "JPM": "XLF", "PAYX": "XLF", "ADP": "XLF", "JKHY": "XLF", "GPN": "XLF",
    "CPAY": "XLF", "FIS": "XLF",
    "XYZ": "XLF",  # 8/19 追加: Block Inc (旧 Square), Financials/Fintech
    "BR": "XLF",   # 8/21 追加: Broadridge, Financial data services
    # Health Care (XLV)
    "UNH": "XLV", "BMY": "XLV", "BAX": "XLV", "MRK": "XLV", "GILD": "XLV",
    "CVS": "XLV", "ABT": "XLV", "BSX": "XLV",
    "PFE": "XLV",  # 8/28 追加: Pfizer, 医薬品
    # Energy (XLE)
    "XOM": "XLE",
    # Industrials (XLI)
    "ROP": "XLI", "ECHO": "XLI",
}

# --- 押し目待ち (Pullback Wait) ---
PULLBACK_ENABLED: bool = os.getenv("PULLBACK_ENABLED", "true").lower() == "true"
PULLBACK_VWAP_ENTRY_PCT: float = float(os.getenv("PULLBACK_VWAP_ENTRY_PCT", "0.5"))  # vwap_dev <= この値で即エントリー / 超えたら待機キュー
PULLBACK_TIMEOUT_MINUTES: int = int(os.getenv("PULLBACK_TIMEOUT_MINUTES", "30"))    # 待機タイムアウト

# --- エントリー直前の価格下落チェック ---
# 直前スキャン (約30秒前) からの下落率がこれ以上ならエントリーをスキップ
# 0.15% = $200の銘柄なら 30¢ 下落でスキップ
ENTRY_PRICE_DROP_THRESHOLD: float = float(os.getenv("ENTRY_PRICE_DROP_THRESHOLD", "0.15"))

# --- エントリー直前の価格トレンドフィルタ (A 案: 5/22 WDAY 失敗対策) ---
# A1: 直近スキャン (30秒前) より current が下げていたらブロック
ENTRY_BLOCK_ON_DECLINE: bool = os.getenv("ENTRY_BLOCK_ON_DECLINE", "false").lower() == "true"
# A3: 直近 3 観測の最安値 +buffer% を上回らないとブロック (Higher Low 形成待ち)
ENTRY_BLOCK_BELOW_LOCAL_LOW: bool = os.getenv("ENTRY_BLOCK_BELOW_LOCAL_LOW", "false").lower() == "true"
ENTRY_PRICE_LOCAL_LOW_BUFFER: float = float(os.getenv("ENTRY_PRICE_LOCAL_LOW_BUFFER", "0.001"))  # 0.1%
# 価格履歴の保持長 (30秒スキャンで 6 = 3 分分)
ENTRY_PRICE_HISTORY_DEPTH: int = int(os.getenv("ENTRY_PRICE_HISTORY_DEPTH", "6"))
# スキャン価格を JSONL に記録 (検証用、 1日数MB)
SCAN_PRICE_LOG_ENABLED: bool = os.getenv("SCAN_PRICE_LOG_ENABLED", "true").lower() == "true"
# A1/A3 ブロックイベントを JSONL に記録 (クールダウン案の事後検証用、 1日数KB)
A1A3_BLOCK_LOG_ENABLED: bool = os.getenv("A1A3_BLOCK_LOG_ENABLED", "true").lower() == "true"

# --- モメンタム検知 (寄付き直前の急騰銘柄を当日WATCHLISTに追加) ---
# 6/10 分析 (n=153): is_momentum=True cohort は n=25 / WR 60% / sum -$103 で損失源。
# is_momentum=False (n=74) は sum +$410 / WR 69%。 当日 WL 追加経由の過熱銘柄が主犯。
# MOMENTUM_DETECTION_ENABLED=false で検知ロジック自体を無効化:
#   - _momentum_added_symbols が常に空 → is_momentum=False で全銘柄評価
#   - Filter A2 (vwap_dev) の momentum 緩和 (1%→2%) は適用されない → 通常閾値で厳格判定
#   - 押し目待ち閾値 (MOMENTUM_VWAP_ENTRY_PCT=1.0%) も適用されず標準 0.5% に
#   - 当日 WL 追加なし
# すべて分析の方向性 (厳しめ) と整合。
MOMENTUM_DETECTION_ENABLED: bool = os.getenv("MOMENTUM_DETECTION_ENABLED", "true").lower() == "true"
MOMENTUM_THRESHOLD_PCT: float = float(os.getenv("MOMENTUM_THRESHOLD_PCT", "5.0"))  # pre/after変化率がこれ以上の銘柄を検知
MOMENTUM_MAX_SYMBOLS: int = int(os.getenv("MOMENTUM_MAX_SYMBOLS", "5"))            # 最大追加銘柄数
MOMENTUM_VWAP_ENTRY_PCT: float = float(os.getenv("MOMENTUM_VWAP_ENTRY_PCT", "1.0"))  # モメンタム銘柄の即エントリー閾値 (通常 PULLBACK_VWAP_ENTRY_PCT=0.5%)
MOMENTUM_ONLY_MODE: bool = os.getenv("MOMENTUM_ONLY_MODE", "false").lower() == "true"   # True=モメンタム検知銘柄のみエントリー (False=通常モード)

SENTIMENT_THRESHOLD: float = 0.6      # LONGセンチメントスコアの最低閾値
SHORT_SENTIMENT_THRESHOLD: float = -0.3  # SHORTセンチメントスコアの閾値（これ以下で弱気）
CONFIDENCE_MIN: float = 0.7           # LLMの確信度最低値
FLOW_BUY_THRESHOLD: float = float(os.getenv("FLOW_BUY_THRESHOLD", "0.65"))  # 大口買い/売り比率の最低閾値 (n=72分析で flow<0.8 が損失源 → .env で 0.8 に)
ENABLE_SHORT: bool = os.getenv("ENABLE_SHORT", "false").lower() == "true"  # 空売り戦略（信用口座への振替が必要）
SHORT_DRY_RUN: bool = os.getenv("SHORT_DRY_RUN", "true").lower() == "true"  # SHORTドライラン（発注せず仮想PnLを記録）

# --- SHORT Phase 1: FINAL-7 フィルタ + 専用設定 (6/19 backlog) ---
# SHORT 専用サイズ。 未設定なら LONG (POSITION_SIZE_USD) と同じになる
# 解禁初期は LONG より小額で試運転 (例: 750)
def _get_short_position_size() -> float:
    val = os.getenv("SHORT_POSITION_SIZE_USD")
    if val:
        return float(val)
    return float(os.getenv("POSITION_SIZE_USD", "1500"))
SHORT_POSITION_SIZE_USD: float = _get_short_position_size()
# 構造的負け銘柄をカンマ区切りで指定 (例: "KLAC,SNDK")
SHORT_BLOCK_SYMBOLS: set[str] = {
    s.strip().upper() for s in os.getenv("SHORT_BLOCK_SYMBOLS", "").split(",") if s.strip()
}
# gap_pct がこの値以下なら SHORT 禁止 (dead cat bounce 罠回避)
SHORT_GAP_MIN_PCT: float = float(os.getenv("SHORT_GAP_MIN_PCT", "-100.0"))
# amplitude がこの値以上なら SHORT 禁止 (過熱反転リスク)
SHORT_AMP_MAX_PCT: float = float(os.getenv("SHORT_AMP_MAX_PCT", "100.0"))
# amplitude がこの値未満なら SHORT 禁止 (値動き不足、 案C 6/23 追加)
SHORT_AMP_MIN_PCT: float = float(os.getenv("SHORT_AMP_MIN_PCT", "0.0"))
# volume_ratio がこの範囲外なら SHORT 禁止 ([MIN, MAX) で判定、 案C 6/23 追加)
SHORT_VOL_RATIO_MIN: float = float(os.getenv("SHORT_VOL_RATIO_MIN", "0.0"))
SHORT_VOL_RATIO_MAX: float = float(os.getenv("SHORT_VOL_RATIO_MAX", "100.0"))
# SPY (prev_close 比) がこの値以上なら SHORT 禁止 (弱気相場のみ解禁)
SHORT_SPY_MAX_PC: float = float(os.getenv("SHORT_SPY_MAX_PC", "100.0"))
# SHORT 専用 SL/TP 乗数 (LONG とは非対称)
ATR_SL_MULTIPLIER_SHORT: float = float(os.getenv("ATR_SL_MULTIPLIER_SHORT", "0.7"))
ATR_TP_MULTIPLIER_SHORT: float = float(os.getenv("ATR_TP_MULTIPLIER_SHORT", "0.7"))

# --- 全LONG実取引の有効/無効 ---
# False: moomoo API を叩かず仮想ポジションで完全シミュレーション
#        (押し目待ち、 SL/TP 監視、 MFE/MAE、 CSV出力、 commission計算すべて動作)
ENABLE_REAL_TRADING: bool = os.getenv("ENABLE_REAL_TRADING", "true").lower() == "true"

# --- リスク管理 ---
MAX_DAILY_LOSS_PCT: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.03"))
MAX_DRAWDOWN_PCT: float = float(os.getenv("MAX_DRAWDOWN_PCT", "0.10"))
POSITION_SIZE_USD: float = float(os.getenv("POSITION_SIZE_USD", "1500"))  # 1ポジションあたりの固定額（ドル）
MIN_POSITION_SHARES: int = 1          # 最低保証株数（Kelly=0でもデータ蓄積用に発注）
# MAX_POSITIONS: int = 10             # (旧) 合計上限 → LONG/SHORT独立管理に変更
LONG_MAX_POSITIONS: int = int(os.getenv("LONG_MAX_POSITIONS", "5"))
SHORT_MAX_POSITIONS: int = int(os.getenv("SHORT_MAX_POSITIONS", "3"))
MIN_BUYING_POWER: float = float(os.getenv("MIN_BUYING_POWER", "150"))  # これ以下ならスキャンスキップ

# --- エントリー注文の保護指値 (成行 → 保護指値への移行) ---
# 0 で従来の成行、 0.02 で last_price × 1.02 の指値 (BUY) / × 0.98 (SELL/SHORT)
# 利点: 通常時は ask で即約定、 急騰時には指値で打ち切り、 moomoo 余力消費も激減
# 5/28 KEYS の "価格乖離保護" 拒否を受けて 0.05 → 0.02 に縮小
# (moomoo の許容範囲は +2-3% 以内、 +5% は中型株で拒否される)
ORDER_PROTECTIVE_LIMIT_PCT: float = float(os.getenv("ORDER_PROTECTIVE_LIMIT_PCT", "0.02"))
CONSECUTIVE_LOSS_LIMIT: int = 3       # 連続敗北でサイズ縮小

# --- ストップロス ---
ATR_SL_MULTIPLIER: float = float(os.getenv("ATR_SL_MULTIPLIER", "0.7"))  # SL = ATR × 0.7 (旧0.5、5月損失分析で緩和)
ATR_TP_MULTIPLIER: float = float(os.getenv("ATR_TP_MULTIPLIER", "1.0"))  # TP = ATR × 1.0（R:R ≒ 1:1.4）
VWAP_DEVIATION_EXIT: float = 0.02     # VWAP乖離2%で撤退

# --- 動的スクリーニング ---
SCREENER_ENABLED: bool = os.getenv("SCREENER_ENABLED", "true").lower() == "true"
SCREENER_MAX_SYMBOLS: int = int(os.getenv("SCREENER_MAX_SYMBOLS", "10"))
SCREENER_CANDIDATES: int = int(os.getenv("SCREENER_CANDIDATES", "100"))  # モメンタム検知用候補プールも兼ねるので拡大
SCREENER_MAX_DROP_PCT: float = float(os.getenv("SCREENER_MAX_DROP_PCT", "-5.0"))  # これ以下の騰落率は除外

# 9/10 追加: 選抜スコアに ATR% を加味する (in_flow 単独ではボラティリティを見ていなかった)
# 従来は in_flow (前日大口資金流入) 順のみで上位 N を選抜していたが、エントリー条件は
# amp>=5% & atr>=3% の高ボラ前提なので基準がミスマッチだった。実測で NOW(条件B 7回) /
# SMCI(5回) / CRWD(4回) が候補プールにいながら in_flow 順で漏れていた。
# score = in_flow * (atr_pct / SCREENER_ATR_BASE) にすることで、資金が入っていて
# かつボラのある銘柄を優先する。SCREENER_ATR_WEIGHT_ENABLED=false で従来動作に戻る。
SCREENER_ATR_WEIGHT_ENABLED: bool = os.getenv("SCREENER_ATR_WEIGHT_ENABLED", "true").lower() == "true"
SCREENER_ATR_BASE: float = float(os.getenv("SCREENER_ATR_BASE", "0.03"))  # 倍率1.0となる基準 ATR% (Filter G の閾値と揃える)
SCREENER_ATR_WEIGHT_CAP: float = float(os.getenv("SCREENER_ATR_WEIGHT_CAP", "3.0"))  # 倍率上限 (極端なボラ銘柄が独占するのを防ぐ)
SCREENER_ATR_PERIOD: int = int(os.getenv("SCREENER_ATR_PERIOD", "14"))  # ATR 計算期間 (日足)

# --- 寄り付きスキップ ---
# 押し目待ちが VWAP 付近のみエントリーするため、寄り付き直後のノイズは自然弾き
# される。15分に短縮 (旧30分)。
MARKET_OPEN_SKIP_MINUTES: int = int(os.getenv("MARKET_OPEN_SKIP_MINUTES", "15"))
LONG_SKIP_DRY_RUN: bool = os.getenv("LONG_SKIP_DRY_RUN", "true").lower() == "true"  # スキップ期間中の LONG シグナルをJSONLに記録（IF分析用）
LONG_FULL_DRY_RUN: bool = os.getenv("LONG_FULL_DRY_RUN", "true").lower() == "true"  # 5枠フル時もスキャン継続して LONG シグナルをJSONLに記録（IF分析用）

# --- メインループ ---
LOOP_INTERVAL_SECONDS: int = 30

# 9/10 追加: scan ループでのフロー取得ペーシング。
# moomoo の get_capital_flow はリクエスト頻度に上限があり、超えると ret=-1 を返す。
# 実測 (2026-09-10, 監視47銘柄): 1秒あたり 1-4 件なら失敗率 0-3% だが、
# 6-7 件で 22-26%、8 件以上で 50-100% に跳ね上がる。
# 監視 35→47 銘柄への拡大でフロー失敗率が 14% → 34% に悪化し、
# watchlist 後半の 14 銘柄が毎回 SKIP(API skipped) されてエントリー対象外になっていた。
# 銘柄ごとに待ってから取得することで 3 件/秒前後に抑える。0 で無効化 (従来動作)。
FLOW_FETCH_INTERVAL_SEC: float = float(os.getenv("FLOW_FETCH_INTERVAL_SEC", "0.3"))

# --- Discord Webhook 通知 ---
DISCORD_WEBHOOK_SIGNAL: str = os.getenv("DISCORD_WEBHOOK_SIGNAL", "")   # mt-signal チャンネル
DISCORD_WEBHOOK_ALERT: str = os.getenv("DISCORD_WEBHOOK_ALERT", "")     # mt-alert チャンネル
DISCORD_WEBHOOK_SUMMARY: str = os.getenv("DISCORD_WEBHOOK_SUMMARY", "") # mt-summary チャンネル

# --- DB ---
DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost/daytrade")
