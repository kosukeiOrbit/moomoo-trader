"""動的スクリーニング結果の読み込みモジュール.

scripts/screener.py が生成した data/watchlist_dynamic.json を読み込む。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
WATCHLIST_PATH = _PROJECT_ROOT / "data" / "watchlist_dynamic.json"

# 96時間（4日）以上古いファイルは無効（3連休対応）
MAX_AGE_HOURS = 96


def get_dynamic_symbols(n: int | None = None) -> list[str]:
    """data/watchlist_dynamic.json から動的銘柄リストを読み込む.

    Args:
        n: 返す銘柄数（デフォルト: settings.SCREENER_MAX_SYMBOLS）

    Returns:
        銘柄シンボルのリスト（ファイルなし or 古すぎる場合は空リスト）
    """
    if n is None:
        n = settings.SCREENER_MAX_SYMBOLS

    if not WATCHLIST_PATH.exists():
        logger.info("[Screener] watchlist_dynamic.json なし → スキップ")
        return []

    try:
        data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[Screener] watchlist_dynamic.json 読み込みエラー: %s", e)
        return []

    # 鮮度チェック
    generated_at_str = data.get("generated_at", "")
    if generated_at_str:
        try:
            generated_at = datetime.fromisoformat(generated_at_str)
            age_hours = (datetime.now() - generated_at).total_seconds() / 3600
            if age_hours > MAX_AGE_HOURS:
                logger.warning(
                    "[Screener] watchlist_dynamic.json が古すぎる (%.1f時間) → スキップ",
                    age_hours,
                )
                return []
        except (ValueError, TypeError):
            logger.warning("[Screener] generated_at のパース失敗")

    symbols = data.get("symbols", [])[:n]
    if symbols:
        logger.info("[Screener] 動的WATCHLIST読み込み: %s (%d銘柄)", " ".join(symbols), len(symbols))
    else:
        logger.info("[Screener] 動的WATCHLIST: 0銘柄")

    return symbols
