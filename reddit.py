"""
Deterministic Reddit feed discovery and normalization.

Exports:
- `subreddit_feed_url(subreddit_name) -> str`
- `search_feed_url(query) -> str`
- `subreddit_search_feed_url(query, subreddit_name) -> str`
- `search_subreddits_by_topic(topic) -> list[Subreddit]`
- `search_reddit_posts(query, subreddit=None) -> list[dict[str, Any]]`
- `get_subreddit_from_post(post_url) -> Subreddit | None`
- `fetch_subreddit_articles(subreddit_names) -> list[dict[str, Any]]`
- `fetch_search_articles(query) -> list[dict[str, Any]]`

Behavior:
- Uses Reddit RSS feeds for subreddit and global search ingestion.
- Uses Reddit subreddit search JSON for topic-based subreddit discovery.
- Returns normalized article dicts with `source_type="reddit"`.
- Dedupes article ids within each call.
- Raises explicit batch/fetch errors instead of silently swallowing failures.
"""

import asyncio
import calendar
import hashlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any, TypedDict
from urllib.parse import quote, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

REDDIT_BASE = "https://www.reddit.com"
REDDIT_TIMEOUT = 20.0
REDDIT_SEARCH_LIMIT = 10


class Subreddit(TypedDict):
    """Normalized subreddit discovery result."""

    subreddit_name: str
    title: str
    description: str
    subscriber_count: int | None
    feed_url: str
    url: str


class RedditFetchError(RuntimeError):
    """Raised when a Reddit resource cannot be fetched or normalized safely."""


class RedditBatchError(RuntimeError):
    """Raised when one or more Reddit fetches fail during a batch call."""

    def __init__(self, errors: list[Exception]):
        self.errors = errors
        message = "; ".join(str(error) for error in errors)
        super().__init__(message)


class RedditArticleParseError(ValueError):
    """Raised when an individual Reddit entry cannot be normalized."""


async def fetch_subreddit_articles(subreddit_names: list[str]) -> list[dict[str, Any]]:
    """Fetch normalized articles from one or more subreddit feeds."""
    feed_specs = [(subreddit_feed_url(name), name.strip()) for name in subreddit_names if str(name).strip()]
    return await _fetch_reddit_feed_specs(feed_specs)


async def fetch_search_articles(query: str) -> list[dict[str, Any]]:
    """Fetch normalized articles from the Reddit global search RSS feed."""
    normalized_query = query.strip()
    if not normalized_query:
        return []
    return await _fetch_reddit_feed_specs([(search_feed_url(normalized_query), normalized_query)])


async def search_subreddits_by_topic(topic: str) -> list[Subreddit]:
    """Search Reddit for subreddits related to a topic."""
    normalized_topic = topic.strip()
    if not normalized_topic:
        return []

    async with _build_http_client() as http_client:
        response = await http_client.get(
            f"{REDDIT_BASE}/subreddits/search.json",
            params={
                "q": normalized_topic,
                "limit": str(REDDIT_SEARCH_LIMIT),
                "raw_json": "1",
            },
            headers=_reddit_headers(),
        )
        response.raise_for_status()

    payload = response.json()
    children = payload.get("data", {}).get("children", [])
    results: list[Subreddit] = []
    seen_names: set[str] = set()
    for child in children:
        data = child.get("data", {}) if isinstance(child, dict) else {}
        subreddit_name = str(data.get("display_name") or "").strip()
        if not subreddit_name or subreddit_name in seen_names:
            continue
        seen_names.add(subreddit_name)
        results.append(
            Subreddit(
                subreddit_name=subreddit_name,
                title=str(data.get("title") or subreddit_name).strip(),
                description=str(data.get("public_description") or data.get("description") or "").strip(),
                subscriber_count=_parse_optional_int(data.get("subscribers")),
                feed_url=subreddit_feed_url(subreddit_name),
                url=f"{REDDIT_BASE}/r/{subreddit_name}",
            )
        )
    return results


async def search_reddit_posts(query: str, subreddit: str | None = None) -> list[dict[str, Any]]:
    """Search Reddit posts globally or within a specific subreddit via RSS."""
    normalized_query = query.strip()
    if not normalized_query:
        return []

    normalized_subreddit = str(subreddit or "").strip()
    if normalized_subreddit:
        feed_url = subreddit_search_feed_url(normalized_query, normalized_subreddit)
        source_hint = normalized_subreddit
    else:
        feed_url = search_feed_url(normalized_query)
        source_hint = normalized_query

    return await _fetch_reddit_feed_specs([(feed_url, source_hint)])


