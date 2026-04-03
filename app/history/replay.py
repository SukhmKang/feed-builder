import asyncio
import calendar
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from app.sources.reddit import search_subreddits_by_topic, subreddit_feed_url
from app.sources.runner import SourceSpec, _enrich_articles_with_youtube_transcripts, _youtube_video_to_article
from app.history.stream_history import _write_articles_sync, replay_stream_from_cache
from app.sources.youtube_scraper import (
    _youtube_get,
    get_channel_feed,
    search_channels_by_topic,
    search_videos_by_topic,
)

WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_FETCH_DELAY_SECONDS = 1.0
WAYBACK_SNAPSHOT_LIMIT = 10
CDX_HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
SNAPSHOT_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
REPLAY_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
ARCTIC_SHIFT_URL = "https://arctic-shift.photon-reddit.com/api/posts/search"
ARCTIC_SHIFT_PAGE_LIMIT = 100
ARCTIC_SHIFT_MAX_PAGES = 20
RSS_REPLAY_CONCURRENCY = 2
REDDIT_REPLAY_CONCURRENCY = 3
YOUTUBE_REPLAY_CONCURRENCY = 3

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
    "tavily",
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


@dataclass
class ReplaySourceOutcome:
    source: SourceSpec
    local_articles: list[dict[str, Any]]
    external_articles: list[dict[str, Any]]
    skipped: bool


async def replay_articles(
    sources: list[SourceSpec],
    start: datetime,
    end: datetime,
) -> ReplayResult:
    print(
        "[replay] start "
        f"sources={len(sources)} "
        f"start={start.isoformat()} "
        f"end={end.isoformat()}"
    )
    rss_semaphore = asyncio.Semaphore(RSS_REPLAY_CONCURRENCY)
    reddit_semaphore = asyncio.Semaphore(REDDIT_REPLAY_CONCURRENCY)
    youtube_semaphore = asyncio.Semaphore(YOUTUBE_REPLAY_CONCURRENCY)

    outcomes = await asyncio.gather(
        *[
            _replay_source(
                source,
                index=index,
                total=len(sources),
                start=start,
                end=end,
                rss_semaphore=rss_semaphore,
                reddit_semaphore=reddit_semaphore,
                youtube_semaphore=youtube_semaphore,
            )
            for index, source in enumerate(sources, start=1)
        ]
    )

    skipped: list[SourceSpec] = []
    all_articles: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    local_seen_ids: set[str] = set()
    observations: list[dict[str, Any]] = []

    for outcome in outcomes:
        source = outcome.source
        source_type = str(source["type"]).strip().lower()
        source_feed = str(source["feed"]).strip()
        local_ids_for_source: set[str] = set()

        for article in outcome.local_articles:
            article_id = str(article.get("id", "")).strip()
            if article_id:
                local_seen_ids.add(article_id)
                local_ids_for_source.add(article_id)

        if outcome.skipped:
            skipped.append(source)
            continue

        for article in outcome.local_articles + outcome.external_articles:
            article_id = str(article.get("id", "")).strip()
            if not article_id or article_id in seen_ids:
                continue
            seen_ids.add(article_id)
            all_articles.append(article)

        for article in outcome.external_articles:
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
        print(
            "[replay] writing cache "
            f"articles={len(external_articles_to_write)} observations={len(observations)}"
        )
        await asyncio.to_thread(
            _write_articles_sync,
            external_articles_to_write,
            observations,
        )

    all_articles.sort(key=_article_sort_key)
    print(
        "[replay] complete "
        f"articles={len(all_articles)} skipped_sources={len(skipped)}"
    )
    return ReplayResult(articles=all_articles, skipped_sources=skipped)


async def replay_from_saved_output(
    output_path: str | Path,
    start: datetime,
    end: datetime,
) -> ReplayResult:
    sources = load_sources_from_saved_output(output_path)
    return await replay_articles(sources, start, end)


def load_sources_from_saved_output(output_path: str | Path) -> list[SourceSpec]:
    payload = json.loads(Path(output_path).expanduser().read_text(encoding="utf-8"))
    return extract_replay_sources(payload)


def extract_replay_sources(payload: dict[str, Any]) -> list[SourceSpec]:
    candidates: Any = None
    if isinstance(payload.get("final_config"), dict):
        final_sources = payload["final_config"].get("sources")
        if isinstance(final_sources, list):
            candidates = final_sources
    if candidates is None and isinstance(payload.get("merged_sources"), list):
        candidates = payload.get("merged_sources")
    if candidates is None and isinstance(payload.get("sources"), list):
        candidates = payload.get("sources")
    if candidates is None:
        raise ValueError(
            "Saved output did not contain replayable sources. Expected final_config.sources, "
            "merged_sources, or sources."
        )

    sources: list[SourceSpec] = []
    for index, item in enumerate(candidates):
        if not isinstance(item, dict):
            raise ValueError(f"Source at index {index} is not an object")
        source_type = str(item.get("type", "")).strip()
        feed = str(item.get("feed", "")).strip()
        if not source_type or not feed:
            raise ValueError(f"Source at index {index} is missing type/feed")
        sources.append(_canonicalize_saved_source_spec(SourceSpec(type=source_type, feed=feed)))
    return sources


