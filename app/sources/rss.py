"""
RSS article fetching and normalization.

Input:
- `feed_urls: list[str]`
- Each item should be a fetchable RSS or Atom feed URL.

Output:
- Returns `list[dict[str, Any]]`
- Each article dict has this shape:
  {
      "id": str,
      "title": str,
      "url": str,
      "published_at": str,
      "content": str,
      "full_text": str,
      "source_url": str,
      "source_name": str,
      "source_type": "rss",
      "raw": dict,
  }
"""

import asyncio
import calendar
import hashlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from urllib.parse import urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

ARTICLE_FETCH_TIMEOUT = 20.0


class FeedFetchError(RuntimeError):
    """Raised when a feed cannot be fetched or normalized safely."""


class FeedBatchError(RuntimeError):
    """Raised when one or more RSS feeds fail during a batch fetch."""

    def __init__(self, errors: list[Exception]):
        self.errors = errors
        message = "; ".join(str(error) for error in errors)
        super().__init__(message)


class ArticleParseError(ValueError):
    """Raised when an individual article breaks feed normalization."""


async def fetch_rss_articles(feed_urls: list[str]) -> list[dict[str, Any]]:
    """Fetch and normalize articles from RSS/Atom feed URLs."""
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=ARTICLE_FETCH_TIMEOUT,
        headers={
            "accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "user-agent": "feed-builder/1.0",
        },
    ) as http_client:
        results = await asyncio.gather(
            *[_fetch_feed(feed_url, http_client) for feed_url in feed_urls],
            return_exceptions=True,
        )

    errors = [result for result in results if isinstance(result, Exception)]
    if errors:
        raise FeedBatchError(errors)

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


async def _fetch_feed(feed_url: str, http_client: httpx.AsyncClient) -> list[dict[str, Any]]:
    try:
        response = await http_client.get(feed_url)
        response.raise_for_status()
    except Exception as exc:
        raise FeedFetchError(f"Failed to download feed {feed_url}: {exc}") from exc

    parsed = await asyncio.to_thread(feedparser.parse, response.content)
    entries = list(getattr(parsed, "entries", []) or [])

    if not entries:
        bozo_exc = getattr(parsed, "bozo_exception", None)
        detail = f" ({bozo_exc})" if bozo_exc else ""
        raise FeedFetchError(f"Feed {feed_url} did not contain any parseable entries{detail}")

    feed_meta = getattr(parsed, "feed", {}) or {}
    source_name = _clean_text(_get_mapping_value(feed_meta, "title")) or _source_name_from_url(feed_url)

    articles: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        try:
            article = await _parse_entry(entry, feed_url=feed_url, source_name=source_name)
        except Exception as exc:
            raise FeedFetchError(
                f"Feed {feed_url} failed while parsing article #{index}: {exc}"
            ) from exc

        if article is None:
            continue
        articles.append(article)

    return articles


async def _parse_entry(
    entry: Any,
    *,
    feed_url: str,
    source_name: str,
) -> dict[str, Any] | None:
    title = _clean_text(_get_mapping_value(entry, "title"))
    url = _clean_text(_extract_entry_url(entry))

    if not title or not url:
        return None

    content = _extract_content(entry, title=title, feed_url=feed_url)
    if not content:
        return None
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
        "source_type": "rss",
        "raw": _to_plain_data(entry),
    }


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


def _extract_content(entry: Any, *, title: str, feed_url: str) -> str:
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

    # Some valid YouTube channel feed entries have empty descriptions.
    # Keep generic RSS strict, but allow a minimal deterministic fallback here.
    if _is_youtube_feed_url(feed_url):
        return _clean_text(title)

    return ""


def _is_youtube_feed_url(feed_url: str) -> bool:
    return "youtube.com/feeds/videos.xml" in feed_url.lower()


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
        raise ArticleParseError(f"Could not parse timestamp value: {value}")

    raise ArticleParseError("Article is missing a usable published timestamp")


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
    text = soup.get_text("\n", strip=True)
    return _clean_text(unescape(text))


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return " ".join(text.split())


def _get_mapping_value(mapping: Any, key: str) -> Any:
    if isinstance(mapping, dict):
        return mapping.get(key)
    return getattr(mapping, key, None)


def _source_name_from_url(feed_url: str) -> str:
    parsed = urlparse(feed_url)
    return parsed.netloc or feed_url


def _to_plain_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _to_plain_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_plain_data(item) for item in value]
    if isinstance(value, tuple):
        return [_to_plain_data(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "tm_year") and hasattr(value, "tm_mon"):
        return list(value)
    return str(value)


__all__ = ["ArticleParseError", "FeedBatchError", "FeedFetchError", "fetch_rss_articles"]
