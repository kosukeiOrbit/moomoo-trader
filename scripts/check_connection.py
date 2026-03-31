"""moomoo OpenD 接続確認スクリプト.

口座開設後にこのスクリプトを1発実行して
全チェックが通過すれば本番運用の準備完了。

Usage:
    python scripts/check_connection.py
"""

from __future__ import annotations

import io
import os
import socket
import sys
import time

# Windows コンソールの文字化け・絵文字対策: stdout を UTF-8 に強制
if sys.platform == "win32":
    os.system("")  # ANSI エスケープシーケンスを有効化
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# プロジェクトルートをパスに追加
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


# ---------------------------------------------------------------------------
# 表示ユーティリティ
# ---------------------------------------------------------------------------

class Colors:
    """ANSI カラーコード."""
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {Colors.GREEN}[OK] {msg}{Colors.RESET}")


def fail(msg: str) -> None:
    print(f"  {Colors.RED}[NG] {msg}{Colors.RESET}")


def warn(msg: str) -> None:
    print(f"  {Colors.YELLOW}[!!] {msg}{Colors.RESET}")


def header(msg: str) -> None:
    print(f"\n{Colors.BOLD}{Colors.CYAN}{msg}{Colors.RESET}")


# ---------------------------------------------------------------------------
# チェック関数
# ---------------------------------------------------------------------------

def check_opend_port() -> bool:
    """Step 1: OpenD がポートで待ち受けているか確認."""
    header(f"[1/5] OpenD 接続テスト (port {settings.MOOMOO_PORT})")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((settings.MOOMOO_HOST, settings.MOOMOO_PORT))
        sock.close()
        if result == 0:
            ok(f"OpenD接続: OK ({settings.MOOMOO_HOST}:{settings.MOOMOO_PORT})")
            return True
        else:
            fail(
                f"OpenD接続: FAILED — ポート {settings.MOOMOO_PORT} に接続できません\n"
                f"         moomoo OpenD を起動してください"
            )
            return False
    except socket.timeout:
        fail("OpenD接続: TIMEOUT — 5秒以内に応答がありませんでした")
        return False
    except OSError as e:
        fail(f"OpenD接続: ERROR — {e}")
        return False


def check_api_connection() -> tuple[bool, object | None]:
    """Step 2: moomoo OpenAPI に接続して認証を確認."""
    header("[2/5] moomoo OpenAPI 認証")
    try:
        from futu import OpenQuoteContext, RET_OK

        quote_ctx = OpenQuoteContext(
            host=settings.MOOMOO_HOST,
            port=settings.MOOMOO_PORT,
        )
        # 簡単なAPIコールで認証を確認
        ret, data = quote_ctx.get_global_state()
        if ret == RET_OK:
            market_status = ""
            if hasattr(data, "iloc"):
                market_status = f" (market: {data.iloc[0].get('market_us', 'N/A')})"
            ok(f"API認証: OK{market_status}")
            return True, quote_ctx
        else:
            fail(f"API認証: FAILED — {data}")
            quote_ctx.close()
            return False, None
    except ImportError:
        fail(
            "API認証: FAILED — moomoo パッケージが見つかりません\n"
            "         pip install futu-api を実行してください"
        )
        return False, None
    except Exception as e:
        fail(f"API認証: ERROR — {e}")
        return False, None


def check_quote_data(quote_ctx: object) -> bool:
    """Step 3: 米国市場の株価を取得."""
    header("[3/5] 株価取得テスト")
    try:
        from futu import RET_OK

        symbols = ["US.AAPL", "US.NVDA"]
        ret, data = quote_ctx.get_market_snapshot(symbols)  # type: ignore[union-attr]
        if ret != RET_OK:
            fail(f"株価取得: FAILED — {data}")
            return False

        prices = {}
        for _, row in data.iterrows():
            code = row["code"]
            last_price = row.get("last_price", row.get("cur_price", 0))
            ticker = code.replace("US.", "")
            prices[ticker] = last_price

        if not prices:
            fail("株価取得: FAILED — データが空です")
            return False

        parts = [f"{sym}=${price:.2f}" for sym, price in prices.items()]
        ok(f"株価取得: {' '.join(parts)}")
        return True

    except Exception as e:
        fail(f"株価取得: ERROR — {e}")
        return False


