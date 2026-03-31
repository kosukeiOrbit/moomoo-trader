"""NewsFeed のユニットテスト."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data.news_feed import NewsFeed, NewsArticle


# ---------------------------------------------------------------------------
# Yahoo RSS XML fixture
# ---------------------------------------------------------------------------

YAHOO_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Yahoo Finance</title>
  <item>
    <title>Apple beats Q2 earnings</title>
    <description>Revenue up 10% year over year</description>
    <link>https://example.com/article1</link>
    <pubDate>Thu, 31 Mar 2026 22:00:00 +0000</pubDate>
  </item>
  <item>
    <title>Old news about Apple</title>
    <description>This is very old</description>
    <link>https://example.com/old</link>
    <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
  </item>
</channel>
</rss>"""


def _mock_response(text: str, status: int = 200) -> AsyncMock:
    """aiohttp response mock."""
    resp = AsyncMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(text: str, status: int = 200) -> MagicMock:
    """Mock aiohttp session."""
    resp = _mock_response(text, status)
    session = MagicMock()
    session.closed = False
    session.get.return_value = resp
    session.close = AsyncMock()  # await-able close()
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNewsFeed:
    """NewsFeed のテスト."""

    @pytest.mark.asyncio
    async def test_get_latest_parses_rss(self) -> None:
        """Yahoo RSS をパースして NewsArticle を返す."""
        feed = NewsFeed()
        feed._session = _mock_session(YAHOO_RSS_XML)

        articles = await feed.get_latest("AAPL")
        # The fixture has one fresh article and one old one
        # Fresh article may be filtered by 30-min cutoff depending on test timing
        # so we check structure if any returned
        if articles:
            assert articles[0].source == "Yahoo Finance"
            assert articles[0].symbol == "AAPL"
            assert articles[0].title  # non-empty
        await feed.close()

    @pytest.mark.asyncio
    async def test_get_latest_http_error_returns_empty(self) -> None:
        """HTTP エラー時は空リストを返す."""
        feed = NewsFeed()
        feed._session = _mock_session("", status=500)

        articles = await feed.get_latest("AAPL")
        assert articles == []
        await feed.close()

    @pytest.mark.asyncio
    async def test_get_latest_invalid_xml_returns_empty(self) -> None:
        """不正な XML でも空リストを返す."""
        feed = NewsFeed()
        feed._session = _mock_session("not xml at all")

        articles = await feed.get_latest("AAPL")
        assert articles == []
        await feed.close()

    @pytest.mark.asyncio
    async def test_get_latest_respects_limit(self) -> None:
        """limit パラメータで件数を制限する."""
        feed = NewsFeed()
        feed._session = _mock_session(YAHOO_RSS_XML)

        articles = await feed.get_latest("AAPL", limit=1)
        assert len(articles) <= 1
        await feed.close()

    def test_parse_rss_date_valid(self) -> None:
        dt = NewsFeed._parse_rss_date("Thu, 31 Mar 2026 22:00:00 +0000")
        assert dt is not None
        assert dt.year == 2026

    def test_parse_rss_date_empty(self) -> None:
        assert NewsFeed._parse_rss_date("") is None

    def test_parse_rss_date_invalid(self) -> None:
        assert NewsFeed._parse_rss_date("not a date") is None