async def get_subreddit_from_post(post_url: str) -> Subreddit | None:
    """Resolve a Reddit post URL to its parent subreddit metadata when possible."""
    subreddit_name = _extract_subreddit_name_from_url(post_url)
    if not subreddit_name:
        return None

    results = await search_subreddits_by_topic(subreddit_name)
    for result in results:
        if result["subreddit_name"].lower() == subreddit_name.lower():
            return result

    return Subreddit(
        subreddit_name=subreddit_name,
        title=subreddit_name,
        description="",
        subscriber_count=None,
        feed_url=subreddit_feed_url(subreddit_name),
        url=f"{REDDIT_BASE}/r/{subreddit_name}",
    )


async def _fetch_reddit_feed_specs(feed_specs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    if not feed_specs:
        return []

    async with _build_http_client() as http_client:
        results = await asyncio.gather(
            *[_fetch_feed(feed_url, source_hint=source_hint, http_client=http_client) for feed_url, source_hint in feed_specs],
            return_exceptions=True,
        )

    errors = [result for result in results if isinstance(result, Exception)]
    if errors:
        raise RedditBatchError(errors)

    seen_ids: set[str] = set()
    articles: list[dict[str, Any]] = []
    for feed_articles in results:
        for article in feed_articles:
            article_id = article["id"]
            if article_id in seen_ids:
                continue
            seen_ids.add(article_id)
            articles.append(article)
    return articles


async def _fetch_feed(feed_url: str, *, source_hint: str, http_client: httpx.AsyncClient) -> list[dict[str, Any]]:
    try:
        response = await http_client.get(feed_url, headers=_reddit_headers())
        response.raise_for_status()
    except Exception as exc:
        raise RedditFetchError(f"Failed to download Reddit feed {feed_url}: {exc}") from exc

    parsed = await asyncio.to_thread(feedparser.parse, response.content)
    entries = list(getattr(parsed, "entries", []) or [])
    if not entries:
        bozo_exc = getattr(parsed, "bozo_exception", None)
        detail = f" ({bozo_exc})" if bozo_exc else ""
        raise RedditFetchError(f"Reddit feed {feed_url} did not contain any parseable entries{detail}")

    feed_meta = getattr(parsed, "feed", {}) or {}
    source_name = _derive_source_name(feed_url, source_hint=source_hint, feed_meta=feed_meta)

    articles: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        try:
            article = _parse_entry(entry, feed_url=feed_url, source_name=source_name)
        except Exception as exc:
            raise RedditFetchError(
                f"Reddit feed {feed_url} failed while parsing article #{index}: {exc}"
            ) from exc
        if article is None:
            continue
        articles.append(article)

    return articles


def _parse_entry(entry: Any, *, feed_url: str, source_name: str) -> dict[str, Any] | None:
    title = _clean_text(_get_mapping_value(entry, "title"))
    url = _clean_text(_extract_entry_url(entry))
    if not title or not url:
        return None

    content = _extract_content(entry)
    if not content:
        raise RedditArticleParseError(f"Reddit article {url} has no usable feed content")
    published_at = _extract_published_at(entry)

    return {
        "id": hashlib.md5(url.encode("utf-8")).hexdigest(),
        "title": title,
        "url": url,
        "published_at": published_at,
        "content": content,
        "full_text": content,
        "source_url": feed_url,
        "source_name": source_name,
        "source_type": "reddit",
        "raw": _to_plain_data(entry),
    }


def subreddit_feed_url(subreddit_name: str) -> str:
    normalized = subreddit_name.strip()
    if normalized.lower().startswith("r/"):
        normalized = normalized[2:]
    return f"{REDDIT_BASE}/r/{normalized}/new/.rss?sort=new"


def search_feed_url(query: str) -> str:
    return f"{REDDIT_BASE}/search.rss?q={quote(query)}&sort=new"


def subreddit_search_feed_url(query: str, subreddit_name: str) -> str:
    normalized_subreddit = subreddit_name.strip()
    if normalized_subreddit.lower().startswith("r/"):
        normalized_subreddit = normalized_subreddit[2:]
    return f"{REDDIT_BASE}/r/{normalized_subreddit}/search.rss?q={quote(query)}&restrict_sr=on&sort=new"


def _derive_source_name(feed_url: str, *, source_hint: str, feed_meta: Any) -> str:
    title = _clean_text(_get_mapping_value(feed_meta, "title"))
    if title:
        return title

    parsed = urlparse(feed_url)
    path = parsed.path.rstrip("/")
    if "/r/" in path:
        subreddit_name = path.split("/r/", 1)[1].split("/", 1)[0]
        if subreddit_name:
            return f"r/{subreddit_name}"

    if path.endswith("/search.rss") or path.endswith("search.rss"):
        return f"Reddit search: {source_hint}"

    return source_hint or parsed.netloc or feed_url


def _extract_subreddit_name_from_url(post_url: str) -> str:
    parsed = urlparse(str(post_url).strip())
    if parsed.netloc.lower() not in {"reddit.com", "www.reddit.com", "old.reddit.com"}:
        return ""

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0].lower() == "r":
        return path_parts[1].strip()
    return ""


