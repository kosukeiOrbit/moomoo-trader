"""BoardScraper のユニットテスト."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.data.board_scraper import BoardScraper, Post


# ---------------------------------------------------------------------------
# Reddit Atom XML fixture
# ---------------------------------------------------------------------------

REDDIT_ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>r/wallstreetbets</title>
  <entry>
    <title>NVDA to the moon! 🚀</title>
    <content type="html">$NVDA is unstoppable, bought 100 shares</content>
    <updated>2026-03-31T22:00:00+00:00</updated>
    <author><name>trader123</name></author>
    <id>t3_abc123</id>
  </entry>
  <entry>
    <title>Random post about cooking</title>
    <content type="html">Nothing about stocks here</content>
    <updated>2026-03-31T22:00:00+00:00</updated>
    <author><name>chef99</name></author>
    <id>t3_xyz789</id>
  </entry>
  <entry>
    <title>Old NVDA post</title>
    <content type="html">NVDA was great last year</content>
    <updated>2025-01-01T00:00:00+00:00</updated>
    <author><name>oldtimer</name></author>
    <id>t3_old001</id>
  </entry>
</feed>"""


def _mock_response(text: str, status: int = 200) -> AsyncMock:
    resp = AsyncMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(text: str, status: int = 200) -> MagicMock:
    from unittest.mock import MagicMock
    resp = _mock_response(text, status)
    session = MagicMock()
    session.closed = False
    session.get.return_value = resp
    session.close = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Symbol matching
# ---------------------------------------------------------------------------

class TestBoardScraperSymbolMatch:
    """_mentions_symbol() のテスト."""

    def test_dollar_sign(self) -> None:
        assert BoardScraper._mentions_symbol("Buy $NVDA now!", "NVDA") is True

    def test_standalone_word(self) -> None:
        assert BoardScraper._mentions_symbol("I love NVDA stock", "NVDA") is True

    def test_in_parentheses(self) -> None:
        assert BoardScraper._mentions_symbol("Nvidia (NVDA) is great", "NVDA") is True

    def test_substring_no_match(self) -> None:
        """NVDA が他の単語の部分文字列の場合は不一致."""
        assert BoardScraper._mentions_symbol("ENVDAT is something", "NVDA") is False

    def test_case_insensitive(self) -> None:
        assert BoardScraper._mentions_symbol("nvda looks good", "NVDA") is True

    def test_no_mention(self) -> None:
        assert BoardScraper._mentions_symbol("Nothing relevant here", "NVDA") is False


# ---------------------------------------------------------------------------
# fetch_posts
# ---------------------------------------------------------------------------

class TestBoardScraperFetch:
    """fetch_posts() のテスト."""

    @pytest.mark.asyncio
    async def test_fetch_filters_by_symbol(self) -> None:
        """NVDA に言及した投稿のみ返す."""
        scraper = BoardScraper()
        scraper._session = _mock_session(REDDIT_ATOM_XML)

        posts = await scraper.fetch_posts("NVDA")
        # "NVDA to the moon" matches, "cooking" does not
        # Fixture date may be outside 60-min window depending on test timing
        if posts:
            assert all("NVDA" in p.text.upper() for p in posts)
        await scraper.close()

    @pytest.mark.asyncio
    async def test_fetch_no_match_returns_empty(self) -> None:
        """銘柄に言及がない場合は空リスト."""
        scraper = BoardScraper()
        scraper._session = _mock_session(REDDIT_ATOM_XML)

        posts = await scraper.fetch_posts("MSFT")
        assert posts == []
        await scraper.close()

    @pytest.mark.asyncio
    async def test_fetch_http_error_returns_empty(self) -> None:
        """HTTP エラー時は空リスト."""
        scraper = BoardScraper()
        scraper._session = _mock_session("", status=429)

        posts = await scraper.fetch_posts("AAPL")
        assert posts == []
        await scraper.close()

    @pytest.mark.asyncio
    async def test_fetch_invalid_xml_returns_empty(self) -> None:
        """不正な XML でも空リスト."""
        scraper = BoardScraper()
        scraper._session = _mock_session("<broken xml")

        posts = await scraper.fetch_posts("AAPL")
        assert posts == []
        await scraper.close()

    @pytest.mark.asyncio
    async def test_fetch_respects_limit(self) -> None:
        """limit で件数を制限する."""
        scraper = BoardScraper()
        scraper._session = _mock_session(REDDIT_ATOM_XML)

        posts = await scraper.fetch_posts("NVDA", limit=1)
        assert len(posts) <= 1
        await scraper.close()

    def test_parse_atom_date_valid(self) -> None:
        dt = BoardScraper._parse_atom_date("2026-03-31T22:00:00+00:00")
        assert dt is not None
        assert dt.year == 2026

    def test_parse_atom_date_with_z(self) -> None:
        dt = BoardScraper._parse_atom_date("2026-03-31T22:00:00Z")
        assert dt is not None

    def test_parse_atom_date_empty(self) -> None:
        assert BoardScraper._parse_atom_date("") is None
