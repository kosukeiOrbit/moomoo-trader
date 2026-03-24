# AI Daytrade Bot — システム設計書
> moomoo OpenAPI × LLM Sentiment × Institutional Flow
> Version: 1.0 | 作成日: 2026-03-24

---

## 1. プロジェクト概要

### 目的
moomoo OpenAPIを使い、LLMセンチメント解析と大口フロー検出を組み合わせた米国株デイトレードの自動売買システムを構築する。

### コアコンセプト
定量シグナル（価格・出来高）は機関投資家のアルゴに支配されており個人に優位性はない。
本システムは以下2つの**個人投資家が使いこなしていない独自優位性**を組み合わせる。

1. **LLMによるテキストセンチメント解析** — 自然言語理解はLLM登場後に個人でも扱えるようになった領域
2. **moomoo独自の大口フロー・空売りデータ** — 一般的な証券APIでは取得できないデータ

### 基本戦略：AND条件二重ロック
```
センチメントスコア ↑ (> +0.3)
        AND
大口フロー 買い超過 (> 閾値)
        ↓
エントリー候補 → リスク計算 → 発注
```
どちらか一方のシグナルのみの場合はスキップ。誤シグナルを構造的に排除する。

---

## 2. システムアーキテクチャ

### 全体構成（5層）

```
┌─────────────────────────────────────────────┐
│  Layer 1: DATA LAYER                        │
│  moomoo OpenAPI / 掲示板 / ニュース          │
└───────────────────┬─────────────────────────┘
                    │
┌───────────────────▼─────────────────────────┐
│  Layer 2: SIGNAL ENGINE                     │
│  LLMセンチメント解析 + 大口フロー検出        │
│  → AND条件フィルター                        │
└───────────────────┬─────────────────────────┘
                    │
┌───────────────────▼─────────────────────────┐
│  Layer 3: RISK MANAGER                      │
│  ポジションサイズ / SL設定 / DD監視          │
└───────────────────┬─────────────────────────┘
                    │
┌───────────────────▼─────────────────────────┐
│  Layer 4: EXECUTION ENGINE                  │
│  moomoo OpenAPI発注 / ペーパートレードモード │
└───────────────────┬─────────────────────────┘
                    │
┌───────────────────▼─────────────────────────┐
│  Layer 5: DASHBOARD                         │
│  P&L監視 / シグナル可視化 / アラート         │
└─────────────────────────────────────────────┘
```

---

## 3. ディレクトリ構成

```
daytrade-bot/
├── README.md
├── .env.example
├── requirements.txt
├── config/
│   └── settings.py          # 全設定値の一元管理
├── src/
│   ├── data/
│   │   ├── moomoo_client.py     # moomoo OpenAPI接続・データ取得
│   │   ├── board_scraper.py     # moomoo掲示板テキスト収集
│   │   └── news_feed.py         # 外部ニュースフィード取得
│   ├── signal/
│   │   ├── sentiment_analyzer.py  # Claude APIによるLLMセンチメント解析
│   │   ├── flow_detector.py       # 大口フロー・空売りデータ検出
│   │   └── and_filter.py          # AND条件フィルター（エントリー判定）
│   ├── risk/
│   │   ├── position_sizer.py    # Kelly基準によるロットサイズ計算
│   │   ├── stop_loss.py         # ATRベース動的SL/TP設定
│   │   └── circuit_breaker.py   # 日次損失上限・ドローダウン監視
│   ├── execution/
│   │   ├── order_router.py      # moomoo OpenAPI発注
│   │   └── paper_trade.py       # ペーパートレードモード
│   ├── monitor/
│   │   ├── pnl_tracker.py       # リアルタイムP&L記録
│   │   └── notifier.py          # Telegram通知
│   └── main.py                  # エントリーポイント・メインループ
├── tests/
│   ├── test_sentiment.py
│   ├── test_flow_detector.py
│   └── test_risk.py
└── scripts/
    └── backtest.py              # バックテスト実行スクリプト
```

---

## 4. 各モジュール仕様

### 4.1 moomoo_client.py

**役割:** moomoo OpenAPIとの接続管理、リアルタイムデータ取得

