"""Feed-oriented Claude Agent SDK tools."""

from typing import Any

from claude_agent_sdk import tool

from app.agents.cache import fetch_articles_cached
from app.sources.nitter import fetch_search_feed, fetch_user_feed
from app.sources.reddit import search_reddit_posts
from app.sources.rss import fetch_rss_articles

from app.agents.agent_tools.common import (
    DEFAULT_PREVIEW_LIMIT,
    SOURCE_SPEC_SCHEMA,
    articles_preview_payload,
    error,
    log_tool_done,
    log_tool_event,
    reddit_article_to_post,
    success,
    tool_timer,
)


@tool(
    "preview_feed",
    "Fetch a raw RSS/Atom feed URL and preview the normalized articles returned by it.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "feed_url": {"type": "string", "format": "uri"},
        },
        "required": ["feed_url"],
    },
)
async def preview_feed_tool(args: dict[str, Any]) -> dict[str, Any]:
    timer = tool_timer(tool_name="preview_feed_tool")
    feed_url = str(args.get("feed_url", "")).strip()
    if not feed_url:
        return error("preview_feed requires a non-empty feed_url")
    log_tool_event("start", {"feed_url": feed_url}, tool_name="preview_feed_tool")

    try:
        articles = await fetch_rss_articles([feed_url])
    except Exception as exc:
        return error(f"preview_feed failed for {feed_url}: {exc}")
    log_tool_done(timer, {"article_count": len(articles)})

    return success(
        articles_preview_payload(
            label="feed_preview",
            input_payload={"feed_url": feed_url},
            articles=articles,
            limit=DEFAULT_PREVIEW_LIMIT,
        )
    )


@tool(
    "preview_sources",
    "Fetch heterogeneous source specs through runner.py and preview the normalized articles returned.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "sources": {
                "type": "array",
                "items": SOURCE_SPEC_SCHEMA,
                "minItems": 1,
            },
        },
        "required": ["sources"],
    },
)
async def preview_sources_tool(args: dict[str, Any]) -> dict[str, Any]:
    timer = tool_timer(tool_name="preview_sources_tool")
    sources = args.get("sources")
    if not isinstance(sources, list) or not sources:
        return error("preview_sources requires a non-empty sources list")
    log_tool_event("start", {"source_count": len(sources), "sources": sources}, tool_name="preview_sources_tool")

    try:
        articles = await fetch_articles_cached(sources)
    except Exception as exc:
        return error(f"preview_sources failed: {exc}")
    log_tool_done(timer, {"article_count": len(articles)})

    return success(
        articles_preview_payload(
            label="sources_preview",
            input_payload={"sources": sources},
            articles=articles,
            limit=DEFAULT_PREVIEW_LIMIT,
        )
    )


@tool(
    "preview_reddit_subreddit",
    "Preview normalized articles for one Reddit subreddit source.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "subreddit": {"type": "string"},
        },
        "required": ["subreddit"],
    },
)
async def preview_reddit_subreddit_tool(args: dict[str, Any]) -> dict[str, Any]:
    timer = tool_timer(tool_name="preview_reddit_subreddit_tool")
    subreddit = str(args.get("subreddit", "")).strip()
    if not subreddit:
        return error("preview_reddit_subreddit requires a non-empty subreddit")
    log_tool_event("start", {"subreddit": subreddit}, tool_name="preview_reddit_subreddit_tool")

    source = {"type": "reddit_subreddit", "feed": subreddit}
    try:
        articles = await fetch_articles_cached([source])
    except Exception as exc:
        return error(f"preview_reddit_subreddit failed for {subreddit}: {exc}")
    log_tool_done(timer, {"article_count": len(articles), "subreddit": subreddit})

    return success(
        articles_preview_payload(
            label="reddit_subreddit_preview",
            input_payload={"subreddit": subreddit},
            articles=articles,
            limit=DEFAULT_PREVIEW_LIMIT,
        )
    )


@tool(
    "preview_reddit_search",
    "Preview normalized articles for one Reddit search query source.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
)
async def preview_reddit_search_tool(args: dict[str, Any]) -> dict[str, Any]:
    timer = tool_timer(tool_name="preview_reddit_search_tool")
    query = str(args.get("query", "")).strip()
    if not query:
        return error("preview_reddit_search requires a non-empty query")
    log_tool_event("start", {"query": query}, tool_name="preview_reddit_search_tool")

    source = {"type": "reddit_search", "feed": query}
    try:
        articles = await fetch_articles_cached([source])
    except Exception as exc:
        return error(f"preview_reddit_search failed for {query}: {exc}")
    log_tool_done(timer, {"article_count": len(articles), "query": query})

    return success(
        articles_preview_payload(
            label="reddit_search_preview",
            input_payload={"query": query},
            articles=articles,
            limit=DEFAULT_PREVIEW_LIMIT,
        )
    )


