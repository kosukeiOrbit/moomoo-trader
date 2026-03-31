"""Reddit RSS を使った銘柄関連テキスト収集モジュール.

r/wallstreetbets, r/stocks の RSS フィードから
銘柄シンボルに言及した投稿を収集する。
APIキー不要。取得失敗時は空リストを返してシステムを止めない。

フィードはキャッシュし、複数銘柄で共有して Reddit のレート制限を回避する。
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

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

# フィードキャッシュの有効期間（秒）
CACHE_TTL_SECONDS = 120


@dataclass
class Post:
    """掲示板投稿."""

    text: str
    symbol: str
    timestamp: datetime
    author: str = ""
    post_id: str = ""


@dataclass
class _CachedFeed:
    """フィードのキャッシュエントリ."""

    entries: list[dict[str, Any]]
    fetched_at: float  # time.monotonic()


class BoardScraper:
    """Reddit RSS ベースの掲示板スクレイパー.

    フィードを CACHE_TTL_SECONDS 間キャッシュし、
    複数銘柄で共有して Reddit へのリクエスト数を最小化する。
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, _CachedFeed] = {}

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "moomoo-trader/1.0"},
            )
        return self._session

    async def fetch_posts(self, symbol: str, limit: int = 30) -> list[Post]:
        """指定銘柄に言及した Reddit 投稿を取得する.

        フィードはキャッシュから取得し、シンボルフィルターのみ適用。

        Args:
            symbol: 銘柄シンボル (例: "AAPL")
            limit: 取得件数上限

        Returns:
            投稿リスト（取得失敗時は空リスト）
        """
        posts: list[Post] = []
        for feed_url in REDDIT_FEEDS:
            entries = await self._get_entries(feed_url)
            filtered = self._filter_entries(entries, symbol)
            posts.extend(filtered)

        posts.sort(key=lambda p: p.timestamp, reverse=True)
        return posts[:limit]

    async def _get_entries(self, feed_url: str) -> list[dict[str, Any]]:
        """フィードのエントリをキャッシュ付きで取得する."""
        import time as _time

        cached = self._cache.get(feed_url)
        if cached and (_time.monotonic() - cached.fetched_at) < CACHE_TTL_SECONDS:
            return cached.entries

        # キャッシュミス → フェッチ
        entries = await self._fetch_feed(feed_url)
        self._cache[feed_url] = _CachedFeed(
            entries=entries,
            fetched_at=_time.monotonic(),
        )
        return entries

    async def _fetch_feed(self, feed_url: str) -> list[dict[str, Any]]:
        """Reddit RSS (Atom) をフェッチしてエントリリストを返す."""
        session = await self._ensure_session()
        entries: list[dict[str, Any]] = []

        try:
            async with session.get(feed_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning("Reddit RSS %d: %s", resp.status, feed_url)
                    return entries

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

                    entries.append({
                        "title": title,
                        "content": content,
                        "updated": updated_str,
                        "author": author,
                        "id": entry_id,
                    })

        except aiohttp.ClientError as e:
            logger.warning("Reddit RSS network error: %s — %s", feed_url, e)
        except ET.ParseError as e:
            logger.warning("Reddit RSS XML parse error: %s — %s", feed_url, e)
        except Exception:
            logger.exception("Reddit RSS unexpected error: %s", feed_url)

        return entries

    def _filter_entries(
        self,
        entries: list[dict[str, Any]],
        symbol: str,
    ) -> list[Post]:
        """エントリリストからシンボルに言及したものを抽出する."""
        posts: list[Post] = []
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=MAX_AGE_MINUTES)

        for e in entries:
            combined = f"{e['title']} {e['content']}"
            if not self._mentions_symbol(combined, symbol):
                continue

            published_at = self._parse_atom_date(e["updated"])
            if published_at and published_at < cutoff:
                continue

            posts.append(Post(
                text=f"{e['title']} {e['content']}"[:500],
                symbol=symbol,
                timestamp=published_at or datetime.now(timezone.utc),
                author=e["author"],
                post_id=e["id"],
            ))

        return posts

    @staticmethod
    def _mentions_symbol(text: str, symbol: str) -> bool:
        """テキストが銘柄シンボルに言及しているか判定する."""
        upper = text.upper()
        if f"${symbol.upper()}" in upper:
            return True
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
