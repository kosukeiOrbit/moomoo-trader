# AI Daytrade Bot — システム設計書
> moomoo OpenAPI × LLM Sentiment × Institutional Flow
> Version: 2.0 | 更新日: 2026-03-25

---

## 1. プロジェクト概要

### 目的
moomoo OpenAPIを使い、LLMセンチメント解析と大口フロー検出を組み合わせた米国株デイトレードの自動売買システムを構築する。

### コアコンセプト
定量シグナル（価格・出来高）は機関投資家のアルゴに支配されており個人に優位性はない。
本システムは以下2つの**個人投資家が使いこなしていない独自優位性**を組み合わせる。

1. **LLMによるテキストセンチメント解析** — 自然言語理解はLLM登場後に個人でも扱えるようになった領域
2. **moomoo独自の大口フロー・空売りデータ** — 一般的な証券APIでは取得できないデータ

### 基本戦略：AND条件四重ロック
```
① sentiment.score        > +0.3
② flow.direction         == "BUY"
③ sentiment.confidence   > 0.6
④ flow.strength          > 0.65
          ↓ 全条件クリア
  エントリー候補 → リスク計算 → 発注
```
1条件でも未達の場合はスキップ。不合格理由をログに記録する。

### 対象市場
- **米国株（メイン）** — moomoo OpenAPI対応済み
- 日本株は現時点でOpenAPIの発注非対応のため対象外

### 稼働時間（Windows・ローカルPC運用）
```
23:20  タスクスケジューラがBot自動起動
23:30  米国市場オープン・監視スタート
05:50  未決済ポジションを全決済
06:10  Bot自動停止
```

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
│  → AND条件フィルター（4条件）               │
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
│  P&L監視 / シグナル可視化 / Discordアラート  │
└─────────────────────────────────────────────┘
```

---

## 3. ディレクトリ構成

```
moomoo-trader/
├── README.md
├── DESIGN.md
├── .env.example
├── .gitignore
├── requirements.txt
├── config/
│   └── settings.py              # 全設定値の一元管理
├── src/
│   ├── data/
│   │   ├── moomoo_client.py     # moomoo OpenAPI接続・データ取得 ⬜
│   │   ├── board_scraper.py     # moomoo掲示板テキスト収集 ⬜
│   │   └── news_feed.py         # 外部ニュースフィード取得 ⬜
│   ├── signal/
│   │   ├── sentiment_analyzer.py  # Claude APIセンチメント解析 ✅
│   │   ├── flow_detector.py       # 大口フロー・空売り検出 ⬜
│   │   └── and_filter.py          # AND条件フィルター（4条件） ✅
│   ├── risk/
│   │   ├── position_sizer.py    # ハーフKellyロットサイズ計算 ✅
│   │   ├── stop_loss.py         # ATRベース動的SL/TP設定 ✅
│   │   └── circuit_breaker.py   # サーキットブレーカー ✅
│   ├── execution/
│   │   ├── order_router.py      # moomoo OpenAPI発注 ⬜
│   │   └── paper_trade.py       # ペーパートレードモード ⬜
│   ├── monitor/
│   │   ├── pnl_tracker.py       # リアルタイムP&L記録 ✅
│   │   └── notifier.py          # Discord Webhook通知 ✅
│   └── main.py                  # エントリーポイント・メインループ ⬜
├── tests/
│   ├── test_sentiment.py        # 23テスト ✅
│   ├── test_and_filter.py       # 18テスト ✅
│   ├── test_position_sizer.py   # 19テスト ✅
│   ├── test_stop_loss.py        # 16テスト ✅
│   ├── test_circuit_breaker.py  # 19テスト ✅
│   ├── test_notifier.py         # 15テスト ✅
│   └── test_pnl_tracker.py      # 35テスト ✅
└── scripts/
    ├── check_connection.py      # 接続前チェック（5ステップ） ✅
    └── backtest.py              # バックテスト実行 ⬜