@tool(
    "preview_reddit_subreddits_by_topic",
    "Preview normalized articles for a Reddit topic-discovery source.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "topic": {"type": "string"},
        },
        "required": ["topic"],
    },
)
async def preview_reddit_subreddits_by_topic_tool(args: dict[str, Any]) -> dict[str, Any]:
    timer = tool_timer(tool_name="preview_reddit_subreddits_by_topic_tool")
    topic = str(args.get("topic", "")).strip()
    if not topic:
        return error("preview_reddit_subreddits_by_topic requires a non-empty topic")
    log_tool_event("start", {"topic": topic}, tool_name="preview_reddit_subreddits_by_topic_tool")

    source = {"type": "reddit_subreddits_by_topic", "feed": topic}
    try:
        articles = await fetch_articles_cached([source])
    except Exception as exc:
        return error(f"preview_reddit_subreddits_by_topic failed for {topic}: {exc}")
    log_tool_done(timer, {"article_count": len(articles), "topic": topic})

    return success(
        articles_preview_payload(
            label="reddit_subreddits_by_topic_preview",
            input_payload={"topic": topic},
            articles=articles,
            limit=DEFAULT_PREVIEW_LIMIT,
        )
    )


@tool(
    "search_reddit_posts",
    "Search Reddit posts globally or within a specific subreddit and return normalized post previews.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
            "subreddit": {"type": "string"},
        },
        "required": ["query"],
    },
)
async def search_reddit_posts_tool(args: dict[str, Any]) -> dict[str, Any]:
    timer = tool_timer(tool_name="search_reddit_posts_tool")
    query = str(args.get("query", "")).strip()
    subreddit = str(args.get("subreddit", "")).strip() or None
    if not query:
        return error("search_reddit_posts requires a non-empty query")
    log_tool_event(
        "start",
        {"query": query, "subreddit": subreddit},
        tool_name="search_reddit_posts_tool",
    )

    try:
        posts = await search_reddit_posts(query, subreddit=subreddit)
    except Exception as exc:
        scope = subreddit or "all of Reddit"
        return error(f"search_reddit_posts failed for {query} in {scope}: {exc}")
    log_tool_done(
        timer,
        {"post_count": len(posts), "query": query, "subreddit": subreddit},
    )

    return success(
        {
            "query": query,
            "subreddit": subreddit,
            "post_count": len(posts),
            "posts": [reddit_article_to_post(post) for post in posts[:DEFAULT_PREVIEW_LIMIT]],
        }
    )


@tool(
    "preview_nitter_user",
    "Preview normalized articles for a Nitter username.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "username": {"type": "string"},
        },
        "required": ["username"],
    },
)
async def preview_nitter_user_tool(args: dict[str, Any]) -> dict[str, Any]:
    timer = tool_timer(tool_name="preview_nitter_user_tool")
    username = str(args.get("username", "")).strip()
    if not username:
        return error("preview_nitter_user requires a non-empty username")
    log_tool_event("start", {"username": username}, tool_name="preview_nitter_user_tool")

    try:
        feed = await fetch_user_feed(username.lstrip("@"))
        articles = feed.to_articles()
    except Exception as exc:
        return error(f"preview_nitter_user failed for {username}: {exc}")
    log_tool_done(timer, {"article_count": len(articles), "username": username})

    return success(
        articles_preview_payload(
            label="nitter_user_preview",
            input_payload={"username": username},
            articles=articles,
            limit=DEFAULT_PREVIEW_LIMIT,
        )
    )


@tool(
    "preview_nitter_search",
    "Preview normalized articles for a Nitter search query.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
)
async def preview_nitter_search_tool(args: dict[str, Any]) -> dict[str, Any]:
    timer = tool_timer(tool_name="preview_nitter_search_tool")
    query = str(args.get("query", "")).strip()
    if not query:
        return error("preview_nitter_search requires a non-empty query")
    log_tool_event("start", {"query": query}, tool_name="preview_nitter_search_tool")

    try:
        feed = await fetch_search_feed(query)
        articles = feed.to_articles()
    except Exception as exc:
        return error(f"preview_nitter_search failed for {query}: {exc}")
    log_tool_done(timer, {"article_count": len(articles), "query": query})

    return success(
        articles_preview_payload(
            label="nitter_search_preview",
            input_payload={"query": query},
            articles=articles,
            limit=DEFAULT_PREVIEW_LIMIT,
        )
    )


@tool(
    "preview_youtube_channel",
    "Preview normalized articles for one YouTube channel-like source.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "channel": {"type": "string"},
        },
        "required": ["channel"],
    },
)
async def preview_youtube_channel_tool(args: dict[str, Any]) -> dict[str, Any]:
    timer = tool_timer(tool_name="preview_youtube_channel_tool")
    channel = str(args.get("channel", "")).strip()
    if not channel:
        return error("preview_youtube_channel requires a non-empty channel")
    log_tool_event("start", {"channel": channel}, tool_name="preview_youtube_channel_tool")

    source = {"type": "youtube_channel", "feed": channel}
    try:
        articles = await fetch_articles_cached([source])
    except Exception as exc:
        return error(f"preview_youtube_channel failed for {channel}: {exc}")
    log_tool_done(timer, {"article_count": len(articles), "channel": channel})

    return success(
        articles_preview_payload(
            label="youtube_channel_preview",
            input_payload={"channel": channel},
            articles=articles,
            limit=DEFAULT_PREVIEW_LIMIT,
        )
    )


