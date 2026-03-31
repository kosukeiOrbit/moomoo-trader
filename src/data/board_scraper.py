"""Reddit RSS を使った銘柄関連テキスト収集モジュール.

r/wallstreetbets, r/stocks の RSS フィードから
銘柄シンボルに言及した投稿を収集する。
APIキー不要。取得失敗時は空リストを返してシステムを止めない。
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

logger = logging.getLogger(__name__)

# Reddit RSS (APIキー不要、.rss を付けるだけ)
REDDIT_FEEDS = [
    "https://www.reddit.com/r/wallstreetbets/new/.rss",
    "https://www.reddit.com/r/stocks/new/.rss",
]

# Atom namespace
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# 投稿の鮮度フィルター（直近N分）
MAX_AGE_MINUTES = 60


@dataclass
class Post:
    """掲示板投稿."""

    text: str
    symbol: str
    timestamp: datetime
    author: str = ""
    post_id: str = ""


class BoardScraper:
    """Reddit RSS ベースの掲示板スクレイパー."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "moomoo-trader/1.0"},
            )
        return self._session

    async def fetch_posts(self, symbol: str, limit: int = 30) -> list[Post]:
        """指定銘柄に言及した Reddit 投稿を取得する.

        r/wallstreetbets と r/stocks の新着投稿から
        銘柄シンボルを含むものをフィルタリングする。

        Args:
            symbol: 銘柄シンボル (例: "AAPL")
            limit: 取得件数上限

        Returns:
            投稿リスト（取得失敗時は空リスト）
        """
        posts: list[Post] = []
        for feed_url in REDDIT_FEEDS:
            fetched = await self._fetch_reddit_rss(feed_url, symbol)
            posts.extend(fetched)

        # 新しい順にソートして上限まで
        posts.sort(key=lambda p: p.timestamp, reverse=True)
        return posts[:limit]

    async def _fetch_reddit_rss(self, feed_url: str, symbol: str) -> list[Post]:
        """Reddit RSS (Atom) から銘柄に言及した投稿を取得する."""
        session = await self._ensure_session()
        posts: list[Post] = []
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=MAX_AGE_MINUTES)

        try:
            async with session.get(feed_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning("Reddit RSS failed: %s (status=%d)", feed_url, resp.status)
                    return posts

                text = await resp.text()
                root = ET.fromstring(text)

                for entry in root.findall("atom:entry", ATOM_NS):
                    title = entry.findtext("atom:title", "", ATOM_NS)
                    content_el = entry.find("atom:content", ATOM_NS)
                    content = content_el.text if content_el is not None and content_el.text else ""
                    updated_str = entry.findtext("atom:updated", "", ATOM_NS)
                    author_el = entry.find("atom:author/atom:name", ATOM_NS)
                    author = author_el.text if author_el is not None and author_el.text else ""
                    entry_id = entry.findtext("atom:id", "", ATOM_NS)

                    # シンボルフィルター: $AAPL or "AAPL" (大文字)
                    combined = f"{title} {content}"
                    if not self._mentions_symbol(combined, symbol):
                        continue

                    # 日付パース
                    published_at = self._parse_atom_date(updated_str)
                    if published_at and published_at < cutoff:
                        continue

                    posts.append(Post(
                        text=f"{title} {content}"[:500],  # 500文字に制限
                        symbol=symbol,
                        timestamp=published_at or datetime.now(timezone.utc),
                        author=author,
                        post_id=entry_id,
                    ))

        except aiohttp.ClientError as e:
            logger.warning("Reddit RSS network error: %s — %s", feed_url, e)
        except ET.ParseError as e:
            logger.warning("Reddit RSS XML parse error: %s — %s", feed_url, e)
        except Exception:
            logger.exception("Reddit RSS unexpected error: %s", feed_url)

        logger.debug("Reddit RSS: %s %s -> %d posts", feed_url[:40], symbol, len(posts))
        return posts

    @staticmethod
    def _mentions_symbol(text: str, symbol: str) -> bool:
        """テキストが銘柄シンボルに言及しているか判定する.

        $AAPL, AAPL, Apple (AAPL) などのパターンを検出する。
        """
        upper = text.upper()
        # $AAPL パターン
        if f"${symbol.upper()}" in upper:
            return True
        # 単語として含まれるか（前後が非英数字）
        sym = symbol.upper()
        for i in range(len(upper) - len(sym) + 1):
            if upper[i:i + len(sym)] == sym:
                before_ok = (i == 0) or not upper[i - 1].isalpha()
                after_ok = (i + len(sym) >= len(upper)) or not upper[i + len(sym)].isalpha()
                if before_ok and after_ok:
                    return True
        return False

    @staticmethod
    def _parse_atom_date(date_str: str) -> datetime | None:
        """Atom の updated (ISO 8601) をパースする."""
        if not date_str:
            return None
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    async def close(self) -> None:
        """セッションを閉じる."""
        if self._session and not self._session.closed:
            await self._session.close()