```

**実装進捗: 145テスト合格済み**

---

## 4. 実装済みモジュール詳細

### 4.1 sentiment_analyzer.py ✅
**テスト: 23件（ユニット19 + 統合4）**

- モデル: `claude-sonnet-4-20250514`
- 入力: テキストリスト + 銘柄コード
- 出力: `SentimentResult(score: float, confidence: float, reasoning: str)`
- scoreは`[-1.0, +1.0]`、confidenceは`[0.0, 1.0]`にクランプ
- `RateLimitError` / `APIConnectionError` で指数バックオフリトライ（最大3回: 1s→2s→4s）
- JSONパース: 前後に余計なテキストがあっても`{}`を抽出
- 履歴の自動プルーニング: 60分以上前のデータを自動削除
- `get_rolling_score(window_minutes=30)` で移動平均スコアを取得

### 4.2 and_filter.py ✅
**テスト: 18件（全条件OK・各条件単独NG・境界値・複数条件未達）**

4条件のANDロジック:
```python
① sentiment.score        > SENTIMENT_THRESHOLD  # 0.3
② flow.direction         == "BUY"
③ sentiment.confidence   > CONFIDENCE_MIN       # 0.6
④ flow.strength          > FLOW_BUY_THRESHOLD   # 0.65
```
不合格時は未達条件を全てreason文字列に列挙（デバッグ・ログ用）

### 4.3 position_sizer.py ✅
**テスト: 19件**

- ハーフKelly基準: `Kelly% = (勝率×利益 - 敗率×損失) / 損失 × 0.5`
- 上限: 総資金の`POSITION_MAX_PCT`（2%）
- 初期勝率50%（データなし時）→ Kelly=0で賭けない
- 連続3敗でサイズを50%に縮小
- `update_stats()`で勝率を動的更新

### 4.4 stop_loss.py ✅
**テスト: 16件**

- SL: `entry - ATR × 1.5`
- TP: `entry + ATR × 2.5`（R:R = 1:1.67）
- フォールバック: 価格履歴<14本 → ATR = entry × 2%
- `calculate_vwap()` でVWAP乖離>2%を検知して撤退フラグ

### 4.5 circuit_breaker.py ✅
**テスト: 19件**

優先度順の発動条件:
1. DD > 10% → `FORCE_CLOSE_ALL`（全ポジ強制決済・システム停止）
2. 日次損失 > 3% → `HALT_NEW_ORDERS`（新規発注停止）
3. 連続3敗 → `REDUCE_SIZE`（サイズ50%縮小、取引継続）

`is_halted`プロパティ、`reset_daily()`で毎朝リセット

### 4.6 notifier.py ✅
**テスト: 15件**

Discord Webhook経由で3チャンネルに通知:

| メソッド | チャンネル | Embed色 |
|---------|-----------|---------|
| `notify_signal()` | mt-signal | 青 #3498DB |
| `notify_entry()` | mt-signal | 緑 #2ECC71 |
| `notify_exit()` 利益 | mt-alert | 緑 #2ECC71 |
| `notify_exit()` 損失 | mt-alert | 赤 #E74C3C |
| `notify_circuit_breaker()` | mt-alert | 赤 + @everyone |
| `notify_daily_summary()` | mt-summary | PnLに応じて緑/赤 |

送信失敗時は`False`を返しログ記録、システムは止めない

### 4.7 pnl_tracker.py ✅
**テスト: 35件**

| 機能 | メソッド |
|-----|---------|
| トレード記録 | `register()` / `close_trade()` |
| 勝率 | `get_win_rate(last_n)` |
| 最大DD | `get_max_drawdown()` |
| シャープレシオ | `get_sharpe_ratio()` |
| 日次サマリー | `get_daily_summary()` |
| CSV保存 | `save_to_csv()` / `load_from_csv()` |

PostgreSQL不要でCSVのみで動作（後からDB追加可能）

### 4.8 check_connection.py ✅

5ステップの接続前チェック:
```
Step 1/5  OpenD ポート接続確認 (127.0.0.1:11111)
Step 2/5  moomoo API認証
Step 3/5  株価取得 (AAPL / NVDA)
Step 4/5  口座残高確認（ペーパートレード）
Step 5/5  Claude API接続確認（独立実行）
```
OpenD未起動時はStep2-4をスキップしStep5のみ実行。
全PASS → exit 0 / 一部FAIL → 対処手順を表示してexit 1

---

## 5. 未実装モジュール（口座開設後に着手）

### 5.1 moomoo_client.py ⬜

```python
class MoomooClient:
    def connect(self) -> None
    def subscribe_realtime(self, symbols: list[str]) -> None
    def get_short_data(self, symbol: str) -> ShortData
    def get_institutional_flow(self, symbol: str) -> FlowData
    def place_order(self, order: Order) -> OrderResult
    def close(self) -> None