@tool(
    "preview_youtube_search",
    "Preview normalized articles for one YouTube search query source.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
)
async def preview_youtube_search_tool(args: dict[str, Any]) -> dict[str, Any]:
    timer = tool_timer(tool_name="preview_youtube_search_tool")
    query = str(args.get("query", "")).strip()
    if not query:
        return error("preview_youtube_search requires a non-empty query")
    log_tool_event("start", {"query": query}, tool_name="preview_youtube_search_tool")

    source = {"type": "youtube_search", "feed": query}
    try:
        articles = await fetch_articles_cached([source])
    except Exception as exc:
        return error(f"preview_youtube_search failed for {query}: {exc}")
    log_tool_done(timer, {"article_count": len(articles), "query": query})

    return success(
        articles_preview_payload(
            label="youtube_search_preview",
            input_payload={"query": query},
            articles=articles,
            limit=DEFAULT_PREVIEW_LIMIT,
        )
    )


@tool(
    "preview_youtube_channels_by_topic",
    "Preview normalized articles for YouTube channel discovery by topic.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "topic": {"type": "string"},
        },
        "required": ["topic"],
    },
)
async def preview_youtube_channels_by_topic_tool(args: dict[str, Any]) -> dict[str, Any]:
    timer = tool_timer(tool_name="preview_youtube_channels_by_topic_tool")
    topic = str(args.get("topic", "")).strip()
    if not topic:
        return error("preview_youtube_channels_by_topic requires a non-empty topic")
    log_tool_event("start", {"topic": topic}, tool_name="preview_youtube_channels_by_topic_tool")

    source = {"type": "youtube_channels_by_topic", "feed": topic}
    try:
        articles = await fetch_articles_cached([source])
    except Exception as exc:
        return error(f"preview_youtube_channels_by_topic failed for {topic}: {exc}")
    log_tool_done(timer, {"article_count": len(articles), "topic": topic})

    return success(
        articles_preview_payload(
            label="youtube_channels_by_topic_preview",
            input_payload={"topic": topic},
            articles=articles,
            limit=DEFAULT_PREVIEW_LIMIT,
        )
    )


@tool(
    "preview_youtube_videos_by_topic",
    "Preview normalized articles for YouTube video discovery by topic.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "topic": {"type": "string"},
        },
        "required": ["topic"],
    },
)
async def preview_youtube_videos_by_topic_tool(args: dict[str, Any]) -> dict[str, Any]:
    timer = tool_timer(tool_name="preview_youtube_videos_by_topic_tool")
    topic = str(args.get("topic", "")).strip()
    if not topic:
        return error("preview_youtube_videos_by_topic requires a non-empty topic")
    log_tool_event("start", {"topic": topic}, tool_name="preview_youtube_videos_by_topic_tool")

    source = {"type": "youtube_videos_by_topic", "feed": topic}
    try:
        articles = await fetch_articles_cached([source])
    except Exception as exc:
        return error(f"preview_youtube_videos_by_topic failed for {topic}: {exc}")
    log_tool_done(timer, {"article_count": len(articles), "topic": topic})

    return success(
        articles_preview_payload(
            label="youtube_videos_by_topic_preview",
            input_payload={"topic": topic},
            articles=articles,
            limit=DEFAULT_PREVIEW_LIMIT,
        )
    )


@tool(
    "preview_tavily_search",
    "Preview normalized articles for one Tavily search query source.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
)
async def preview_tavily_search_tool(args: dict[str, Any]) -> dict[str, Any]:
    timer = tool_timer(tool_name="preview_tavily_search_tool")
    query = str(args.get("query", "")).strip()
    if not query:
        return error("preview_tavily_search requires a non-empty query")
    log_tool_event("start", {"query": query}, tool_name="preview_tavily_search_tool")

    source = {"type": "tavily", "feed": query}
    try:
        articles = await fetch_articles_cached([source])
    except Exception as exc:
        return error(f"preview_tavily_search failed for {query}: {exc}")
    log_tool_done(timer, {"article_count": len(articles), "query": query})

    return success(
        articles_preview_payload(
            label="tavily_search_preview",
            input_payload={"query": query},
            articles=articles,
            limit=DEFAULT_PREVIEW_LIMIT,
        )
    )


FEED_TOOLS = [
    preview_feed_tool,
    preview_sources_tool,
    preview_reddit_subreddit_tool,
    preview_reddit_search_tool,
    preview_reddit_subreddits_by_topic_tool,
    search_reddit_posts_tool,
    preview_nitter_user_tool,
    preview_nitter_search_tool,
    preview_youtube_channel_tool,
    preview_youtube_search_tool,
    preview_youtube_channels_by_topic_tool,
    preview_youtube_videos_by_topic_tool,
    preview_tavily_search_tool,
]
