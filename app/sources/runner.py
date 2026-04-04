"""
Source orchestrator for normalized article fetching.

Input:
- `sources: list[SourceSpec]`
- Each source spec must include:
  {
      "type": str,
      "feed": str,
  }

Supported source types:
- `rss`
- `tavily`
- `google_news_search`
- `nitter_user`
- `nitter_search`
- `reddit_subreddit`
- `reddit_search`
- `reddit_subreddits_by_topic`
- `youtube_search`
- `youtube_channel`
- `youtube_channel_url`
- `youtube_channels_by_topic`
- `youtube_videos_by_topic`

Usage by source type:
- `rss`
  - Use when you already have an RSS or Atom feed URL.
  - Example:
    `{"type": "rss", "feed": "https://www.rockpapershotgun.com/feed"}`
- `tavily`
  - Use when you want Tavily news search results for a query.
  - Useful for broad web/news discovery when you want fresh article URLs directly.
  - Example:
    `{"type": "tavily", "feed": "Ace Attorney announcement"}`
- `google_news_search`
  - Use when you want a synthetic Google News RSS feed for a query.
  - Useful as a fallback when a site looks valuable but no native feed is available.
  - Example:
    `{"type": "google_news_search", "feed": "site:bbc.com BBC"}`
- `nitter_user`
  - Use when you want posts from one Nitter account.
  - `feed` should be the username.
  - Example:
    `{"type": "nitter_user", "feed": "SteamDeckHQ"}`
- `nitter_search`
  - Use when you want Nitter search results.
  - `feed` should be the search query.
  - Example:
    `{"type": "nitter_search", "feed": "portable gaming pc"}`
- `reddit_subreddit`
  - Use when you want posts from a specific subreddit.
  - `feed` should be the subreddit name.
  - Example:
    `{"type": "reddit_subreddit", "feed": "AceAttorney"}`
- `reddit_search`
  - Use when you want Reddit search results across all subreddits.
  - `feed` should be the search query.
  - Example:
    `{"type": "reddit_search", "feed": "Ace Attorney announcement"}`
- `reddit_subreddits_by_topic`
  - Use when you want to discover relevant subreddits for a topic,
    then ingest from those subreddits.
  - Example:
    `{"type": "reddit_subreddits_by_topic", "feed": "Ace Attorney news"}`
- `youtube_search`
  - Use when you want the top relevant YouTube videos for a query right now.
  - Returns direct video results in YouTube API relevance order.
  - Example:
    `{"type": "youtube_search", "feed": "Ace Attorney announcement"}`
- `youtube_channel`
  - Use when you want videos from one specific YouTube channel.
  - `feed` can be a channel id, channel URL, or channel-like name.
  - Examples:
    `{"type": "youtube_channel", "feed": "UCW7h-1mymnJ96akzjrmiIgA"}`
    `{"type": "youtube_channel", "feed": "https://www.youtube.com/@NintendoAmerica"}`
    `{"type": "youtube_channel", "feed": "Capcom USA"}`
- `youtube_channel_url`
  - Use when you specifically have a YouTube channel URL.
  - Example:
    `{"type": "youtube_channel_url", "feed": "https://www.youtube.com/@NintendoAmerica"}`
- `youtube_channels_by_topic`
  - Use when you want to discover channels about a topic, then ingest from those channels.
  - Example:
    `{"type": "youtube_channels_by_topic", "feed": "Ace Attorney news"}`
- `youtube_videos_by_topic`
  - Use when you want to discover channels via videos matching a topic, then ingest from those channels.
  - Example:
    `{"type": "youtube_videos_by_topic", "feed": "Ace Attorney announcement"}`

Behavior:
- `rss` fetches articles directly from the given feed URL.
- `tavily` runs a Tavily news search for the query and normalizes the results.
- `google_news_search` builds a Google News RSS search URL from the query and
  fetches articles from that synthetic feed.
- `nitter_user` fetches a Nitter user feed by username.
- `nitter_search` fetches Nitter search results by query string.
- `reddit_subreddit` fetches a specific subreddit RSS feed.
- `reddit_search` fetches Reddit global search RSS results.
- `reddit_subreddits_by_topic` discovers subreddits by topic, then fetches
  those subreddit feeds.
- `youtube_search` returns top YouTube video search results directly in
  YouTube API relevance order as normalized articles.
- `youtube_search` fills `full_text` from the video description.
- `youtube_channel` resolves a channel id, channel URL, or channel-like name
  into a verified YouTube channel feed and fetches its articles.
- `youtube_channel_url` resolves a YouTube channel URL into a verified feed.
- `youtube_channels_by_topic` searches channels for a topic, verifies their feeds,
  and fetches articles from all matching channel feeds.
- `youtube_videos_by_topic` searches videos for a topic, dedupes parent channels,
  verifies their feeds, and fetches articles from those channel feeds.
- All returned articles are normalized into one shared schema.
- Articles are deduped by `id` across all sources in one call.
"""