def _canonicalize_saved_source_spec(source: SourceSpec) -> SourceSpec:
    source_type = str(source["type"]).strip().lower()
    feed = str(source["feed"]).strip()

    if source_type == "rss":
        youtube_channel_id = _extract_youtube_channel_id_from_feed_url(feed)
        if youtube_channel_id:
            return SourceSpec(type="youtube_channel", feed=youtube_channel_id)

        reddit_subreddit = _extract_reddit_subreddit_name(feed)
        if reddit_subreddit:
            return SourceSpec(type="reddit_subreddit", feed=reddit_subreddit)

        reddit_query = _extract_reddit_search_query(feed)
        if reddit_query:
            return SourceSpec(type="reddit_search", feed=reddit_query)

    return SourceSpec(type=source_type, feed=feed)


def _extract_youtube_channel_id_from_feed_url(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.netloc.lower() not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        return None
    if parsed.path != "/feeds/videos.xml":
        return None
    for channel_id in parse_qs(parsed.query).get("channel_id", []):
        normalized = str(channel_id).strip()
        if normalized:
            return normalized
    return None


def _extract_reddit_subreddit_name(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or "reddit.com" not in parsed.netloc.lower():
        return None
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0].lower() == "r":
        candidate = unquote(path_parts[1]).strip()
        return candidate or None
    return None


def _extract_reddit_search_query(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or "reddit.com" not in parsed.netloc.lower():
        return None
    query_values = parse_qs(parsed.query).get("q", [])
    for query in query_values:
        normalized = unquote(str(query)).strip()
        if normalized:
            return normalized
    return None


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


async def _replay_source(
    source: SourceSpec,
    *,
    index: int,
    total: int,
    start: datetime,
    end: datetime,
    rss_semaphore: asyncio.Semaphore,
    reddit_semaphore: asyncio.Semaphore,
    youtube_semaphore: asyncio.Semaphore,
) -> ReplaySourceOutcome:
    source_type = str(source["type"]).strip().lower()
    source_feed = str(source["feed"]).strip()
    print(f"[replay] source {index}/{total} start {source_type} {source_feed}")

    local_articles = _replay_local(source, start, end)
    external_articles: list[dict[str, Any]] = []
    if source_type in EXTERNAL_REPLAYABLE_SOURCE_TYPES:
        semaphore = _select_replay_semaphore(
            source_type,
            rss_semaphore=rss_semaphore,
            reddit_semaphore=reddit_semaphore,
            youtube_semaphore=youtube_semaphore,
        )
        try:
            async with semaphore:
                external_articles = await _replay_external(source, start, end)
        except Exception as exc:
            print(
                "[replay] external fetch failed for "
                f"{source_type} {source_feed}: {type(exc).__name__}: {exc!r}"
            )

    combined_count = len(local_articles) + len(external_articles)
    skipped = combined_count == 0 and source_type in NON_REPLAYABLE_SOURCE_TYPES
    print(
        f"[replay] source {index}/{total} done "
        f"{source_type} {source_feed} "
        f"local={len(local_articles)} external={len(external_articles)} combined={combined_count}"
    )
    return ReplaySourceOutcome(
        source=source,
        local_articles=local_articles,
        external_articles=external_articles,
        skipped=skipped,
    )


def _select_replay_semaphore(
    source_type: str,
    *,
    rss_semaphore: asyncio.Semaphore,
    reddit_semaphore: asyncio.Semaphore,
    youtube_semaphore: asyncio.Semaphore,
) -> asyncio.Semaphore:
    if source_type == "rss":
        return rss_semaphore
    if source_type.startswith("reddit_"):
        return reddit_semaphore
    if source_type.startswith("youtube_"):
        return youtube_semaphore
    return rss_semaphore


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

    async with httpx.AsyncClient(timeout=CDX_HTTP_TIMEOUT, follow_redirects=True) as client:
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
                client.timeout = SNAPSHOT_HTTP_TIMEOUT
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
                except Exception as exc:
                    print(
                        "[replay] rss entry normalization failed for "
                        f"{feed_url} at {timestamp}: {type(exc).__name__}: {exc}"
                    )
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


def _parse_cli_datetime(value: str) -> datetime:
    parsed = _parse_datetime(value)
    if parsed is None:
        raise ValueError(f"Invalid datetime value: {value}")
    return parsed


async def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Replay historical articles from a saved run output")
    parser.add_argument("input", help="Path to output.json, sources.json, or a final_config-like JSON file")
    parser.add_argument("--start", required=True, help="ISO datetime, e.g. 2026-03-01T00:00:00Z")
    parser.add_argument("--end", required=True, help="ISO datetime, e.g. 2026-03-31T23:59:59Z")
    parser.add_argument("--limit", type=int, default=10, help="How many articles to include in the printed preview")
    args = parser.parse_args()

    result = await replay_from_saved_output(
        args.input,
        _parse_cli_datetime(args.start),
        _parse_cli_datetime(args.end),
    )
    preview = [
        {
            "id": article.get("id"),
            "title": article.get("title"),
            "url": article.get("url"),
            "published_at": article.get("published_at"),
            "source_name": article.get("source_name"),
            "source_type": article.get("source_type"),
        }
        for article in result.articles[: args.limit]
    ]
    print(
        json.dumps(
            {
                "input": str(Path(args.input).expanduser()),
                "article_count": len(result.articles),
                "skipped_sources": result.skipped_sources,
                "articles": preview,
            },
            indent=2,
            ensure_ascii=True,
        )
    )


__all__ = [
    "ReplayResult",
    "extract_replay_sources",
    "load_sources_from_saved_output",
    "replay_articles",
    "replay_from_saved_output",
]


if __name__ == "__main__":
    asyncio.run(_main())