```

### 5.2 flow_detector.py ⬜

- 過去15分間の大口フロー累積値を計算
- 買い超過比率 = 大口買い / (大口買い + 大口売り)
- 空売り比率急増でショートスクイーズ候補フラグ

```python
class FlowDetector:
    def get_flow_signal(self, symbol: str) -> FlowSignal
    # FlowSignal: direction: "BUY"|"SELL"|"NEUTRAL", strength: float, short_squeeze: bool
```

### 5.3 order_router.py ⬜

```python
class OrderRouter:
    def enter(self, signal: EntryDecision, size: int) -> Order
    def exit(self, order_id: str, reason: str) -> None
    def monitor_positions(self) -> None  # 非同期ループ
```

### 5.4 main.py ⬜

```python
async def main_loop():
    while market_is_open():
        for symbol in WATCHLIST:
            posts     = board_scraper.fetch_posts(symbol)
            news      = news_feed.get_latest(symbol)
            flow      = flow_detector.get_flow_signal(symbol)
            sentiment = sentiment_analyzer.analyze(posts + news, symbol)
            decision  = and_filter.should_enter(sentiment, flow)

            if decision.go:
                size   = position_sizer.calculate(symbol, price, balance)
                levels = stop_loss_manager.calculate_levels(symbol, price)
                order  = order_router.enter(decision, size)
                pnl_tracker.register(order, levels)

        await asyncio.sleep(LOOP_INTERVAL_SECONDS)
```

---

## 6. 技術スタック

| カテゴリ | ライブラリ | 用途 |
|---------|-----------|------|
| API接続 | `moomoo-openapi` (公式SDK) | 株価取得・発注 |
| 非同期 | `asyncio`, `aiohttp` | 低レイテンシ非同期処理 |
| LLM | `anthropic` (Claude API) | センチメント解析 |
| データ処理 | `pandas`, `numpy` | 価格・フロー集計 |
| テクニカル | `pandas-ta` | ATR・VWAP計算 |
| HTTP | `requests` | Discord Webhook送信 |
| DB | `PostgreSQL` + `TimescaleDB` | 時系列データ保存（将来） |
| 通知 | Discord Webhook | 3チャンネル通知 |
| テスト | `pytest`, `pytest-asyncio` | 単体・統合テスト |

**Pythonバージョン:** 3.11+

---

## 7. 環境変数（.env.example）

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
LOOP_INTERVAL_SECONDS=30    # メインループ間隔

# Discord Webhook（3チャンネル）
DISCORD_WEBHOOK_SIGNAL=https://discord.com/api/webhooks/...   # mt-signal
DISCORD_WEBHOOK_ALERT=https://discord.com/api/webhooks/...    # mt-alert
DISCORD_WEBHOOK_SUMMARY=https://discord.com/api/webhooks/...  # mt-summary
```

---

## 8. シグナル閾値設定（config/settings.py）

