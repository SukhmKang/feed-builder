"""Tavily search source integration."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone
from html import unescape
from itertools import cycle
from typing import Any

from tavily import TavilyClient

logger = logging.getLogger(__name__)

_TAVILY_TIMEOUT = 20
_TAVILY_MAX_RESULTS = 8
_TAVILY_MIN_SCORE = 0.8
_tavily_key_cycle: Any | None = None


class TavilyFetchError(RuntimeError):
    """Raised when a Tavily query cannot be executed successfully."""


def _get_tavily_key_cycle():
    global _tavily_key_cycle
    if _tavily_key_cycle is None:
        raw_keys = os.getenv("TAVILY_API_KEYS", "").strip()
        keys = [value.strip() for value in raw_keys.split(",") if value.strip()]
        if not keys:
            fallback = os.getenv("TAVILY_API_KEY", "").strip()
            if fallback:
                keys = [fallback]
        if not keys:
            raise RuntimeError("TAVILY_API_KEY or TAVILY_API_KEYS must be configured")
        _tavily_key_cycle = cycle(keys)
    return _tavily_key_cycle


def get_tavily_client() -> TavilyClient:
    """Return a TavilyClient using the next key in the round-robin rotation."""
    return TavilyClient(api_key=next(_get_tavily_key_cycle()))


async def fetch_tavily_articles(query: str, *, days: int = 7, max_results: int = _TAVILY_MAX_RESULTS) -> list[dict[str, Any]]:
    response = await asyncio.to_thread(_search_tavily, query, days, max_results)
    return [_normalize_tavily_result(result, query=query) for result in response]


def _search_tavily(query: str, days: int, max_results: int) -> list[dict[str, Any]]:
    client = get_tavily_client()
    start_date = (datetime.now(timezone.utc) - timedelta(days=max(int(days), 1))).strftime("%Y-%m-%d")

    def _search() -> dict[str, Any]:
        return client.search(
            query=query,
            topic="news",
            start_date=start_date,
            max_results=max(int(max_results), 1),
            include_raw_content="text",
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_search)
            response = future.result(timeout=_TAVILY_TIMEOUT)
    except FuturesTimeoutError:
        raise TavilyFetchError(f"Tavily search timed out after {_TAVILY_TIMEOUT}s for {query!r}") from None
    except Exception as exc:
        raise TavilyFetchError(f"Tavily search failed for {query!r}: {exc}") from exc

    results = response.get("results", [])
    if not isinstance(results, list):
        raise TavilyFetchError(f"Tavily returned an unexpected payload for {query!r}")
    filtered: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        url = str(result.get("url", "")).strip()
        score = float(result.get("score") or 0.0)
        if not url or score <= _TAVILY_MIN_SCORE:
            continue
        filtered.append(result)
    return filtered


def _normalize_tavily_result(result: dict[str, Any], *, query: str) -> dict[str, Any]:
    url = str(result.get("url", "")).strip()
    title = unescape(str(result.get("title", "")).strip())
    snippet = unescape(str(result.get("content", "")).strip())
    raw_content = unescape(str(result.get("raw_content", "")).strip())
    source_name = unescape(str(result.get("source", "")).strip()) or "Tavily"
    published_at = str(result.get("published_date", "")).strip() or datetime.now(timezone.utc).isoformat()

    return {
        "id": hashlib.md5(url.encode("utf-8")).hexdigest(),
        "title": title or url,
        "url": url,
        "published_at": published_at,
        "content": snippet,
        "full_text": raw_content or snippet,
        "source_url": f"https://app.tavily.com/search?q={query}",
        "source_name": source_name,
        "source_type": "tavily",
        "raw": dict(result),
    }
