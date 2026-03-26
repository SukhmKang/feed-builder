"""Discovery-oriented Claude Agent SDK tools."""

import os
import random
from typing import Any

from claude_agent_sdk import tool
from dotenv import load_dotenv
from tavily import AsyncTavilyClient

from discover_feeds import discover_feeds_detailed
from google_news import google_news_search_feed_url
from reddit import get_subreddit_from_post, search_subreddits_by_topic, subreddit_feed_url
from rsscatalog import get_category_feeds, search_categories as search_rsscatalog_categories
from youtube_scraper import (
    get_channel_feed as get_youtube_channel_feed,
    get_channel_from_video,
    search_channels_by_topic,
    search_videos_direct_by_topic,
)

from agent_tools.common import MAX_PREVIEW_LIMIT, error, success, truncate_text, youtube_video_preview

load_dotenv()


async def _search_web_results(query: str) -> list[dict[str, str]]:
    api_key = _pick_tavily_api_key()
    client = AsyncTavilyClient(api_key=api_key)
    payload = await client.search(
        query=query,
        max_results=MAX_PREVIEW_LIMIT,
        include_answer=False,
        include_raw_content=False,
    )
    results = payload.get("results", [])
    if not isinstance(results, list):
        raise ValueError("Tavily search payload is missing a results list")

    normalized: list[dict[str, str]] = []
    for item in results[:MAX_PREVIEW_LIMIT]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()
        snippet = str(item.get("content", "") or item.get("snippet", "")).strip()
        if not url:
            continue
        normalized.append({"title": title, "url": url, "snippet": truncate_text(snippet, max_chars=80)})
    return normalized


def _pick_tavily_api_key() -> str:
    raw_value = os.getenv("TAVILY_API_KEYS", "").strip()
    if not raw_value:
        raise ValueError("TAVILY_API_KEYS is not configured")

    candidates = [item.strip() for item in raw_value.split(",") if item.strip()]
    if not candidates:
        raise ValueError("TAVILY_API_KEYS does not contain any usable API keys")

    return random.choice(candidates)


@tool(
    "discover_feeds",
    "Try a bundle of deterministic heuristics to find RSS/Atom/JSON feed URLs for a concrete publication, site, domain, or homepage URL. Use this only after you have identified the source itself. Do not use it for vague descriptions like 'Ace Attorney fan site'; use search_web first to identify the actual site.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source_name": {"type": "string"},
        },
        "required": ["source_name"],
    },
)
async def discover_feeds_tool(args: dict[str, Any]) -> dict[str, Any]:
    source_name = str(args.get("source_name", "")).strip()
    if not source_name:
        return error("discover_feeds requires a non-empty source_name")

    try:
        result = await discover_feeds_detailed(source_name)
    except Exception as exc:
        return error(f"discover_feeds failed for {source_name}: {exc}")

    payload = {
        "source_name": source_name,
        "homepage": result.homepage,
        "feeds": [feed.url for feed in result.feeds],
    }
    return success(payload)


