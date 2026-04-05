"""Tests for the SQLite-backed per-run article cache."""

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.cache import ArticleCache, _current_cache, fetch_articles_cached, run_cache_context


def run(coro):
    return asyncio.run(coro)


# ─── ArticleCache unit tests ─────────────────────────────────────────────────


def test_article_cache_creates_and_deletes_db_file():
    cache = ArticleCache()
    assert os.path.exists(cache._db_path)
    cache.close()
    assert not os.path.exists(cache._db_path)


def test_article_cache_get_returns_none_for_missing_key():
    cache = ArticleCache()
    try:
        assert cache._get(("rss", "https://example.com/feed")) is None
    finally:
        cache.close()


def test_article_cache_set_and_get_roundtrip():
    cache = ArticleCache()
    try:
        key = ("rss", "https://example.com/feed")
        articles = [{"id": "1", "title": "Hello"}, {"id": "2", "title": "World"}]
        cache._set(key, articles)
        assert cache._get(key) == articles
    finally:
        cache.close()


def test_article_cache_set_overwrites_existing():
    cache = ArticleCache()
    try:
        key = ("reddit", "python")
        cache._set(key, [{"id": "old"}])
        cache._set(key, [{"id": "new"}])
        assert cache._get(key) == [{"id": "new"}]
    finally:
        cache.close()


def test_article_cache_stores_empty_list():
    cache = ArticleCache()
    try:
        key = ("rss", "https://empty.com/feed")
        cache._set(key, [])
        assert cache._get(key) == []
    finally:
        cache.close()


# ─── ArticleCache.fetch ───────────────────────────────────────────────────────


def test_fetch_calls_fetch_articles_for_uncached_sources():
    fake_articles = [{"id": "a1", "title": "Article 1"}]
    with patch("app.agents.cache.fetch_articles", new_callable=AsyncMock, return_value=fake_articles):
        cache = ArticleCache()
        try:
            sources = [{"type": "rss", "feed": "https://example.com/feed"}]
            result = run(cache.fetch(sources))
            assert result == fake_articles
        finally:
            cache.close()


def test_fetch_returns_cached_result_without_refetching():
    fake_articles = [{"id": "a1", "title": "Cached Article"}]
    with patch("app.agents.cache.fetch_articles", new_callable=AsyncMock, return_value=fake_articles) as mock_fetch:
        cache = ArticleCache()
        try:
            sources = [{"type": "rss", "feed": "https://example.com/feed"}]
            run(cache.fetch(sources))
            mock_fetch.reset_mock()
            result = run(cache.fetch(sources))
            assert result == fake_articles
            mock_fetch.assert_not_called()
        finally:
            cache.close()


def test_fetch_raises_on_source_error():
    with patch("app.agents.cache.fetch_articles", new_callable=AsyncMock, side_effect=RuntimeError("network error")):
        cache = ArticleCache()
        try:
            with pytest.raises(RuntimeError, match="network error"):
                run(cache.fetch([{"type": "rss", "feed": "https://bad.com/feed"}]))
        finally:
            cache.close()


def test_fetch_caches_empty_list_on_error_then_does_not_refetch():
    """A failed fetch is stored as [] so the source is not re-attempted."""
    with patch("app.agents.cache.fetch_articles", new_callable=AsyncMock, side_effect=RuntimeError("fail")) as mock_fetch:
        cache = ArticleCache()
        try:
            with pytest.raises(RuntimeError):
                run(cache.fetch([{"type": "rss", "feed": "https://bad.com/feed"}]))
            mock_fetch.reset_mock()
            result = run(cache.fetch([{"type": "rss", "feed": "https://bad.com/feed"}]))
            assert result == []
            mock_fetch.assert_not_called()
        finally:
            cache.close()


def test_fetch_handles_mixed_cached_and_uncached_sources():
    cached_articles = [{"id": "cached"}]
    fresh_articles = [{"id": "fresh"}]

    with patch("app.agents.cache.fetch_articles", new_callable=AsyncMock, return_value=fresh_articles) as mock_fetch:
        cache = ArticleCache()
        try:
            cache._set(("rss", "https://cached.com"), cached_articles)
            result = run(cache.fetch([
                {"type": "rss", "feed": "https://cached.com"},
                {"type": "reddit", "feed": "python"},
            ]))
            assert cached_articles[0] in result
            assert fresh_articles[0] in result
            mock_fetch.assert_called_once()
        finally:
            cache.close()


# ─── run_cache_context ────────────────────────────────────────────────────────


def test_run_cache_context_sets_and_clears_context_var():
    async def _test():
        assert _current_cache.get() is None
        async with run_cache_context():
            assert _current_cache.get() is not None
        assert _current_cache.get() is None

    run(_test())


def test_run_cache_context_deletes_db_file_on_exit():
    async def _test():
        async with run_cache_context():
            db_path = _current_cache.get()._db_path
            assert os.path.exists(db_path)
        assert not os.path.exists(db_path)

    run(_test())


def test_run_cache_context_is_reentrant():
    async def _test():
        async with run_cache_context():
            outer_cache = _current_cache.get()
            async with run_cache_context():
                assert _current_cache.get() is outer_cache
            # Inner context must not close the cache
            assert _current_cache.get() is outer_cache
        assert _current_cache.get() is None

    run(_test())


def test_run_cache_context_cleans_up_on_exception():
    db_path = None

    async def _test():
        nonlocal db_path
        with pytest.raises(ValueError):
            async with run_cache_context():
                db_path = _current_cache.get()._db_path
                raise ValueError("simulated error")
        assert _current_cache.get() is None

    run(_test())
    assert db_path is not None and not os.path.exists(db_path)


# ─── fetch_articles_cached ────────────────────────────────────────────────────


def test_fetch_articles_cached_uses_active_context():
    fake_articles = [{"id": "x1"}]

    async def _test():
        with patch("app.agents.cache.fetch_articles", new_callable=AsyncMock, return_value=fake_articles):
            async with run_cache_context():
                result = await fetch_articles_cached([{"type": "rss", "feed": "https://example.com"}])
                assert result == fake_articles

    run(_test())


def test_fetch_articles_cached_creates_throwaway_cache_when_no_context():
    """Outside a run_cache_context, fetch_articles_cached should still work."""
    fake_articles = [{"id": "y1"}]

    async def _test():
        assert _current_cache.get() is None
        with patch("app.agents.cache.fetch_articles", new_callable=AsyncMock, return_value=fake_articles):
            result = await fetch_articles_cached([{"type": "rss", "feed": "https://example.com"}])
            assert result == fake_articles
        assert _current_cache.get() is None

    run(_test())
