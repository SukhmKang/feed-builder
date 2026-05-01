"""Tavily search source integration."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any

from tavily import TavilyClient

logger = logging.getLogger(__name__)

_TAVILY_TIMEOUT = 20
_TAVILY_MAX_RESULTS = 8
_TAVILY_MIN_SCORE = 0.8


class TavilyFetchError(RuntimeError):
    """Raised when a Tavily query cannot be executed successfully."""


class _TavilyKeyManager:
    """Round-robin key manager that permanently drops deactivated keys."""

    def __init__(self, keys: list[str]) -> None:
        self._lock = threading.Lock()
        self._keys = list(keys)
        self._index = 0

    def next_key(self) -> str | None:
        with self._lock:
            if not self._keys:
                return None
            key = self._keys[self._index % len(self._keys)]
            self._index = (self._index + 1) % len(self._keys)
            return key

    def mark_dead(self, key: str) -> None:
        with self._lock:
            if key not in self._keys:
                return
            idx = self._keys.index(key)
            self._keys.remove(key)
            if not self._keys:
                self._index = 0
            else:
                if self._index > idx:
                    self._index -= 1
                self._index %= len(self._keys)
            logger.warning(
                "Tavily key ...%s is deactivated; removed from rotation (%d key(s) remaining)",
                key[-4:],
                len(self._keys),
            )

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._keys)


_tavily_key_manager: _TavilyKeyManager | None = None
_tavily_manager_init_lock = threading.Lock()


def _get_tavily_key_manager() -> _TavilyKeyManager:
    global _tavily_key_manager
    if _tavily_key_manager is None:
        with _tavily_manager_init_lock:
            if _tavily_key_manager is None:
                raw_keys = os.getenv("TAVILY_API_KEYS", "").strip()
                keys = [v.strip() for v in raw_keys.split(",") if v.strip()]
                if not keys:
                    fallback = os.getenv("TAVILY_API_KEY", "").strip()
                    if fallback:
                        keys = [fallback]
                if not keys:
                    raise RuntimeError("TAVILY_API_KEY or TAVILY_API_KEYS must be configured")
                _tavily_key_manager = _TavilyKeyManager(keys)
    return _tavily_key_manager


def get_tavily_client() -> TavilyClient:
    """Return a TavilyClient using the next key in the round-robin rotation."""
    manager = _get_tavily_key_manager()
    key = manager.next_key()
    if key is None:
        raise RuntimeError("No active Tavily API keys available")
    return TavilyClient(api_key=key)


async def fetch_tavily_articles(query: str, *, days: int = 7, max_results: int = _TAVILY_MAX_RESULTS) -> list[dict[str, Any]]:
    response = await asyncio.to_thread(_search_tavily, query, days, max_results)
    return [_normalize_tavily_result(result, query=query) for result in response]


def _search_tavily(query: str, days: int, max_results: int) -> list[dict[str, Any]]:
    manager = _get_tavily_key_manager()
    start_date = (datetime.now(timezone.utc) - timedelta(days=max(int(days), 1))).strftime("%Y-%m-%d")

    tried: set[str] = set()
    while True:
        key = manager.next_key()
        if key is None:
            raise TavilyFetchError(
                f"Tavily search failed for {query!r}: all configured API keys are deactivated"
            )
        if key in tried:
            raise TavilyFetchError(
                f"Tavily search failed for {query!r}: all active API keys exhausted without success"
            )
        tried.add(key)

        client = TavilyClient(api_key=key)

        # Default-arg captures `client` by value to avoid late-binding across loop iterations.
        def _search(_client: TavilyClient = client) -> dict[str, Any]:
            return _client.search(
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
            raise TavilyFetchError(
                f"Tavily search timed out after {_TAVILY_TIMEOUT}s for {query!r}"
            ) from None
        except Exception as exc:
            if "deactivated" in str(exc).lower():
                manager.mark_dead(key)
                continue
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


def _parse_published_date(value: str) -> str:
    """Normalize a Tavily published_date (RFC 2822 or ISO) to an ISO 8601 string."""
    if not value:
        return datetime.now(timezone.utc).isoformat()
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass
    return datetime.now(timezone.utc).isoformat()


def _normalize_tavily_result(result: dict[str, Any], *, query: str) -> dict[str, Any]:
    url = str(result.get("url", "")).strip()
    title = unescape(str(result.get("title", "")).strip())
    snippet = unescape(str(result.get("content", "")).strip())
    raw_content = unescape(str(result.get("raw_content", "")).strip())
    source_name = unescape(str(result.get("source", "")).strip()) or "Tavily"
    published_at = _parse_published_date(str(result.get("published_date", "") or "").strip())

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