@tool(
    "search_web",
    "Search the web for a topic and return a compact list of results with titles, URLs, and snippets.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
)
async def search_web_tool(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return error("search_web requires a non-empty query")

    try:
        results = await _search_web_results(query)
    except Exception as exc:
        return error(f"search_web failed for {query}: {exc}")

    return success({"query": query, "results": results})


@tool(
    "get_google_news_feed",
    "Build a synthetic Google News RSS search feed URL for a query. Use this when a source is valuable but you cannot find a native feed URL.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
)
async def get_google_news_feed_tool(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return error("get_google_news_feed requires a non-empty query")

    try:
        feed_url = google_news_search_feed_url(query)
    except Exception as exc:
        return error(f"get_google_news_feed failed for {query}: {exc}")

    return success(
        {
            "query": query,
            "feed_url": feed_url,
            "source_spec": {"type": "google_news_search", "feed": query},
        }
    )


@tool(
    "search_rsscatalog",
    "Search RSS Catalog categories locally after scraping the homepage category list. Use this when you want extra RSS feed candidates from RSS Catalog.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
)
async def search_rsscatalog_tool(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return error("search_rsscatalog requires a non-empty query")

    try:
        categories = await search_rsscatalog_categories(query, limit=MAX_PREVIEW_LIMIT)
    except Exception as exc:
        return error(f"search_rsscatalog failed for {query}: {exc}")

    return success(
        {
            "query": query,
            "category_count": len(categories),
            "categories": categories,
        }
    )


@tool(
    "get_rsscatalog_category_feeds",
    "Fetch feed URLs from a specific RSS Catalog category page.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "category": {"type": "string"},
        },
        "required": ["category"],
    },
)
async def get_rsscatalog_category_feeds_tool(args: dict[str, Any]) -> dict[str, Any]:
    category = str(args.get("category", "")).strip()
    if not category:
        return error("get_rsscatalog_category_feeds requires a non-empty category")

    try:
        feeds = await get_category_feeds(category, limit=MAX_PREVIEW_LIMIT)
    except Exception as exc:
        return error(f"get_rsscatalog_category_feeds failed for {category}: {exc}")

    return success(
        {
            "category": category,
            "feed_count": len(feeds),
            "feeds": feeds,
        }
    )


@tool(
    "search_subreddits",
    "Search Reddit for relevant subreddits for a topic and return subreddit metadata.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
)
async def search_subreddits_tool(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return error("search_subreddits requires a non-empty query")

    try:
        subreddits = await search_subreddits_by_topic(query)
    except Exception as exc:
        return error(f"search_subreddits failed for {query}: {exc}")

    return success(
        {
            "query": query,
            "subreddits": [
                {
                    "subreddit_name": str(subreddit.get("subreddit_name", "")).strip(),
                    "title": str(subreddit.get("title", "")).strip(),
                    "subscriber_count": subreddit.get("subscriber_count"),
                    "feed_url": subreddit_feed_url(str(subreddit.get("subreddit_name", "")).strip()),
                    "description": truncate_text(subreddit.get("description", ""), max_chars=100),
                }
                for subreddit in subreddits[:MAX_PREVIEW_LIMIT]
            ],
        }
    )


@tool(
    "search_youtube_videos",
    "Search YouTube videos directly and return the top video results in API relevance order.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
)
async def search_youtube_videos_tool(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return error("search_youtube_videos requires a non-empty query")

    try:
        videos = await search_videos_direct_by_topic(query)
    except Exception as exc:
        return error(f"search_youtube_videos failed for {query}: {exc}")

    return success(
        {
            "query": query,
            "videos": [
                {
                    **youtube_video_preview(video),
                    "description": truncate_text(video.get("description", ""), max_chars=100),
                }
                for video in videos[:MAX_PREVIEW_LIMIT]
            ],
        }
    )


@tool(
    "search_youtube_channels",
    "Search YouTube channels by topic and return verified channel results.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
)
async def search_youtube_channels_tool(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return error("search_youtube_channels requires a non-empty query")

    try:
        channels = await search_channels_by_topic(query)
    except Exception as exc:
        return error(f"search_youtube_channels failed for {query}: {exc}")

    return success(
        {
            "query": query,
            "channels": [
                {
                    "channel_id": str(channel.get("channel_id", "")).strip(),
                    "channel_name": str(channel.get("channel_name", "")).strip(),
                    "subscriber_count": channel.get("subscriber_count"),
                    "feed_url": str(channel.get("feed_url", "")).strip(),
                    "description": truncate_text(channel.get("description", ""), max_chars=100),
                }
                for channel in channels[:MAX_PREVIEW_LIMIT]
            ],
        }
    )


@tool(
    "get_channel_from_video",
    "Resolve a YouTube video id to its parent verified YouTube channel.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "video_id": {"type": "string"},
        },
        "required": ["video_id"],
    },
)
async def get_channel_from_video_tool(args: dict[str, Any]) -> dict[str, Any]:
    video_id = str(args.get("video_id", "")).strip()
    if not video_id:
        return error("get_channel_from_video requires a non-empty video_id")

    try:
        channel = await get_channel_from_video(video_id)
    except Exception as exc:
        return error(f"get_channel_from_video failed for {video_id}: {exc}")

    return success({"video_id": video_id, "channel": channel})


@tool(
    "get_subreddit_from_post",
    "Resolve a Reddit post URL to its parent subreddit metadata.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "post_url": {"type": "string", "format": "uri"},
        },
        "required": ["post_url"],
    },
)
async def get_subreddit_from_post_tool(args: dict[str, Any]) -> dict[str, Any]:
    post_url = str(args.get("post_url", "")).strip()
    if not post_url:
        return error("get_subreddit_from_post requires a non-empty post_url")

    try:
        subreddit = await get_subreddit_from_post(post_url)
    except Exception as exc:
        return error(f"get_subreddit_from_post failed for {post_url}: {exc}")

    return success({"post_url": post_url, "subreddit": subreddit})


@tool(
    "get_channel_feed",
    "Resolve a YouTube channel id to its verified channel feed URL.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "channel_id": {"type": "string"},
        },
        "required": ["channel_id"],
    },
)
async def get_channel_feed_tool(args: dict[str, Any]) -> dict[str, Any]:
    channel_id = str(args.get("channel_id", "")).strip()
    if not channel_id:
        return error("get_channel_feed requires a non-empty channel_id")

    try:
        channel = await get_youtube_channel_feed(channel_id)
    except Exception as exc:
        return error(f"get_channel_feed failed for {channel_id}: {exc}")

    return success(
        {
            "channel_id": channel_id,
            "feed_url": None if channel is None else channel["feed_url"],
            "channel": channel,
        }
    )


@tool(
    "get_subreddit_feed",
    "Build the RSS feed URL for a subreddit.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "subreddit": {"type": "string"},
        },
        "required": ["subreddit"],
    },
)
async def get_subreddit_feed_tool(args: dict[str, Any]) -> dict[str, Any]:
    subreddit = str(args.get("subreddit", "")).strip()
    if not subreddit:
        return error("get_subreddit_feed requires a non-empty subreddit")

    return success({"subreddit": subreddit, "feed_url": subreddit_feed_url(subreddit)})


DISCOVERY_TOOLS = [
    discover_feeds_tool,
    search_web_tool,
    get_google_news_feed_tool,
    search_rsscatalog_tool,
    get_rsscatalog_category_feeds_tool,
    search_subreddits_tool,
    search_youtube_videos_tool,
    search_youtube_channels_tool,
    get_channel_from_video_tool,
    get_subreddit_from_post_tool,
    get_channel_feed_tool,
    get_subreddit_feed_tool,
]
