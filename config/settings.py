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

# --- Anthropic Claude API ---
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL: str = "claude-sonnet-4-20250514"
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
    # ハイテク・グロース（既存）
    "AAPL", "NVDA", "TSLA", "META", "MSFT",
    # 金融（既存）
    "JPM", "GS",
    # エネルギー（既存）
    "XOM",
    # 景気敏感（既存）
    "CAT",
    # ヘルスケア（既存）
    "UNH",
    # 追加
    "AVGO",  # AI半導体
    "PLTR",  # AI・防衛
    "CRWD",  # サイバーセキュリティ
    "TSM",   # 半導体製造
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

# Filter H: 過熱ガード (寄付き直後の gap/pre 過熱銘柄をブロック)
# n=72 分析: gap or pre >= +5% かつ ET 9:30-10:30 = n=13 / WR 46% / net -$199
# 同じ過熱でも ET 10:30 以降は n=6 / WR 83% / net +$57 → 午前序盤のみブロック
# 5/19 NOW(-$74), 5/22 WDAY(-$53), 5/26 APP(-$44), 5/29 DELL×2(-$57) を防ぐのが目的
# 0 で各値を無効化
TIGHT_GAP_MAX_PCT: float = float(os.getenv("TIGHT_GAP_MAX_PCT", "5.0"))
TIGHT_PRE_MAX_PCT: float = float(os.getenv("TIGHT_PRE_MAX_PCT", "5.0"))
TIGHT_OVERHEAT_GUARD_MINUTES: int = int(os.getenv("TIGHT_OVERHEAT_GUARD_MINUTES", "60"))
# 適用範囲: ET 9:30 から N 分間。 60=10:30まで、 90=11:00まで、 0で常時ON

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
LONG_MAX_POSITIONS: int = int(os.getenv("LONG_MAX_POSITIONS", "7"))
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

# --- 寄り付きスキップ ---
# 押し目待ちが VWAP 付近のみエントリーするため、寄り付き直後のノイズは自然弾き
# される。15分に短縮 (旧30分)。
MARKET_OPEN_SKIP_MINUTES: int = int(os.getenv("MARKET_OPEN_SKIP_MINUTES", "15"))
LONG_SKIP_DRY_RUN: bool = os.getenv("LONG_SKIP_DRY_RUN", "true").lower() == "true"  # スキップ期間中の LONG シグナルをJSONLに記録（IF分析用）
LONG_FULL_DRY_RUN: bool = os.getenv("LONG_FULL_DRY_RUN", "true").lower() == "true"  # 5枠フル時もスキャン継続して LONG シグナルをJSONLに記録（IF分析用）

# --- メインループ ---
LOOP_INTERVAL_SECONDS: int = 30

# --- Discord Webhook 通知 ---
DISCORD_WEBHOOK_SIGNAL: str = os.getenv("DISCORD_WEBHOOK_SIGNAL", "")   # mt-signal チャンネル
DISCORD_WEBHOOK_ALERT: str = os.getenv("DISCORD_WEBHOOK_ALERT", "")     # mt-alert チャンネル
DISCORD_WEBHOOK_SUMMARY: str = os.getenv("DISCORD_WEBHOOK_SUMMARY", "") # mt-summary チャンネル

# --- DB ---
DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost/daytrade")
