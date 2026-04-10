"""Yahoo Finance RSS + Google News RSS を使ったニュースフィード取得モジュール.

APIキー不要の RSS フィードから銘柄ごとのニュース記事を取得する。
取得失敗時は空リストを返してシステムを止めない。
Google News RSS は銘柄ごとに最低60秒間隔でフェッチする（レート制限対策）。
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import aiohttp

logger = logging.getLogger(__name__)

# Yahoo Finance RSS (APIキー不要)
YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"

# Google News RSS (APIキー不要)
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"

# 記事の鮮度フィルター（直近N分）
MAX_AGE_MINUTES = 15

# Google News のフェッチ間隔（秒）— レート制限対策
GOOGLE_FETCH_INTERVAL = 60.0


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
    """ニュースフィード取得クライアント (Yahoo Finance RSS + Google News RSS)."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        # Google News のフェッチ時刻キャッシュ: symbol -> (monotonic_time, articles)
        self._google_cache: dict[str, tuple[float, list[NewsArticle]]] = {}

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "moomoo-trader/1.0"},
            )
        return self._session

    async def get_latest(self, symbol: str, limit: int = 20) -> list[NewsArticle]:
        """指定銘柄の最新ニュースを取得する.

        Yahoo Finance RSS + Google News RSS から直近30分以内の記事を取得。
        URL で重複除去する。

        Args:
            symbol: 銘柄シンボル (例: "AAPL")
            limit: 取得件数上限

        Returns:
            ニュース記事リスト（取得失敗時は空リスト）
        """
        # 並行取得
        yahoo_task = asyncio.ensure_future(self._fetch_yahoo_rss(symbol, limit))
        google_task = asyncio.ensure_future(self._fetch_google_news_rss(symbol, limit))
        yahoo, google = await asyncio.gather(yahoo_task, google_task)

        # URL で重複除去（URL が空の場合はタイトルでフォールバック）
        seen: set[str] = set()
        articles: list[NewsArticle] = []
        for article in yahoo + google:
            key = article.url.strip() if article.url.strip() else article.title.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            articles.append(article)

        if articles:
            logger.info("[%s] News: Yahoo=%d Google=%d total=%d", symbol, len(yahoo), len(google), len(articles))

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
                    logger.debug("Yahoo RSS failed: %s (status=%d)", symbol, resp.status)
                    return articles

                text = await resp.text()
                root = ET.fromstring(text)

                for item in root.findall(".//item"):
                    title = item.findtext("title", "")
                    description = item.findtext("description", "")
                    link = item.findtext("link", "")
                    pub_date_str = item.findtext("pubDate", "")

                    published_at = self._parse_rss_date(pub_date_str)
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
            logger.debug("Yahoo RSS network error: %s — %s", symbol, e)
        except ET.ParseError as e:
            logger.debug("Yahoo RSS XML parse error: %s — %s", symbol, e)
        except Exception:
            logger.exception("Yahoo RSS unexpected error: %s", symbol)

        return articles

    async def _fetch_google_news_rss(self, symbol: str, limit: int) -> list[NewsArticle]:
        """Google News RSS からニュースを取得する（60秒キャッシュ付き）."""
        # レート制限: 前回フェッチから60秒以内はキャッシュを返す
        now = _time.monotonic()
        cached = self._google_cache.get(symbol)
        if cached:
            last_fetch, cached_articles = cached
            if now - last_fetch < GOOGLE_FETCH_INTERVAL:
                return cached_articles

        session = await self._ensure_session()
        articles: list[NewsArticle] = []
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=MAX_AGE_MINUTES)

        try:
            params = {
                "q": f"{symbol} stock",
                "hl": "en-US",
                "gl": "US",
                "ceid": "US:en",
            }
            async with session.get(GOOGLE_NEWS_RSS_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.debug("Google News RSS failed: %s (status=%d)", symbol, resp.status)
                    self._google_cache[symbol] = (now, [])
                    return articles

                text = await resp.text()
                root = ET.fromstring(text)

                for item in root.findall(".//item"):
                    title = item.findtext("title", "")
                    description = item.findtext("description", "")
                    link = item.findtext("link", "")
                    pub_date_str = item.findtext("pubDate", "")

                    published_at = self._parse_rss_date(pub_date_str)
                    if published_at and published_at < cutoff:
                        continue
                    if not title:
                        continue

                    articles.append(NewsArticle(
                        title=title.strip(),
                        body=description.strip(),
                        symbol=symbol,
                        source="Google News",
                        published_at=published_at or datetime.now(timezone.utc),
                        url=link,
                    ))
                    if len(articles) >= limit:
                        break

        except aiohttp.ClientError as e:
            logger.debug("Google News RSS network error: %s — %s", symbol, e)
        except ET.ParseError as e:
            logger.debug("Google News RSS XML parse error: %s — %s", symbol, e)
        except Exception:
            logger.exception("Google News RSS unexpected error: %s", symbol)

        self._google_cache[symbol] = (now, articles)
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
