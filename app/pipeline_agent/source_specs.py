from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

SOURCE_SPEC_TYPES = {
    "rss",
    "google_news_search",
    "nitter_user",
    "nitter_search",
    "reddit_subreddit",
    "reddit_search",
    "reddit_subreddits_by_topic",
    "youtube_search",
    "youtube_channel",
    "youtube_channel_url",
    "youtube_channels_by_topic",
    "youtube_videos_by_topic",
}


def validate_source_spec(source: dict[str, Any], *, label: str) -> dict[str, str]:
    source_type = str(source.get("type", "")).strip()
    feed = str(source.get("feed", "")).strip()
    if source_type not in SOURCE_SPEC_TYPES:
        raise ValueError(f"{label} has unsupported type: {source_type}")
    if not feed:
        raise ValueError(f"{label} is missing a non-empty feed value")
    return {"type": source_type, "feed": feed}


def normalize_submitted_source_spec(
    source: dict[str, Any],
    *,
    agent_name: str,
    label: str,
) -> dict[str, str]:
    feed = str(source.get("feed", "")).strip()
    if not feed:
        raise ValueError(f"{label} is missing a non-empty feed value")

    if agent_name == "youtube":
        return canonicalize_youtube_source_spec(feed)
    if agent_name == "reddit":
        return canonicalize_reddit_source_spec(feed)
    return validate_source_spec(source, label=label)


def canonicalize_youtube_source_spec(feed: str) -> dict[str, str]:
    channel_id = extract_youtube_channel_id_from_feed_url(feed)
    if channel_id:
        return {"type": "youtube_channel", "feed": channel_id}
    if looks_like_youtube_url(feed) or feed.startswith("UC"):
        return {"type": "youtube_channel", "feed": feed}
    return {"type": "youtube_search", "feed": feed}


def canonicalize_reddit_source_spec(feed: str) -> dict[str, str]:
    subreddit_name = extract_reddit_subreddit_name(feed)
    if subreddit_name:
        return {"type": "reddit_subreddit", "feed": subreddit_name}

    if looks_like_reddit_url(feed):
        query = extract_reddit_search_query(feed)
        if query:
            return {"type": "reddit_search", "feed": query}

    normalized = feed.strip()
    if normalized.lower().startswith("r/"):
        normalized = normalized[2:]
    if looks_like_simple_subreddit_name(normalized):
        return {"type": "reddit_subreddit", "feed": normalized}
    return {"type": "reddit_search", "feed": feed}


def extract_youtube_channel_id_from_feed_url(value: str) -> str | None:
    parsed = urlparse(value)
    hostname = parsed.netloc.lower()
    if hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        return None
    if parsed.path != "/feeds/videos.xml":
        return None
    channel_ids = parse_qs(parsed.query).get("channel_id", [])
    for channel_id in channel_ids:
        normalized = str(channel_id).strip()
        if normalized:
            return normalized
    return None


def looks_like_youtube_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and "youtube.com" in parsed.netloc.lower()


def extract_reddit_subreddit_name(value: str) -> str | None:
    normalized = value.strip()
    if normalized.lower().startswith("r/"):
        candidate = normalized[2:].strip().strip("/")
        return candidate or None

    parsed = urlparse(normalized)
    if parsed.scheme in {"http", "https"} and "reddit.com" in parsed.netloc.lower():
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) >= 2 and path_parts[0].lower() == "r":
            candidate = unquote(path_parts[1]).strip()
            return candidate or None
    return None


def extract_reddit_search_query(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or "reddit.com" not in parsed.netloc.lower():
        return None
    query_values = parse_qs(parsed.query).get("q", [])
    for query in query_values:
        normalized = unquote(str(query)).strip()
        if normalized:
            return normalized
    return None


def looks_like_reddit_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and "reddit.com" in parsed.netloc.lower()


def looks_like_simple_subreddit_name(value: str) -> bool:
    if not value or " " in value or "/" in value:
        return False
    return value.replace("_", "").isalnum()
