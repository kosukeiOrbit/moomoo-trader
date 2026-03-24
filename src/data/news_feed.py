"""外部ニュースフィード取得モジュール."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class NewsArticle:
    """ニュース記事."""
    title: str
    body: str
    symbol: str
    source: str
    published_at: datetime
    url: str = ""


class NewsFeed:
    """ニュースフィード取得クライアント."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_latest(self, symbol: str, limit: int = 20) -> list[NewsArticle]:
        """指定銘柄の最新ニュースを取得する.

        Args:
            symbol: 銘柄シンボル
            limit: 取得件数上限

        Returns:
            ニュース記事リスト
        """
        session = await self._ensure_session()
        articles: list[NewsArticle] = []
        try:
            # moomoo OpenAPI のニュース取得エンドポイントを利用
            url = f"https://api.moomoo.com/news/stock/{symbol}"
            params = {"limit": limit}
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning("ニュース取得失敗: %s (status=%d)", symbol, resp.status)
                    return articles
                data = await resp.json()
                for item in data.get("articles", []):
                    articles.append(NewsArticle(
                        title=item.get("title", ""),
                        body=item.get("body", ""),
                        symbol=symbol,
                        source=item.get("source", ""),
                        published_at=datetime.fromisoformat(item.get("published_at", "")),
                        url=item.get("url", ""),
                    ))
        except Exception:
            logger.exception("ニュース取得エラー: %s", symbol)
        return articles

    async def close(self) -> None:
        """セッションを閉じる."""
        if self._session and not self._session.closed:
            await self._session.close()