def _extract_entry_url(entry: Any) -> str:
    direct_link = _get_mapping_value(entry, "link")
    if direct_link:
        return str(direct_link)

    links = _get_mapping_value(entry, "links") or []
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue
            href = link.get("href")
            rel = str(link.get("rel", "alternate")).lower()
            if href and rel == "alternate":
                return str(href)
        for link in links:
            if isinstance(link, dict) and link.get("href"):
                return str(link["href"])

    entry_id = _get_mapping_value(entry, "id")
    return str(entry_id or "")


def _extract_content(entry: Any) -> str:
    candidates: list[str] = []

    content_items = _get_mapping_value(entry, "content") or []
    if isinstance(content_items, list):
        for item in content_items:
            if isinstance(item, dict):
                value = item.get("value") or item.get("content")
                if value:
                    candidates.append(str(value))

    for key in ("summary", "description"):
        value = _get_mapping_value(entry, key)
        if value:
            candidates.append(str(value))

    summary_detail = _get_mapping_value(entry, "summary_detail")
    if isinstance(summary_detail, dict) and summary_detail.get("value"):
        candidates.append(str(summary_detail["value"]))

    for value in candidates:
        normalized = _html_to_text(value)
        if normalized:
            return normalized

    return ""


def _extract_published_at(entry: Any) -> str:
    for parsed_key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = _get_mapping_value(entry, parsed_key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc).isoformat()

    for text_key in ("published", "updated", "created"):
        value = _clean_text(_get_mapping_value(entry, text_key))
        if not value:
            continue

        parsed = _parse_datetime_text(value)
        if parsed is not None:
            return parsed.isoformat()
        raise RedditArticleParseError(f"Could not parse Reddit timestamp value: {value}")

    raise RedditArticleParseError("Reddit article is missing a usable published timestamp")


def _parse_datetime_text(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
    except Exception:
        parsed = None

    if parsed is not None:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    iso_candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _html_to_text(value: str) -> str:
    soup = BeautifulSoup(value, "html.parser")
    text = soup.get_text("\n")
    return _clean_text(text)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = unescape(str(value))
    lines = [line.strip() for line in text.splitlines()]
    non_empty = [line for line in lines if line]
    return "\n".join(non_empty).strip()


def _get_mapping_value(mapping: Any, key: str) -> Any:
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)


def _to_plain_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _to_plain_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_plain_data(item) for item in value]
    if isinstance(value, tuple):
        return [_to_plain_data(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "keys"):
        try:
            return {str(key): _to_plain_data(value[key]) for key in value.keys()}
        except Exception:
            pass
    return str(value)


def _parse_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=REDDIT_TIMEOUT,
        headers=_reddit_headers(),
    )


def _reddit_headers() -> dict[str, str]:
    return {"user-agent": "feed-builder/1.0"}


__all__ = [
    "RedditArticleParseError",
    "RedditBatchError",
    "RedditFetchError",
    "Subreddit",
    "fetch_search_articles",
    "fetch_subreddit_articles",
    "get_subreddit_from_post",
    "search_feed_url",
    "search_reddit_posts",
    "search_subreddits_by_topic",
    "subreddit_search_feed_url",
    "subreddit_feed_url",
]
