"""moomooコミュニティ掲示板からテキストをリアルタイム収集するモジュール."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class Post:
    """掲示板投稿."""
    text: str
    symbol: str
    timestamp: datetime
    author: str = ""
    post_id: str = ""


class BoardScraper:
    """moomooコミュニティ掲示板スクレイパー."""

    BASE_URL = "https://www.moomoo.com/community"

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def fetch_posts(self, symbol: str, limit: int = 50) -> list[Post]:
        """指定銘柄の掲示板投稿を取得する.

        Args:
            symbol: 銘柄シンボル（例: "AAPL"）
            limit: 取得件数上限

        Returns:
            投稿リスト
        """
        session = await self._ensure_session()
        posts: list[Post] = []
        try:
            url = f"{self.BASE_URL}/api/posts"
            params = {"symbol": symbol, "limit": limit}
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning("掲示板取得失敗: %s (status=%d)", symbol, resp.status)
                    return posts
                data = await resp.json()
                for item in data.get("posts", []):
                    posts.append(Post(
                        text=item.get("content", ""),
                        symbol=symbol,
                        timestamp=datetime.fromisoformat(item.get("time", "")),
                        author=item.get("author", ""),
                        post_id=item.get("id", ""),
                    ))
        except Exception:
            logger.exception("掲示板スクレイピングエラー: %s", symbol)
        return posts

    async def stream_new_posts(self, symbol: str, callback: Callable[[Post], None]) -> None:
        """新しい投稿をストリーミングで監視する.

        Args:
            symbol: 銘柄シンボル
            callback: 新規投稿を受け取るコールバック関数
        """
        logger.info("掲示板ストリーム開始: %s", symbol)
        last_post_id: str = ""
        while True:
            posts = await self.fetch_posts(symbol, limit=10)
            for post in posts:
                if post.post_id and post.post_id != last_post_id:
                    callback(post)
                    last_post_id = post.post_id
            import asyncio
            await asyncio.sleep(30)

    async def close(self) -> None:
        """セッションを閉じる."""
        if self._session and not self._session.closed:
            await self._session.close()
