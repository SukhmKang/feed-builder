"""Per-run article cache backed by a SQLite temp file.

Lifecycle:
- `run_cache_context()` creates an `ArticleCache` (temp SQLite file), sets it as the active
  cache for the current async context, and deletes the file when the context exits.
- `fetch_articles_cached()` reads the active cache from the context var. If called outside
  a run context (tests, one-off tool calls), it creates a throw-away cache scoped to that call.
"""

import asyncio
import json
import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from app.sources.runner import fetch_articles


class ArticleCache:
    """SQLite-backed cache for a single pipeline agent run."""

    def __init__(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".cache.db")
        os.close(fd)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE fetch_cache "
            "(source_type TEXT, source_feed TEXT, articles_json TEXT, "
            "PRIMARY KEY (source_type, source_feed))"
        )
        self._conn.commit()

    def _get(self, key: tuple[str, str]) -> list[dict[str, Any]] | None:
        row = self._conn.execute(
            "SELECT articles_json FROM fetch_cache WHERE source_type=? AND source_feed=?",
            key,
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _set(self, key: tuple[str, str], articles: list[dict[str, Any]]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO fetch_cache VALUES (?, ?, ?)",
            (*key, json.dumps(articles)),
        )
        self._conn.commit()

    async def fetch(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return cached articles for sources, fetching any that are not yet cached."""
        results: list[dict[str, Any]] = []
        to_fetch: list[dict[str, Any]] = []

        for source in sources:
            key = (str(source.get("type", "")), str(source.get("feed", "")))
            cached = await asyncio.to_thread(self._get, key)
            if cached is not None:
                results.extend(cached)
            else:
                to_fetch.append(source)

        if not to_fetch:
            return results

        fetched = await asyncio.gather(
            *[fetch_articles([s]) for s in to_fetch],
            return_exceptions=True,
        )

        first_error: Exception | None = None
        for source, batch in zip(to_fetch, fetched):
            key = (str(source.get("type", "")), str(source.get("feed", "")))
            if isinstance(batch, Exception):
                if first_error is None:
                    first_error = batch
                await asyncio.to_thread(self._set, key, [])
            else:
                await asyncio.to_thread(self._set, key, batch)
                results.extend(batch)

        if first_error is not None:
            raise first_error

        return results

    def close(self) -> None:
        self._conn.close()
        try:
            os.unlink(self._db_path)
        except OSError:
            pass


_current_cache: ContextVar[ArticleCache | None] = ContextVar("article_cache", default=None)


@asynccontextmanager
async def run_cache_context():
    """Async context manager that owns an ArticleCache for the duration of a run.

    Reentrant: if a cache is already active in the current context, this is a no-op.
    The cache file is deleted when the outermost context exits.
    """
    if _current_cache.get() is not None:
        yield
        return

    cache = ArticleCache()
    token = _current_cache.set(cache)
    try:
        yield
    finally:
        _current_cache.reset(token)
        await asyncio.to_thread(cache.close)


async def fetch_articles_cached(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """fetch_articles() with per-(type, feed) memoization for the current run."""
    cache = _current_cache.get()
    if cache is None:
        async with run_cache_context():
            return await fetch_articles_cached(sources)
    return await cache.fetch(sources)