**取得データ:**
- 株価・板情報・約定データ（WebSocket）
- 空売り比率データ
- 大口投資家フローデータ（moomoo独自）

**主要クラス:**
```python
class MoomooClient:
    def connect(self) -> None
    def subscribe_realtime(self, symbols: list[str]) -> None
    def get_short_data(self, symbol: str) -> ShortData
    def get_institutional_flow(self, symbol: str) -> FlowData
    def place_order(self, order: Order) -> OrderResult
    def close(self) -> None
```

**設定値（config/settings.py）:**
```python
MOOMOO_HOST = "127.0.0.1"
MOOMOO_PORT = 11111
TRADE_ENV = "SIMULATE"  # 本番: "REAL"
WATCHLIST = ["AAPL", "NVDA", "TSLA", "META", "MSFT"]
```

---

### 4.2 board_scraper.py

**役割:** moomooコミュニティ掲示板からテキストをリアルタイム収集

**取得対象:**
- 監視銘柄ごとのコメント・投稿
- 投稿時刻・著者情報（匿名）

**主要クラス:**
```python
class BoardScraper:
    def fetch_posts(self, symbol: str, limit: int = 50) -> list[Post]
    def stream_new_posts(self, symbol: str, callback: Callable) -> None
```

---

### 4.3 sentiment_analyzer.py

**役割:** Claude APIを使い、テキストをBull/Bearスコアに変換する

**仕様:**
- 入力: 掲示板投稿・ニュース記事のテキストリスト
- 出力: -1.0（強気Bearish）〜 +1.0（強気Bullish）のスコア
- 皮肉・ジャーゴン・絵文字も考慮したコンテキスト理解
- 銘柄ごとに直近30分のスコアを移動平均で保持

**主要クラス:**
```python
class SentimentAnalyzer:
    def __init__(self, api_key: str)
    def analyze(self, texts: list[str], symbol: str) -> SentimentResult
    # SentimentResult: score: float, confidence: float, reasoning: str

    def get_rolling_score(self, symbol: str, window_minutes: int = 30) -> float
```

**Claude APIプロンプト方針:**
- モデル: `claude-sonnet-4-20250514`
- システムプロンプトで「株式市場のセンチメント分析専門家」として設定
- JSON形式で `{"score": 0.0, "confidence": 0.0, "reasoning": ""}` を返させる
- バッチ処理で複数テキストを一括分析しAPIコール数を最小化

---

### 4.4 flow_detector.py

**役割:** moomoo独自の大口フローデータを監視し、買い超過・売り超過を検出する

**検出ロジック:**
- 過去15分間の大口フロー累積値を計算
- 買い超過比率 = 大口買い / (大口買い + 大口売り)
- 閾値: `FLOW_BUY_THRESHOLD = 0.65`（65%以上が買いなら買い超過シグナル）
- 空売り比率が急増した場合はショートスクイーズ候補としてフラグ

**主要クラス:**
```python
class FlowDetector:
    def get_flow_signal(self, symbol: str) -> FlowSignal
    # FlowSignal: direction: "BUY"|"SELL"|"NEUTRAL", strength: float, short_squeeze: bool
```

---

### 4.5 and_filter.py

**役割:** センチメントと大口フローの両シグナルを統合し、エントリー判定を行う

**判定ロジック:**
```python
def should_enter(sentiment: SentimentResult, flow: FlowSignal) -> EntryDecision:
    if sentiment.score > SENTIMENT_THRESHOLD:      # default: +0.3
        if flow.direction == "BUY":
            if sentiment.confidence > CONFIDENCE_MIN:  # default: 0.6
                return EntryDecision(go=True, direction="LONG")
    return EntryDecision(go=False)
```

**設定値:**
```python
SENTIMENT_THRESHOLD = 0.3    # センチメントスコアの最低閾値
CONFIDENCE_MIN = 0.6          # LLMの確信度最低値
FLOW_BUY_THRESHOLD = 0.65    # 大口買い比率の最低閾値
```

---

### 4.6 position_sizer.py

**役割:** Kelly基準に基づき最適ポジションサイズを算出する