def check_account_balance() -> bool:
    """Step 4: ペーパートレードアカウントの残高を取得."""
    header("[4/5] 口座残高取得")
    try:
        from futu import OpenSecTradeContext, TrdEnv, TrdMarket, RET_OK

        trd_env = TrdEnv.SIMULATE if settings.TRADE_ENV == "SIMULATE" else TrdEnv.REAL
        env_label = "ペーパートレード" if trd_env == TrdEnv.SIMULATE else "本番"

        trade_ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=settings.MOOMOO_HOST,
            port=settings.MOOMOO_PORT,
        )

        # トレードパスワードでアンロック（本番のみ。ペーパートレードは不要）
        if trd_env != TrdEnv.SIMULATE and settings.MOOMOO_TRADE_PWD:
            ret, data = trade_ctx.unlock_trade(settings.MOOMOO_TRADE_PWD)
            if ret != RET_OK:
                fail(f"トレードアンロック: FAILED — {data}")
                trade_ctx.close()
                return False

        ret, data = trade_ctx.accinfo_query(trd_env=trd_env, currency="USD")
        trade_ctx.close()

        if ret != RET_OK:
            fail(f"口座残高: FAILED — {data}")
            return False

        if data.empty:
            fail("口座残高: FAILED — 口座情報が空です")
            return False

        total_assets = data.iloc[0].get("total_assets", 0)
        cash = data.iloc[0].get("cash", 0)
        ok(
            f"口座残高: ${total_assets:,.2f} ({env_label})\n"
            f"         現金: ${cash:,.2f}"
        )
        return True

    except ImportError:
        fail("口座残高: FAILED — moomoo パッケージが見つかりません")
        return False
    except Exception as e:
        fail(f"口座残高: ERROR — {e}")
        return False


def check_anthropic_api() -> bool:
    """Step 5: Anthropic Claude API の接続確認."""
    header("[5/5] Anthropic Claude API 確認")

    if not settings.ANTHROPIC_API_KEY:
        fail("Claude API: FAILED — ANTHROPIC_API_KEY が .env に設定されていません")
        return False

    if not settings.ANTHROPIC_API_KEY.startswith("sk-ant-"):
        warn("Claude API: APIキーの形式が不正な可能性があります")

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=32,
            messages=[{"role": "user", "content": "Say OK"}],
        )
        text = response.content[0].text.strip()
        ok(f"Claude API: OK (model={settings.CLAUDE_MODEL}, response='{text}')")
        return True
    except ImportError:
        fail(
            "Claude API: FAILED — anthropic パッケージが見つかりません\n"
            "         pip install anthropic を実行してください"
        )
        return False
    except anthropic.AuthenticationError:
        fail("Claude API: FAILED — APIキーが無効です")
        return False
    except anthropic.APIError as e:
        fail(f"Claude API: FAILED — {e}")
        return False
    except Exception as e:
        fail(f"Claude API: ERROR — {e}")
        return False


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    """全チェックを順番に実行する."""
    print(f"\n{Colors.BOLD}{'=' * 50}")
    print(" moomoo-trader 接続確認")
    print(f"{'=' * 50}{Colors.RESET}")
    print(f"  Host: {settings.MOOMOO_HOST}:{settings.MOOMOO_PORT}")
    print(f"  Env:  {settings.TRADE_ENV}")

    results: dict[str, bool] = {}
    quote_ctx = None

    try:
        # Step 1: OpenD ポート
        results["opend"] = check_opend_port()

        if results["opend"]:
            # Step 2: API認証
            api_ok, quote_ctx = check_api_connection()
            results["api"] = api_ok

            if results["api"] and quote_ctx is not None:
                # Step 3: 株価取得
                results["quote"] = check_quote_data(quote_ctx)
            else:
                warn("株価取得: スキップ（API認証が未完了）")
                results["quote"] = False

            # Step 4: 口座残高
            results["account"] = check_account_balance()
        else:
            warn("Step 2-4: スキップ（OpenDに接続できません）")
            results["api"] = False
            results["quote"] = False
            results["account"] = False

        # Step 5: Claude API（OpenDとは独立）
        results["claude"] = check_anthropic_api()

    finally:
        if quote_ctx is not None:
            try:
                quote_ctx.close()  # type: ignore[union-attr]
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 結果サマリー
    # ------------------------------------------------------------------
    header("結果サマリー")
    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for name, ok_flag in results.items():
        status = f"{Colors.GREEN}PASS{Colors.RESET}" if ok_flag else f"{Colors.RED}FAIL{Colors.RESET}"
        print(f"  {name:10s} {status}")

    print()
    if passed == total:
        print(f"  {Colors.GREEN}{Colors.BOLD}>>> 全チェック通過 ({passed}/{total})。本番運用の準備ができています。{Colors.RESET}")
        sys.exit(0)
    elif results.get("opend") is False:
        print(
            f"  {Colors.RED}{Colors.BOLD}>>> OpenD が起動していません。{Colors.RESET}\n"
            f"     1. moomoo アプリを開く\n"
            f"     2. OpenD を起動する（設定 > OpenD）\n"
            f"     3. このスクリプトを再実行"
        )
        sys.exit(1)
    else:
        print(f"  {Colors.YELLOW}{Colors.BOLD}>>> {passed}/{total} チェック通過。上記のエラーを確認してください。{Colors.RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()