import asyncio
import hashlib
from html import unescape
from typing import Any, TypedDict
from urllib.parse import parse_qs, quote, urlparse

from app.sources.google_news import google_news_search_feed_url, is_google_news_search_feed_url
from app.sources.nitter import fetch_search_feed, fetch_user_feed
from app.sources.reddit import fetch_search_articles, fetch_subreddit_articles, search_subreddits_by_topic
from app.sources.rss import FeedBatchError, fetch_rss_articles
from app.sources.tavily import fetch_tavily_articles
from app.history.stream_history import _write_articles_sync
from app.sources.youtube_scraper import (
    get_channel_feed,
    search_channels_by_topic,
    search_videos_by_topic,
    search_videos_direct_by_topic,
)


class SourceSpec(TypedDict):
    type: str
    feed: str


class SourceBatchError(RuntimeError):
    """Raised when one or more source specs fail during orchestration."""

    def __init__(self, errors: list[dict[str, Any]]):
        self.errors = errors
        message = "; ".join(
            f"{error['source']['type']}:{error['source']['feed']} -> {error['error']}"
            for error in errors
        )
        super().__init__(message)


async def fetch_articles(sources: list[SourceSpec]) -> list[dict[str, Any]]:
    """Fetch normalized articles from heterogeneous source specs."""
    if not sources:
        return []

    tasks = [_fetch_source_articles(source) for source in sources]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    errors: list[dict[str, Any]] = []
    for source, result in zip(sources, results, strict=False):
        if isinstance(result, Exception):
            errors.append(
                {
                    "source": {
                        "type": str(source.get("type", "")).strip(),
                        "feed": str(source.get("feed", "")).strip(),
                    },
                    "error_type": type(result).__name__,
                    "error": str(result),
                }
            )
    if errors:
        raise SourceBatchError(errors)

    observations: list[dict[str, Any]] = []
    for source, source_articles in zip(sources, results, strict=False):
        for article in source_articles:
            url = str(article.get("url", "")).strip()
            if not url:
                continue
            observations.append(
                {
                    "url": url,
                    "source_type": str(source.get("type", "")).strip().lower(),
                    "source_feed": str(source.get("feed", "")).strip(),
                    "source_spec": {
                        "type": str(source.get("type", "")).strip().lower(),
                        "feed": str(source.get("feed", "")).strip(),
                    },
                }
            )

    seen_ids: set[str] = set()
    articles: list[dict[str, Any]] = []
    for source, source_articles in zip(sources, results, strict=False):
        if isinstance(source_articles, Exception):
            continue
        for article in source_articles:
            article_id = str(article.get("id", "")).strip()
            if not article_id or article_id in seen_ids:
                continue
            seen_ids.add(article_id)
            # Stamp the logical source spec key onto the article so the DB can filter
            # by spec (type, feed) without needing to reverse-resolve URLs.
            article["spec_source_type"] = str(source.get("type", "")).strip()
            article["spec_source_feed"] = str(source.get("feed", "")).strip()
            articles.append(article)

    await asyncio.to_thread(_write_articles_sync, articles, observations)

    return articles


async def _fetch_source_articles(source: SourceSpec) -> list[dict[str, Any]]:
    source_type = str(source.get("type", "")).strip().lower()
    feed_value = str(source.get("feed", "")).strip()

    if not source_type:
        raise ValueError("Source spec is missing required field: type")
    if not feed_value:
        raise ValueError(f"Source spec for type '{source_type}' is missing required field: feed")

    if source_type == "rss":
        return await _fetch_rss_source(feed_value)

    if source_type == "tavily":
        return await fetch_tavily_articles(feed_value)

    if source_type == "google_news_search":
        return await _fetch_google_news_search_source(feed_value)

    if source_type == "nitter_user":
        feed = await fetch_user_feed(_normalize_nitter_username(feed_value))
        return feed.to_articles()

    if source_type == "nitter_search":
        feed = await fetch_search_feed(feed_value)
        return feed.to_articles()

    if source_type == "reddit_subreddit":
        return await fetch_subreddit_articles([feed_value])

    if source_type == "reddit_search":
        return await fetch_search_articles(feed_value)

    if source_type == "reddit_subreddits_by_topic":
        return await _fetch_reddit_topic_articles(feed_value)

    if source_type == "youtube_search":
        return await _fetch_youtube_search_articles(feed_value)

    if source_type == "youtube_channel":
        return await _fetch_youtube_channel_articles(feed_value)

    if source_type == "youtube_channel_url":
        if not _looks_like_youtube_url(feed_value):
            raise ValueError(f"YouTube channel URL source must be a YouTube URL: {feed_value}")
        return await _fetch_youtube_channel_articles(feed_value)

    if source_type == "youtube_channels_by_topic":
        return await _fetch_youtube_topic_articles(feed_value, search_mode="channels")

    if source_type == "youtube_videos_by_topic":
        return await _fetch_youtube_topic_articles(feed_value, search_mode="videos")

    raise ValueError(f"Unknown source type: {source_type}")


