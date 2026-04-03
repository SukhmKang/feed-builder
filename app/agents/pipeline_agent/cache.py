"""Per-run in-process article cache.

Module-level dict is safe here because this code only runs inside the
pipeline-agent subprocess — one build per process lifetime.
"""

import asyncio
from typing import Any

from app.sources.runner import fetch_articles

# (source_type, source_feed) -> articles
_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
# Tracks in-flight fetches so parallel callers don't duplicate the same request
_inflight: dict[tuple[str, str], asyncio.Event] = {}


async def fetch_articles_cached(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """fetch_articles() with per-(type, feed) memoization for the lifetime of this process."""
    results: list[dict[str, Any]] = []
    to_fetch: list[dict[str, Any]] = []

    for source in sources:
        key = (str(source.get("type", "")), str(source.get("feed", "")))
        if key in _cache:
            results.extend(_cache[key])
        elif key in _inflight:
            # Another coroutine is already fetching this source — wait for it
            await _inflight[key].wait()
            results.extend(_cache.get(key, []))
        else:
            to_fetch.append(source)

    if not to_fetch:
        return results

    # Mark all as in-flight before kicking off fetches
    events: dict[tuple[str, str], asyncio.Event] = {}
    for source in to_fetch:
        key = (str(source.get("type", "")), str(source.get("feed", "")))
        event = asyncio.Event()
        _inflight[key] = event
        events[key] = event

    # Fetch each source individually so we can cache per (type, feed)
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
            _cache[key] = []
        else:
            _cache[key] = batch
            results.extend(batch)
        events[key].set()
        _inflight.pop(key, None)

    if first_error is not None:
        raise first_error

    return results
