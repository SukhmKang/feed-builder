import asyncio
import calendar
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from urllib.parse import urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from reddit import search_subreddits_by_topic, subreddit_feed_url
from runner import SourceSpec, _enrich_articles_with_youtube_transcripts, _youtube_video_to_article
from stream_history import _write_articles_sync, replay_stream_from_cache
from youtube_scraper import (
    _youtube_get,
    get_channel_feed,
    search_channels_by_topic,
    search_videos_by_topic,
)

WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_FETCH_DELAY_SECONDS = 1.0
WAYBACK_SNAPSHOT_LIMIT = 10
REPLAY_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
ARCTIC_SHIFT_URL = "https://arctic-shift.photon-reddit.com/api/posts/search"
ARCTIC_SHIFT_PAGE_LIMIT = 100
ARCTIC_SHIFT_MAX_PAGES = 20

EXTERNAL_REPLAYABLE_SOURCE_TYPES = {
    "rss",
    "reddit_subreddit",
    "reddit_subreddits_by_topic",
    "youtube_search",
    "youtube_channel",
    "youtube_channel_url",
    "youtube_channels_by_topic",
    "youtube_videos_by_topic",
}

NON_REPLAYABLE_SOURCE_TYPES = {
    "google_news_search",
    "nitter_user",
    "nitter_search",
    # Arctic Shift historical post search is subreddit-scoped, so global
    # reddit_search does not currently have a true external replay path.
    "reddit_search",
}


@dataclass
class ReplayResult:
    articles: list[dict[str, Any]]
    skipped_sources: list[SourceSpec]


async def replay_articles(
    sources: list[SourceSpec],
    start: datetime,
    end: datetime,
) -> ReplayResult:
    skipped: list[SourceSpec] = []
    all_articles: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    local_seen_ids: set[str] = set()
    local_seen_ids_by_source: dict[tuple[str, str], set[str]] = {}
    observations: list[dict[str, Any]] = []

    for source in sources:
        source_type = str(source["type"]).strip().lower()
        source_feed = str(source["feed"]).strip()
        source_key = (source_type, source_feed)

        local_articles = _replay_local(source, start, end)
        local_ids_for_source: set[str] = set()
        for article in local_articles:
            article_id = str(article.get("id", "")).strip()
            if article_id:
                local_seen_ids.add(article_id)
                local_ids_for_source.add(article_id)
        local_seen_ids_by_source[source_key] = local_ids_for_source

        external_articles: list[dict[str, Any]] = []
        if source_type in EXTERNAL_REPLAYABLE_SOURCE_TYPES:
            try:
                external_articles = await _replay_external(source, start, end)
            except Exception as exc:
                print(
                    "[replay] external fetch failed for "
                    f"{source_type} {source_feed}: {type(exc).__name__}: {exc!r}"
                )

        combined = local_articles + external_articles

        if not combined and source_type in NON_REPLAYABLE_SOURCE_TYPES:
            skipped.append(source)
            continue

        for article in combined:
            article_id = str(article.get("id", "")).strip()
            if not article_id or article_id in seen_ids:
                continue
            seen_ids.add(article_id)
            all_articles.append(article)

        for article in external_articles:
            article_id = str(article.get("id", "")).strip()
            url = str(article.get("url", "")).strip()
            if not article_id or not url:
                continue
            if article_id in local_ids_for_source:
                continue
            observations.append(
                {
                    "url": url,
                    "source_type": source_type,
                    "source_feed": source_feed,
                    "source_spec": dict(source),
                }
            )

    external_articles_to_write: list[dict[str, Any]] = []
    written_ids: set[str] = set()
    for article in all_articles:
        article_id = str(article.get("id", "")).strip()
        if not article_id or article_id in local_seen_ids or article_id in written_ids:
            continue
        written_ids.add(article_id)
        external_articles_to_write.append(article)

    if external_articles_to_write or observations:
        await asyncio.to_thread(
            _write_articles_sync,
            external_articles_to_write,
            observations,
        )

    all_articles.sort(key=_article_sort_key)
    return ReplayResult(articles=all_articles, skipped_sources=skipped)


