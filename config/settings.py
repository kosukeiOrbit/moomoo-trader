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
SENTIMENT_THRESHOLD: float = 0.6      # LONGセンチメントスコアの最低閾値
SHORT_SENTIMENT_THRESHOLD: float = -0.3  # SHORTセンチメントスコアの閾値（これ以下で弱気）
CONFIDENCE_MIN: float = 0.7           # LLMの確信度最低値
FLOW_BUY_THRESHOLD: float = 0.65      # 大口買い/売り比率の最低閾値
ENABLE_SHORT: bool = os.getenv("ENABLE_SHORT", "false").lower() == "true"  # 空売り戦略（信用口座への振替が必要）
SHORT_DRY_RUN: bool = os.getenv("SHORT_DRY_RUN", "true").lower() == "true"  # SHORTドライラン（発注せず仮想PnLを記録）

# --- リスク管理 ---
MAX_DAILY_LOSS_PCT: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.03"))
MAX_DRAWDOWN_PCT: float = float(os.getenv("MAX_DRAWDOWN_PCT", "0.10"))
POSITION_MAX_PCT: float = float(os.getenv("POSITION_MAX_PCT", "0.02"))
KELLY_FRACTION: float = 0.5           # ハーフケリー
MIN_POSITION_SHARES: int = 1          # 最低保証株数（Kelly=0でもデータ蓄積用に発注）
MAX_POSITIONS: int = 10                # 同時保有ポジション上限
MIN_BUYING_POWER: float = float(os.getenv("MIN_BUYING_POWER", "150"))  # これ以下ならスキャンスキップ
CONSECUTIVE_LOSS_LIMIT: int = 3       # 連続敗北でサイズ縮小

# --- ストップロス ---
ATR_SL_MULTIPLIER: float = 1.0        # SL = ATR × 1.0
ATR_TP_MULTIPLIER: float = 1.5        # TP = ATR × 1.5（R:R = 1:1.5）
VWAP_DEVIATION_EXIT: float = 0.02     # VWAP乖離2%で撤退

# --- 動的スクリーニング ---
SCREENER_ENABLED: bool = os.getenv("SCREENER_ENABLED", "true").lower() == "true"
SCREENER_MAX_SYMBOLS: int = int(os.getenv("SCREENER_MAX_SYMBOLS", "10"))
SCREENER_CANDIDATES: int = int(os.getenv("SCREENER_CANDIDATES", "50"))
SCREENER_MAX_DROP_PCT: float = float(os.getenv("SCREENER_MAX_DROP_PCT", "-5.0"))  # これ以下の騰落率は除外

# --- 寄り付きスキップ ---
MARKET_OPEN_SKIP_MINUTES: int = int(os.getenv("MARKET_OPEN_SKIP_MINUTES", "30"))  # 寄り付き後この分数はエントリーをスキップ（0で無効）

# --- メインループ ---
LOOP_INTERVAL_SECONDS: int = 30

# --- Discord Webhook 通知 ---
DISCORD_WEBHOOK_SIGNAL: str = os.getenv("DISCORD_WEBHOOK_SIGNAL", "")   # mt-signal チャンネル
DISCORD_WEBHOOK_ALERT: str = os.getenv("DISCORD_WEBHOOK_ALERT", "")     # mt-alert チャンネル
DISCORD_WEBHOOK_SUMMARY: str = os.getenv("DISCORD_WEBHOOK_SUMMARY", "") # mt-summary チャンネル

# --- DB ---
DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost/daytrade")
