from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

SOURCE_SPEC_TYPES = {
    "rss",
    "tavily",
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

YOUTUBE_SUBMISSION_TYPES = {
    "channel",
    "channel_url",
    "search",
    "channels_by_topic",
    "videos_by_topic",
}

REDDIT_SUBMISSION_TYPES = {
    "subreddit",
    "search",
    "subreddits_by_topic",
}

NITTER_SUBMISSION_TYPES = {
    "user",
    "search",
}

TAVILY_SUBMISSION_TYPES = {
    "search",
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
    submitted_type = str(source.get("type", "")).strip().lower()
    feed = str(source.get("feed", "")).strip()
    if not feed:
        raise ValueError(f"{label} is missing a non-empty feed value")

    if agent_name == "youtube":
        return canonicalize_youtube_source_spec(submitted_type, feed, label=label)
    if agent_name == "reddit":
        return canonicalize_reddit_source_spec(submitted_type, feed, label=label)
    if agent_name == "nitter":
        return canonicalize_nitter_source_spec(submitted_type, feed, label=label)
    if agent_name == "tavily":
        return canonicalize_tavily_source_spec(submitted_type, feed, label=label)
    return validate_source_spec(source, label=label)


def canonicalize_youtube_source_spec(submitted_type: str, feed: str, *, label: str) -> dict[str, str]:
    if submitted_type not in YOUTUBE_SUBMISSION_TYPES:
        raise ValueError(
            f"{label} has unsupported youtube type: {submitted_type}. "
            "Expected one of: channel, channel_url, search, channels_by_topic, videos_by_topic"
        )

    if submitted_type == "channel":
        channel_id = extract_youtube_channel_id_from_feed_url(feed)
        if channel_id:
            return {"type": "youtube_channel", "feed": channel_id}
        return {"type": "youtube_channel", "feed": feed}
    if submitted_type == "channel_url":
        return {"type": "youtube_channel_url", "feed": feed}
    if submitted_type == "search":
        return {"type": "youtube_search", "feed": feed}
    if submitted_type == "channels_by_topic":
        return {"type": "youtube_channels_by_topic", "feed": feed}
    return {"type": "youtube_videos_by_topic", "feed": feed}


def canonicalize_reddit_source_spec(submitted_type: str, feed: str, *, label: str) -> dict[str, str]:
    if submitted_type not in REDDIT_SUBMISSION_TYPES:
        raise ValueError(
            f"{label} has unsupported reddit type: {submitted_type}. "
            "Expected one of: subreddit, search, subreddits_by_topic"
        )

    if submitted_type == "subreddit":
        subreddit_name = extract_reddit_subreddit_name(feed)
        if subreddit_name:
            return {"type": "reddit_subreddit", "feed": subreddit_name}
        normalized = feed.strip()
        if normalized.lower().startswith("r/"):
            normalized = normalized[2:]
        return {"type": "reddit_subreddit", "feed": normalized.strip("/")}
    if submitted_type == "search":
        if looks_like_reddit_url(feed):
            query = extract_reddit_search_query(feed)
            if query:
                return {"type": "reddit_search", "feed": query}
        return {"type": "reddit_search", "feed": feed}
    return {"type": "reddit_subreddits_by_topic", "feed": feed}


def canonicalize_nitter_source_spec(submitted_type: str, feed: str, *, label: str) -> dict[str, str]:
    if submitted_type not in NITTER_SUBMISSION_TYPES:
        raise ValueError(
            f"{label} has unsupported nitter type: {submitted_type}. Expected one of: user, search"
        )
    if submitted_type == "user":
        normalized = feed.strip()
        if normalized.startswith("@"):
            normalized = normalized[1:]
        return {"type": "nitter_user", "feed": normalized}
    return {"type": "nitter_search", "feed": feed}


def canonicalize_tavily_source_spec(submitted_type: str, feed: str, *, label: str) -> dict[str, str]:
    if submitted_type not in TAVILY_SUBMISSION_TYPES:
        raise ValueError(
            f"{label} has unsupported tavily type: {submitted_type}. Expected one of: search"
        )
    return {"type": "tavily", "feed": feed}


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