def _replay_local(
    source: SourceSpec,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    try:
        return replay_stream_from_cache(
            str(source["feed"]).strip(),
            start.isoformat(),
            end.isoformat(),
        )
    except Exception:
        return []


async def _replay_external(
    source: SourceSpec,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    source_type = str(source["type"]).strip().lower()
    feed_value = str(source["feed"]).strip()

    if source_type == "rss":
        return await _replay_rss_wayback(feed_value, start, end)
    if source_type == "reddit_subreddit":
        return await _replay_reddit_arctic_shift(feed_value, None, start, end)
    if source_type == "reddit_search":
        return await _replay_reddit_arctic_shift(None, feed_value, start, end)
    if source_type == "reddit_subreddits_by_topic":
        subreddits = await search_subreddits_by_topic(feed_value)
        articles: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for subreddit in subreddits:
            replayed = await _replay_reddit_arctic_shift(
                str(subreddit["subreddit_name"]).strip(),
                None,
                start,
                end,
            )
            for article in replayed:
                article_id = str(article.get("id", "")).strip()
                if not article_id or article_id in seen_ids:
                    continue
                seen_ids.add(article_id)
                articles.append(article)
        return articles
    if source_type == "youtube_search":
        return await _replay_youtube_search(feed_value, start, end)
    if source_type in {"youtube_channel", "youtube_channel_url"}:
        return await _replay_youtube_channel(feed_value, start, end)
    if source_type == "youtube_channels_by_topic":
        channels = await search_channels_by_topic(feed_value)
        return await _replay_youtube_channels(channels, source_query=feed_value, start=start, end=end)
    if source_type == "youtube_videos_by_topic":
        videos = await search_videos_by_topic(feed_value)
        channels = [
            {
                "channel_id": str(video["channel_id"]).strip(),
                "channel_name": str(video["channel_name"]).strip(),
            }
            for video in videos
        ]
        return await _replay_youtube_channels(channels, source_query=feed_value, start=start, end=end)

    return []


async def _replay_rss_wayback(
    feed_url: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    params = {
        "url": feed_url,
        "output": "json",
        "fl": "timestamp,statuscode",
        "from": start.strftime("%Y%m%d%H%M%S"),
        "to": end.strftime("%Y%m%d%H%M%S"),
        "filter": "statuscode:200",
        "limit": str(WAYBACK_SNAPSHOT_LIMIT),
        "collapse": "digest",
    }

    async with httpx.AsyncClient(timeout=REPLAY_HTTP_TIMEOUT, follow_redirects=True) as client:
        try:
            cdx_response = await client.get(WAYBACK_CDX_URL, params=params)
            cdx_response.raise_for_status()
            payload = cdx_response.json()
        except Exception as exc:
            raise RuntimeError(f"Wayback CDX lookup failed for {feed_url}: {type(exc).__name__}: {exc!r}") from exc

        if not isinstance(payload, list) or len(payload) <= 1:
            return []

        snapshot_rows = payload[1:]
        articles: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        for row in snapshot_rows:
            if not isinstance(row, list) or not row:
                continue
            timestamp = str(row[0]).strip()
            if not timestamp:
                continue

            snapshot_url = f"https://web.archive.org/web/{timestamp}/{feed_url}"
            try:
                snapshot_response = await client.get(snapshot_url)
                snapshot_response.raise_for_status()
                snapshot_text = snapshot_response.text
            except Exception:
                await asyncio.sleep(WAYBACK_FETCH_DELAY_SECONDS)
                continue
            if "Wayback Machine" in snapshot_text[:500]:
                await asyncio.sleep(WAYBACK_FETCH_DELAY_SECONDS)
                continue

            try:
                parsed = await asyncio.to_thread(feedparser.parse, snapshot_text)
            except Exception:
                await asyncio.sleep(WAYBACK_FETCH_DELAY_SECONDS)
                continue
            for entry in list(getattr(parsed, "entries", []) or []):
                try:
                    article = _normalize_rss_entry(entry, feed_url, getattr(parsed, "feed", {}) or {})
                except Exception:
                    continue
                if article is None:
                    continue
                if not _article_within_range(article, start, end):
                    continue
                article_url = str(article.get("url", "")).strip()
                if not article_url or article_url in seen_urls:
                    continue
                seen_urls.add(article_url)
                articles.append(article)

            await asyncio.sleep(WAYBACK_FETCH_DELAY_SECONDS)

    return articles


def _normalize_rss_entry(
    entry: Any,
    feed_url: str,
    feed_meta: Any,
) -> dict[str, Any] | None:
    title = _clean_text(_get_mapping_value(entry, "title"))
    url = _clean_text(_extract_entry_url(entry))
    if not title or not url:
        return None

    content = _extract_rss_content(entry)
    if not content:
        return None

    published_at = _extract_rss_published_at(entry)
    source_title = _clean_text(_get_mapping_value(feed_meta, "title"))
    source_name = source_title or (_domain_from_url(feed_url) or feed_url)

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


async def _replay_reddit_arctic_shift(
    subreddit: str | None,
    query: str | None,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    start_ts = int(start.timestamp())
    before_ts = int(end.timestamp())

    async with httpx.AsyncClient(timeout=REPLAY_HTTP_TIMEOUT, follow_redirects=True) as client:
        for _ in range(ARCTIC_SHIFT_MAX_PAGES):
            items = await _fetch_arctic_shift_page(
                client=client,
                subreddit=subreddit,
                query=query,
                start_ts=start_ts,
                before_ts=before_ts,
            )
            if not items:
                break

            oldest_created_utc: int | None = None
            page_count = 0
            for post in items:
                if not isinstance(post, dict):
                    continue
                created_value = _to_int(post.get("created_utc"))
                if created_value is not None:
                    if oldest_created_utc is None or created_value < oldest_created_utc:
                        oldest_created_utc = created_value
                article = _normalize_arctic_shift_post(post)
                article_id = str(article.get("id", "")).strip()
                if not article_id or article_id in seen_ids:
                    continue
                seen_ids.add(article_id)
                articles.append(article)
                page_count += 1

            if len(items) < ARCTIC_SHIFT_PAGE_LIMIT:
                break
            if oldest_created_utc is None:
                break

            next_before_ts = oldest_created_utc - 1
            if next_before_ts <= start_ts or next_before_ts >= before_ts:
                break
            before_ts = next_before_ts

    return articles


async def _fetch_arctic_shift_page(
    *,
    client: httpx.AsyncClient,
    subreddit: str | None,
    query: str | None,
    start_ts: int,
    before_ts: int,
) -> list[Any]:
    params: dict[str, str] = {
        "after": str(start_ts),
        "before": str(before_ts),
        "limit": str(ARCTIC_SHIFT_PAGE_LIMIT),
    }
    if subreddit:
        params["subreddit"] = subreddit
    if query:
        params["q"] = _normalize_reddit_replay_query(query)

    response = await client.get(ARCTIC_SHIFT_URL, params=params)
    response.raise_for_status()
    payload = response.json()
    return _extract_arctic_shift_items(payload)


def _normalize_arctic_shift_post(post: dict[str, Any]) -> dict[str, Any]:
    permalink = str(post.get("permalink", "")).strip()
    if not permalink.startswith("/"):
        permalink = f"/{permalink.lstrip('/')}"
    url = f"https://reddit.com{permalink}"
    subreddit = str(post.get("subreddit", "")).strip()
    created_utc = post.get("created_utc")
    published_at = datetime.fromtimestamp(float(created_utc), tz=timezone.utc).isoformat()
    content = _clean_html_to_text(str(post.get("selftext", "") or ""))

    return {
        "id": hashlib.md5(url.encode("utf-8")).hexdigest(),
        "title": str(post.get("title", "")).strip(),
        "url": url,
        "published_at": published_at,
        "content": content,
        "full_text": content,
        "source_url": subreddit_feed_url(subreddit),
        "source_name": f"r/{subreddit}",
        "source_type": "reddit",
        "raw": dict(post),
    }


async def _replay_youtube_search(
    query: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=REPLAY_HTTP_TIMEOUT, follow_redirects=True) as client:
        payload = await _youtube_get(
            "/search",
            {
                "type": "video",
                "q": query,
                "part": "snippet",
                "publishedAfter": start.isoformat(),
                "publishedBefore": end.isoformat(),
                "maxResults": 50,
                "order": "date",
            },
            http_client=client,
        )

    articles = _youtube_search_payload_to_articles(payload, source_query=query)
    return await _enrich_articles_with_youtube_transcripts(articles)


async def _replay_youtube_channel(
    feed_value: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    channel = await get_channel_feed(feed_value)
    if channel is None:
        return []

    channel_id = str(channel["channel_id"]).strip()
    source_query = str(channel["channel_name"]).strip() or feed_value
    async with httpx.AsyncClient(timeout=REPLAY_HTTP_TIMEOUT, follow_redirects=True) as client:
        payload = await _youtube_get(
            "/search",
            {
                "type": "video",
                "part": "snippet",
                "channelId": channel_id,
                "order": "date",
                "publishedAfter": start.isoformat(),
                "publishedBefore": end.isoformat(),
                "maxResults": 50,
            },
            http_client=client,
        )

    articles = _youtube_search_payload_to_articles(payload, source_query=source_query)
    return await _enrich_articles_with_youtube_transcripts(articles)


async def _replay_youtube_channels(
    channels: list[dict[str, Any]],
    *,
    source_query: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    seen_channel_ids: set[str] = set()
    all_articles: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for channel in channels:
        channel_id = str(channel.get("channel_id", "")).strip()
        if not channel_id or channel_id in seen_channel_ids:
            continue
        seen_channel_ids.add(channel_id)
        articles = await _replay_youtube_channel(channel_id, start, end)
        for article in articles:
            article_id = str(article.get("id", "")).strip()
            if not article_id or article_id in seen_ids:
                continue
            seen_ids.add(article_id)
            if not str(article.get("source_url", "")).strip():
                article["source_url"] = f"https://www.youtube.com/results?search_query={source_query}"
            all_articles.append(article)
    return all_articles


def _youtube_search_payload_to_articles(
    payload: dict[str, Any],
    *,
    source_query: str,
) -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    for item in payload.get("items", []):
        if not isinstance(item, dict):
            continue

        video_id = _nested_str(item, "id", "videoId")
        snippet = item.get("snippet", {}) if isinstance(item.get("snippet"), dict) else {}
        published_at = _nested_str(item, "snippet", "publishedAt")
        if not video_id or not published_at:
            continue

        video = {
            "video_id": video_id,
            "video_title": unescape(str(snippet.get("title", "")).strip()),
            "published_at": published_at,
            "channel_id": str(snippet.get("channelId", "")).strip(),
            "channel_name": unescape(str(snippet.get("channelTitle", "")).strip()),
            "description": unescape(str(snippet.get("description", "")).strip()),
            "feed_url": "",
        }
        try:
            article = _youtube_video_to_article(video, source_query=source_query, full_text="")
        except Exception:
            continue
        articles.append(article)
    return articles


def _extract_arctic_shift_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "items", "posts", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested_key in ("children", "items", "posts", "results"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return nested
    return []


def _to_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except Exception:
        return None


def _normalize_reddit_replay_query(query: str) -> str:
    normalized = str(query).strip()
    if not normalized:
        return ""
    normalized = normalized.replace('"', " ").replace("'", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _article_sort_key(article: dict[str, Any]) -> tuple[str, str]:
    published_at = str(article.get("published_at", "")).strip()
    return (published_at, str(article.get("id", "")).strip())


def _article_within_range(article: dict[str, Any], start: datetime, end: datetime) -> bool:
    published_at = str(article.get("published_at", "")).strip()
    if not published_at:
        return False
    try:
        published_dt = _parse_datetime_text(published_at)
    except Exception:
        return False
    if published_dt is None:
        return False
    return start <= published_dt <= end


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


def _extract_rss_content(entry: Any) -> str:
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
        normalized = _clean_html_to_text(value)
        if normalized:
            return normalized
    return ""


def _extract_rss_published_at(entry: Any) -> str:
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

    raise ValueError("RSS entry is missing a usable published timestamp")


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


def _clean_html_to_text(value: str) -> str:
    if "<" not in value and ">" not in value:
        return _clean_text(unescape(value))
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


def _domain_from_url(url: str) -> str:
    return urlparse(url).netloc or url


def _nested_str(value: dict[str, Any], *path: str) -> str:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return str(current or "").strip()


__all__ = ["ReplayResult", "replay_articles"]
