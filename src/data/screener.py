"""動的スクリーニングモジュール.

Finviz で出来高急増の大型株を取得し、
moomoo の大口フローでスコアリングして上位銘柄を返す。
全体失敗時は空リストを返してシステムを止めない。
"""

from __future__ import annotations

import logging
import time as _time
from datetime import date, timedelta

from config import settings
from src.data.moomoo_client import MoomooClient

logger = logging.getLogger(__name__)

# スクリーニング全体のタイムアウト（秒）
SCREENER_TIMEOUT = 300  # 5分


class StockScreener:
    """動的銘柄スクリーナー."""

    def __init__(self, client: MoomooClient) -> None:
        self._client = client

    def get_top_symbols(self, n: int | None = None) -> list[str]:
        """Finviz + moomoo フローで上位銘柄を返す.

        Args:
            n: 返す銘柄数（デフォルト: settings.SCREENER_MAX_SYMBOLS）

        Returns:
            銘柄シンボルのリスト（失敗時は空リスト）
        """
        if n is None:
            n = settings.SCREENER_MAX_SYMBOLS

        start_time = _time.monotonic()

        try:
            # 1) Finviz で候補取得
            candidates = self._fetch_finviz_candidates()
            if not candidates:
                logger.warning("[Screener] Finviz から候補を取得できません")
                return []

            # タイムアウトチェック
            if _time.monotonic() - start_time > SCREENER_TIMEOUT:
                logger.warning("[Screener] タイムアウト (Finviz)")
                return []

            # 2) moomoo で大口フローを確認
            scored = self._score_by_flow(candidates, start_time)
            if not scored:
                logger.warning("[Screener] フロースコアリング結果なし")
                return []

            # 3) スコア上位を返す
            scored.sort(key=lambda x: x[1], reverse=True)
            top = [sym for sym, score in scored[:n] if score > 0]

            logger.info(
                "[Screener] 動的追加: %s (%d銘柄)",
                " ".join(top), len(top),
            )
            return top

        except Exception:
            logger.exception("[Screener] スクリーニング全体エラー")
            return []

    def _fetch_finviz_candidates(self) -> list[str]:
        """Finviz で出来高急増の大型株を取得する."""
        try:
            from finviz.screener import Screener

            filters = [
                "sh_relvol_o2",     # 相対出来高 2倍以上
                "cap_largeover",    # 大型株
                "exch_nasd",        # NASDAQ上場
            ]
            stocks = Screener(
                filters=filters,
                table="Overview",
                order="-volume",
            )
            candidates = [s["Ticker"] for s in stocks[:settings.SCREENER_CANDIDATES]]

            logger.info("[Screener] Finviz: %d銘柄取得", len(candidates))
            return candidates

        except ImportError:
            logger.error("[Screener] finviz パッケージ未インストール: pip install finviz")
            return []
        except Exception:
            logger.exception("[Screener] Finviz 取得エラー")
            return []

    def _score_by_flow(
        self,
        candidates: list[str],
        start_time: float,
    ) -> list[tuple[str, float]]:
        """moomoo の大口フローでスコアリングする."""
        scored: list[tuple[str, float]] = []

        logger.info(
            "[Screener] moomoo flow確認: %d銘柄 (約%d秒)",
            len(candidates), len(candidates),
        )

        for i, symbol in enumerate(candidates):
            # タイムアウトチェック
            if _time.monotonic() - start_time > SCREENER_TIMEOUT:
                logger.warning("[Screener] タイムアウト (%d/%d)", i, len(candidates))
                break

            try:
                flow = self._client.get_institutional_flow(symbol)
                net_flow = flow.net_flow  # big_buy - big_sell
                if net_flow > 0:
                    scored.append((symbol, net_flow))
            except Exception:
                logger.debug("[Screener] フロー取得失敗: %s", symbol)

            # レート制限対策: 1秒スリープ
            _time.sleep(1.0)

        logger.info(
            "[Screener] フロー確認完了: %d/%d銘柄がプラスフロー",
            len(scored), len(candidates),
        )
        return scored