**計算式:**
```
Kelly% = (勝率 × 平均利益) - (敗率 × 平均損失) / 平均損失
実際のサイズ = Kelly% × 0.5  # ハーフケリーで保守的に
上限 = 総資金の2%
```

**主要クラス:**
```python
class PositionSizer:
    def calculate(self, symbol: str, price: float, account_balance: float) -> int
    def update_stats(self, trade_result: TradeResult) -> None  # 勝率を動的更新
```

---

### 4.7 stop_loss.py

**役割:** ATR（Average True Range）ベースで動的にSL/TPを設定する

**ロジック:**
- SL = エントリー価格 - (ATR × 1.5)
- TP = エントリー価格 + (ATR × 2.5)  → リスクリワード比 1:1.67
- VWAPからの乖離が2%を超えた場合は即時撤退

**主要クラス:**
```python
class StopLossManager:
    def calculate_levels(self, symbol: str, entry_price: float) -> Levels
    # Levels: stop_loss: float, take_profit: float, trailing_stop: float
```

---

### 4.8 circuit_breaker.py

**役割:** 異常時の自動停止とリスク管理

**発動条件:**
- 日次損失が資金の3%を超えた → 当日の全新規発注を停止
- 最大ドローダウンが10%を超えた → 全ポジション強制決済・システム停止
- 連続3敗 → ポジションサイズを50%に縮小

```python
class CircuitBreaker:
    def check(self, account_state: AccountState) -> BreakerStatus
    def reset_daily(self) -> None  # 毎朝9:30（ET）に自動リセット
```

---

### 4.9 order_router.py

**役割:** moomoo OpenAPI経由での発注・決済管理

**発注フロー:**
1. `circuit_breaker.check()` で安全確認
2. `paper_trade` フラグ確認
3. 成行 or 指値で発注
4. 注文IDを記録し、SL/TPを監視ループに登録

```python
class OrderRouter:
    def enter(self, signal: EntryDecision, size: int) -> Order
    def exit(self, order_id: str, reason: str) -> None
    def monitor_positions(self) -> None  # 非同期ループ
```

---

### 4.10 main.py

**役割:** 全モジュールのオーケストレーション

**メインループ（疑似コード）:**
```python
async def main_loop():
    while market_is_open():
        for symbol in WATCHLIST:
            # データ収集
            posts = board_scraper.fetch_posts(symbol)
            news  = news_feed.get_latest(symbol)
            flow  = flow_detector.get_flow_signal(symbol)

            # シグナル生成
            sentiment = sentiment_analyzer.analyze(posts + news, symbol)
            decision  = and_filter.should_enter(sentiment, flow)

            # リスク計算 → 発注
            if decision.go:
                size   = position_sizer.calculate(symbol, current_price, balance)
                levels = stop_loss_manager.calculate_levels(symbol, current_price)
                order  = order_router.enter(decision, size)
                pnl_tracker.register(order, levels)

        await asyncio.sleep(LOOP_INTERVAL_SECONDS)  # default: 30
```

---

## 5. 技術スタック

| カテゴリ | ライブラリ | 用途 |
|---------|-----------|------|
| API接続 | `moomoo-openapi` (公式SDK) | 株価取得・発注 |
| 非同期 | `asyncio`, `aiohttp` | 低レイテンシ非同期処理 |
| LLM | `anthropic` (Claude API) | センチメント解析 |
| データ処理 | `pandas`, `numpy` | 価格・フロー集計 |
| テクニカル | `pandas-ta` | ATR・VWAP計算 |
| DB | `PostgreSQL` + `TimescaleDB` | 時系列データ保存 |
| 監視 | `Grafana` | P&L・シグナルダッシュボード |
| 通知 | `python-telegram-bot` | Telegramアラート |
| テスト | `pytest`, `pytest-asyncio` | 単体・統合テスト |

**Pythonバージョン:** 3.11+

**requirements.txt（主要パッケージ）:**
```
moomoo-openapi>=4.0.0
anthropic>=0.20.0
pandas>=2.0.0
pandas-ta>=0.3.14b
numpy>=1.24.0
aiohttp>=3.9.0
asyncpg>=0.29.0
python-telegram-bot>=21.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
python-dotenv>=1.0.0
```