```python
SENTIMENT_THRESHOLD   = 0.3    # センチメントスコア最低値
CONFIDENCE_MIN        = 0.6    # LLM確信度最低値
FLOW_BUY_THRESHOLD    = 0.65   # 大口買い比率最低値
FLOW_WINDOW_MINUTES   = 15     # 大口フロー集計ウィンドウ
SENTIMENT_WINDOW_MIN  = 30     # センチメント移動平均ウィンドウ
VWAP_DEVIATION_MAX    = 0.02   # VWAP乖離撤退閾値（2%）
ATR_SL_MULTIPLIER     = 1.5    # SL = entry - ATR × 1.5
ATR_TP_MULTIPLIER     = 2.5    # TP = entry + ATR × 2.5
CONSECUTIVE_LOSS_MAX  = 3      # 連続敗北でサイズ縮小
```

---

## 9. 実装ロードマップ

### Phase 1: 完了済み ✅
- [x] sentiment_analyzer.py（23テスト・実API確認済み）
- [x] and_filter.py（18テスト）
- [x] position_sizer.py（19テスト）
- [x] stop_loss.py（16テスト）
- [x] circuit_breaker.py（19テスト）
- [x] notifier.py（15テスト・Discord Webhook対応）
- [x] pnl_tracker.py（35テスト）
- [x] check_connection.py（5ステップ確認）

### Phase 2: 口座開設後 ⬜
- [ ] moomoo OpenAPI申請・承認
- [ ] OpenD Windows版インストール・起動
- [ ] `python scripts/check_connection.py` で全ステップ確認
- [ ] moomoo_client.py 実装・テスト
- [ ] flow_detector.py 実装・テスト
- [ ] board_scraper.py 実装
- [ ] order_router.py 実装
- [ ] main.py 統合

### Phase 3: 統合・検証 ⬜
- [ ] ペーパートレードで2週間ライブ動作確認
- [ ] backtest.py で過去データ検証
- [ ] シャープレシオ > 1.5 を確認
- [ ] 小額実弾（$500程度）でスタート

### Phase 4: 将来拡張 ⬜
- [ ] 強化学習（RL）エージェント導入
- [ ] 監視銘柄の拡大
- [ ] PostgreSQL + TimescaleDB 導入
- [ ] Grafanaダッシュボード整備

---

## 10. 将来拡張：強化学習（RL）統合

本設計はRL追加を想定した構造になっている。
`and_filter.py`のAND条件をRLエージェントに段階的に置き換えるだけで移行可能。

```
Phase 1（現在）: ルールベース AND条件
Phase 2:        RLエージェントが閾値を動的最適化
Phase 3:        RLエージェントが特徴量の重みを自律決定
```

**状態空間（State）として使える特徴量:**
- センチメントスコア・移動平均（5分・15分・30分）
- 大口フロー強度・空売り比率
- VWAPからの価格乖離率・ATR
- 時間帯（寄り付き・昼・引け）

**注意:** RLはデータ量が必要なため、最低3〜6ヶ月のライブデータ蓄積後に着手を推奨。

---

## 11. Claude Codeへの次の実装依頼（口座開設後）

```
moomoo OpenAPIの承認が完了しました。
scripts/check_connection.py が全ステップPASSしました。

DESIGN.mdのセクション5に従い以下を実装してください:

1. src/data/moomoo_client.py
   - moomoo OpenAPI公式SDKを使用
   - WebSocket経由のリアルタイム株価取得
   - 大口フロー・空売りデータの取得
   - ペーパートレード / 本番の切り替え対応

2. src/signal/flow_detector.py
   - MoomooClientから大口フローデータを取得
   - 15分ウィンドウで買い超過比率を計算
   - FlowSignal(direction, strength, short_squeeze)を返す

3. src/execution/order_router.py
   - CircuitBreakerの確認後に発注
   - SL/TPをStopLossManagerから取得して設定
   - 非同期でポジション監視ループを実行

各モジュールに型ヒント・docstring・ユニットテストをセットで作成。
```
