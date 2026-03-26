"""Feed-oriented Claude Agent SDK tools."""

from typing import Any

from claude_agent_sdk import tool

from nitter import fetch_search_feed, fetch_user_feed
from reddit import search_reddit_posts
from rss import fetch_rss_articles
from runner import fetch_articles

from agent_tools.common import (
    DEFAULT_PREVIEW_LIMIT,
    SOURCE_SPEC_SCHEMA,
    articles_preview_payload,
    error,
    reddit_article_to_post,
    success,
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
    feed_url = str(args.get("feed_url", "")).strip()
    if not feed_url:
        return error("preview_feed requires a non-empty feed_url")

    try:
        articles = await fetch_rss_articles([feed_url])
    except Exception as exc:
        return error(f"preview_feed failed for {feed_url}: {exc}")

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
    sources = args.get("sources")
    if not isinstance(sources, list) or not sources:
        return error("preview_sources requires a non-empty sources list")

    try:
        articles = await fetch_articles(sources)
    except Exception as exc:
        return error(f"preview_sources failed: {exc}")

    return success(
        articles_preview_payload(
            label="sources_preview",
            input_payload={"sources": sources},
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
    query = str(args.get("query", "")).strip()
    subreddit = str(args.get("subreddit", "")).strip() or None
    if not query:
        return error("search_reddit_posts requires a non-empty query")

    try:
        posts = await search_reddit_posts(query, subreddit=subreddit)
    except Exception as exc:
        scope = subreddit or "all of Reddit"
        return error(f"search_reddit_posts failed for {query} in {scope}: {exc}")

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
    username = str(args.get("username", "")).strip()
    if not username:
        return error("preview_nitter_user requires a non-empty username")

    try:
        feed = await fetch_user_feed(username.lstrip("@"))
        articles = feed.to_articles()
    except Exception as exc:
        return error(f"preview_nitter_user failed for {username}: {exc}")

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
    query = str(args.get("query", "")).strip()
    if not query:
        return error("preview_nitter_search requires a non-empty query")

    try:
        feed = await fetch_search_feed(query)
        articles = feed.to_articles()
    except Exception as exc:
        return error(f"preview_nitter_search failed for {query}: {exc}")

    return success(
        articles_preview_payload(
            label="nitter_search_preview",
            input_payload={"query": query},
            articles=articles,
            limit=DEFAULT_PREVIEW_LIMIT,
        )
    )


FEED_TOOLS = [
    preview_feed_tool,
    preview_sources_tool,
    search_reddit_posts_tool,
    preview_nitter_user_tool,
    preview_nitter_search_tool,
]