async def _fetch_rss_source(feed_url: str) -> list[dict[str, Any]]:
    try:
        return await fetch_rss_articles([feed_url])
    except FeedBatchError as exc:
        raise ValueError(f"RSS source failed for {feed_url}: {exc}") from exc


async def _fetch_google_news_search_source(query_or_feed_url: str) -> list[dict[str, Any]]:
    feed_url = (
        query_or_feed_url
        if is_google_news_search_feed_url(query_or_feed_url)
        else google_news_search_feed_url(query_or_feed_url)
    )
    articles = await _fetch_rss_source(feed_url)
    return _relabel_source_type(articles, source_type="google_news")


async def _fetch_youtube_search_articles(feed_value: str) -> list[dict[str, Any]]:
    videos = await search_videos_direct_by_topic(feed_value)
    articles = [
        _youtube_video_to_article(
            video,
            source_query=feed_value,
            full_text="",
        )
        for video in videos
    ]
    return await _enrich_articles_with_youtube_transcripts(articles)


async def _fetch_reddit_topic_articles(feed_value: str) -> list[dict[str, Any]]:
    subreddits = await search_subreddits_by_topic(feed_value)
    subreddit_names = [subreddit["subreddit_name"] for subreddit in subreddits]
    if not subreddit_names:
        return []
    return await fetch_subreddit_articles(subreddit_names)


async def _fetch_youtube_channel_articles(feed_value: str) -> list[dict[str, Any]]:
    channel = await get_channel_feed(feed_value)
    if channel is None:
        raise ValueError(f"Could not resolve a YouTube channel feed for: {feed_value}")
    articles = await _fetch_rss_source(channel["feed_url"])
    relabeled_articles = _relabel_source_type(articles, source_type="youtube")
    return await _enrich_articles_with_youtube_transcripts(relabeled_articles)


async def _fetch_youtube_topic_articles(feed_value: str, *, search_mode: str) -> list[dict[str, Any]]:
    if search_mode == "channels":
        channels = await search_channels_by_topic(feed_value)
        feed_urls = [channel["feed_url"] for channel in channels]
    elif search_mode == "videos":
        videos = await search_videos_by_topic(feed_value)
        feed_urls = [video["feed_url"] for video in videos]
    else:
        raise ValueError(f"Unsupported YouTube search mode: {search_mode}")

    unique_feed_urls = _dedupe_strings(feed_urls)
    if not unique_feed_urls:
        return []
    articles = await _fetch_rss_source_batch(unique_feed_urls, context_label=feed_value)
    relabeled_articles = _relabel_source_type(articles, source_type="youtube")
    return await _enrich_articles_with_youtube_transcripts(relabeled_articles)


async def _fetch_rss_source_batch(feed_urls: list[str], *, context_label: str) -> list[dict[str, Any]]:
    try:
        return await fetch_rss_articles(feed_urls)
    except FeedBatchError as exc:
        raise ValueError(f"RSS feed batch failed for {context_label}: {exc}") from exc


def _normalize_nitter_username(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("@"):
        return normalized[1:]
    return normalized


def _looks_like_youtube_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return False
    hostname = parsed.netloc.lower()
    return hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        results.append(normalized)
    return results


def _relabel_source_type(articles: list[dict[str, Any]], *, source_type: str) -> list[dict[str, Any]]:
    relabeled: list[dict[str, Any]] = []
    for article in articles:
        updated_article = dict(article)
        updated_article["source_type"] = source_type
        relabeled.append(updated_article)
    return relabeled


async def _enrich_articles_with_youtube_transcripts(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched_articles: list[dict[str, Any]] = []
    for article in articles:
        enriched_article = dict(article)
        content_text = str(enriched_article.get("content", "")).strip()
        enriched_article["full_text"] = content_text
        enriched_articles.append(enriched_article)
    return enriched_articles


def _youtube_video_to_article(
    video: dict[str, Any],
    *,
    source_query: str,
    full_text: str,
) -> dict[str, Any]:
    video_id = str(video.get("video_id", "")).strip()
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    description = unescape(str(video.get("description", "")).strip())
    published_at = str(video.get("published_at", "")).strip()
    if not published_at:
        raise ValueError(f"YouTube video {video_url} is missing a usable published timestamp")
    return {
        "id": hashlib.md5(video_url.encode("utf-8")).hexdigest(),
        "title": unescape(str(video.get("video_title", "")).strip()),
        "url": video_url,
        "published_at": published_at,
        "content": description,
        "full_text": full_text.strip(),
        "source_url": f"https://www.youtube.com/results?search_query={quote(source_query)}",
        "source_name": unescape(str(video.get("channel_name", "")).strip()),
        "source_type": "youtube",
        "raw": dict(video),
    }


__all__ = ["SourceBatchError", "SourceSpec", "fetch_articles"]
