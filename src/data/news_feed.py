"""Yahoo Finance RSS を使ったニュースフィード取得モジュール.

APIキー不要の RSS フィードから銘柄ごとのニュース記事を取得する。
取得失敗時は空リストを返してシステムを止めない。
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import aiohttp

logger = logging.getLogger(__name__)

# Yahoo Finance RSS (APIキー不要)
YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"

# 記事の鮮度フィルター（直近N分）
MAX_AGE_MINUTES = 30


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
    """ニュースフィード取得クライアント (Yahoo Finance RSS)."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "moomoo-trader/1.0"},
            )
        return self._session

    async def get_latest(self, symbol: str, limit: int = 20) -> list[NewsArticle]:
        """指定銘柄の最新ニュースを取得する.

        Yahoo Finance RSS から直近30分以内の記事を取得する。

        Args:
            symbol: 銘柄シンボル (例: "AAPL")
            limit: 取得件数上限

        Returns:
            ニュース記事リスト（取得失敗時は空リスト）
        """
        articles: list[NewsArticle] = []

        # Yahoo Finance RSS
        yahoo = await self._fetch_yahoo_rss(symbol, limit)
        articles.extend(yahoo)

        return articles[:limit]

    async def _fetch_yahoo_rss(self, symbol: str, limit: int) -> list[NewsArticle]:
        """Yahoo Finance RSS からニュースを取得する."""
        session = await self._ensure_session()
        articles: list[NewsArticle] = []
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=MAX_AGE_MINUTES)

        try:
            params = {"s": symbol, "region": "US", "lang": "en-US"}
            async with session.get(YAHOO_RSS_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning("Yahoo RSS failed: %s (status=%d)", symbol, resp.status)
                    return articles

                text = await resp.text()
                root = ET.fromstring(text)

                for item in root.findall(".//item"):
                    title = item.findtext("title", "")
                    description = item.findtext("description", "")
                    link = item.findtext("link", "")
                    pub_date_str = item.findtext("pubDate", "")

                    # 日付パース
                    published_at = self._parse_rss_date(pub_date_str)

                    # 鮮度フィルター
                    if published_at and published_at < cutoff:
                        continue

                    if not title:
                        continue

                    articles.append(NewsArticle(
                        title=title.strip(),
                        body=description.strip(),
                        symbol=symbol,
                        source="Yahoo Finance",
                        published_at=published_at or datetime.now(timezone.utc),
                        url=link,
                    ))

                    if len(articles) >= limit:
                        break

        except aiohttp.ClientError as e:
            logger.warning("Yahoo RSS network error: %s — %s", symbol, e)
        except ET.ParseError as e:
            logger.warning("Yahoo RSS XML parse error: %s — %s", symbol, e)
        except Exception:
            logger.exception("Yahoo RSS unexpected error: %s", symbol)

        logger.debug("Yahoo RSS: %s -> %d articles", symbol, len(articles))
        return articles

    @staticmethod
    def _parse_rss_date(date_str: str) -> datetime | None:
        """RSS の pubDate (RFC 2822) をパースする."""
        if not date_str:
            return None
        try:
            return parsedate_to_datetime(date_str)
        except (ValueError, TypeError):
            return None

    async def close(self) -> None:
        """セッションを閉じる."""
        if self._session and not self._session.closed:
            await self._session.close()
