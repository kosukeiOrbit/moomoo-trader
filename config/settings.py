"""全設定値の一元管理モジュール."""

import os
from dotenv import load_dotenv

load_dotenv()

# --- moomoo OpenAPI ---
MOOMOO_HOST: str = os.getenv("MOOMOO_HOST", "127.0.0.1")
MOOMOO_PORT: int = int(os.getenv("MOOMOO_PORT", "11111"))
MOOMOO_TRADE_PWD: str = os.getenv("MOOMOO_TRADE_PWD", "")
TRADE_ENV: str = os.getenv("TRADE_ENV", "SIMULATE")  # "SIMULATE" or "REAL"
SECURITY_FIRM: str = os.getenv("SECURITY_FIRM", "FUTUJP")  # moomoo証券（日本）

# --- Anthropic Claude API ---
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL: str = "claude-sonnet-4-20250514"
MIN_TEXTS_FOR_ANALYSIS: int = 1  # これ未満のテキスト数ではAPI呼び出しをスキップ

# --- 監視銘柄 ---
WATCHLIST: list[str] = [
    # ハイテク・グロース
    "AAPL", "NVDA", "TSLA", "META", "MSFT",
    # 金融（セクターローテーション対応）
    "JPM", "GS",
    # エネルギー（原油・地政学リスク対応）
    "XOM",
    # 景気敏感（ダウ牽引役）
    "CAT",
    # ヘルスケア（ディフェンシブ）
    "UNH",
]

# --- シグナル閾値 ---
SENTIMENT_THRESHOLD: float = 0.3      # センチメントスコアの最低閾値
CONFIDENCE_MIN: float = 0.6           # LLMの確信度最低値
FLOW_BUY_THRESHOLD: float = 0.65      # 大口買い比率の最低閾値

# --- リスク管理 ---
MAX_DAILY_LOSS_PCT: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.03"))
MAX_DRAWDOWN_PCT: float = float(os.getenv("MAX_DRAWDOWN_PCT", "0.10"))
POSITION_MAX_PCT: float = float(os.getenv("POSITION_MAX_PCT", "0.02"))
KELLY_FRACTION: float = 0.5           # ハーフケリー
MIN_POSITION_SHARES: int = 1          # 最低保証株数（Kelly=0でもデータ蓄積用に発注）
MAX_POSITIONS: int = 3                # 同時保有ポジション上限
CONSECUTIVE_LOSS_LIMIT: int = 3       # 連続敗北でサイズ縮小

# --- ストップロス ---
ATR_SL_MULTIPLIER: float = 1.5        # SL = ATR × 1.5
ATR_TP_MULTIPLIER: float = 2.5        # TP = ATR × 2.5
VWAP_DEVIATION_EXIT: float = 0.02     # VWAP乖離2%で撤退

# --- メインループ ---
LOOP_INTERVAL_SECONDS: int = 30

# --- Discord Webhook 通知 ---
DISCORD_WEBHOOK_SIGNAL: str = os.getenv("DISCORD_WEBHOOK_SIGNAL", "")   # mt-signal チャンネル
DISCORD_WEBHOOK_ALERT: str = os.getenv("DISCORD_WEBHOOK_ALERT", "")     # mt-alert チャンネル
DISCORD_WEBHOOK_SUMMARY: str = os.getenv("DISCORD_WEBHOOK_SUMMARY", "") # mt-summary チャンネル

# --- DB ---
DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost/daytrade")