---

## 6. 環境変数（.env.example）

```env
# moomoo OpenAPI
MOOMOO_HOST=127.0.0.1
MOOMOO_PORT=11111
MOOMOO_TRADE_PWD=your_password

# Anthropic Claude API
ANTHROPIC_API_KEY=sk-ant-...

# 取引設定
TRADE_ENV=SIMULATE          # SIMULATE or REAL
MAX_DAILY_LOSS_PCT=0.03     # 日次最大損失 3%
MAX_DRAWDOWN_PCT=0.10       # 最大ドローダウン 10%
POSITION_MAX_PCT=0.02       # 1ポジション最大 2%

# Telegram通知
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id

# DB
DATABASE_URL=postgresql://user:pass@localhost/daytrade
```

---

## 7. 実装ロードマップ

### Phase 1: 基盤構築（Week 1-2）
- [ ] moomoo OpenAPI接続・ペーパートレード動作確認
- [ ] `MoomooClient` の実装とユニットテスト
- [ ] `config/settings.py` の整備

### Phase 2: シグナルエンジン（Week 3-4）
- [ ] `SentimentAnalyzer` 実装（Claude API連携）
- [ ] `FlowDetector` 実装・閾値チューニング
- [ ] `AndFilter` 実装・ロジック検証
- [ ] 過去データで精度測定

### Phase 3: リスク管理（Week 5）
- [ ] `PositionSizer` 実装（ハーフKelly）
- [ ] `StopLossManager` 実装（ATRベース）
- [ ] `CircuitBreaker` 実装・テスト

### Phase 4: 統合・バックテスト（Week 6-7）
- [ ] `main.py` で全モジュール統合
- [ ] `backtest.py` で過去3ヶ月データ検証
- [ ] パフォーマンス指標確認（シャープレシオ > 1.5 を目標）

### Phase 5: 本番デプロイ（Week 8〜）
- [ ] `TRADE_ENV=SIMULATE` で2週間ライブペーパートレード
- [ ] 問題なければ小額実弾（$500程度）でスタート
- [ ] Grafanaダッシュボード整備
- [ ] Telegram通知セットアップ

---

## 8. 将来拡張：強化学習（RL）統合

本設計はRL追加を想定した構造になっている。

**現在のAND条件フィルターをRLエージェントに段階的に置き換える:**

```
Phase 1（現在）: ルールベース AND条件
Phase 2: RLエージェントがAND条件の閾値を動的最適化
Phase 3: RLエージェントが特徴量の重みを自律決定
```

**状態空間（State）として渡せる特徴量:**
- センチメントスコア（-1.0〜+1.0）
- センチメント移動平均（5分・15分・30分）
- 大口フロー強度（0.0〜1.0）
- 空売り比率
- VWAPからの価格乖離率
- ATR（ボラティリティ指標）
- 時間帯（寄り付き・昼・引け）

**報酬関数:**
```
報酬 = リスクリワード比を考慮した利益 - ドローダウンペナルティ
```

**注意:** RLはデータ量が必要なため、最低3〜6ヶ月のライブデータ蓄積後に着手を推奨。
moomooのペーパートレード機能をGym環境として活用できる。

---

## 9. Claude Codeへの実装依頼メモ

本設計書に基づき、以下の順序で実装を依頼する:

1. **まず `config/settings.py` と `.env.example` を作成**
2. **`src/data/moomoo_client.py` を実装** — moomoo OpenAPI SDK公式ドキュメントを参照
3. **`src/signal/sentiment_analyzer.py` を実装** — Anthropic Python SDKを使用
4. **`src/signal/flow_detector.py` を実装**
5. **`src/signal/and_filter.py` を実装**
6. **`src/risk/` 配下を実装**
7. **`src/execution/order_router.py` を実装**
8. **`src/main.py` で統合**
9. **`tests/` 配下にユニットテストを作成**

**各モジュールに求める品質:**
- 型ヒント（Type Hints）を全関数に付与
- docstringで仕様を明記
- ユニットテストをセットで作成
- 非同期処理（async/await）を活用し低レイテンシを維持
- エラーハンドリング（API接続断・レート制限）を適切に実装
```
