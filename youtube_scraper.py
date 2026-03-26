"""
Deterministic YouTube feed discovery helpers.

Exports:
- `search_channels_by_topic(topic) -> list[Channel]`
- `search_videos_direct_by_topic(topic) -> list[Video]`
- `search_videos_by_topic(topic) -> list[Video]`
- `get_channel_id(channel_name) -> str | None`
- `get_channel_feed(channel_id_or_url) -> Channel | None`
- `get_channel_from_video(video_id) -> Channel | None`
- `get_video_transcript(video_id) -> VideoTranscript | None`
- `verify_feed(feed_url) -> bool`

Behavior:
- Uses YouTube Data API v3 for channel/video discovery.
- Verifies every returned channel feed before including it.
- `search_videos_direct_by_topic` returns raw top video matches without feed verification.
- Resolves channel IDs from raw `UC...` ids or YouTube URLs.
- Falls back to page parsing for channel URLs before using API name search.
- Uses `youtube_transcript_api` for transcript fetching without an API key.
- Prefers English, then any available transcript.
- Returns transcript metadata including language and whether the transcript is auto-generated.
- Raises clear runtime errors for missing API key or malformed API responses.
"""

import asyncio
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, TypedDict
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from dotenv import load_dotenv
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import WebshareProxyConfig

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_FEED_BASE = "https://www.youtube.com/feeds/videos.xml?channel_id="
YOUTUBE_TIMEOUT = 20.0
YOUTUBE_SEARCH_LIMIT = 10

load_dotenv()


class Channel(TypedDict):
    """Normalized YouTube channel search result."""

    channel_id: str
    channel_name: str
    description: str
    subscriber_count: int | None
    feed_url: str


class Video(TypedDict):
    """Normalized YouTube video search result with parent channel feed info."""

    video_id: str
    video_title: str
    published_at: str
    channel_id: str
    channel_name: str
    description: str
    feed_url: str


class TranscriptSegment(TypedDict):
    """One transcript segment with timing metadata."""

    start: float
    duration: float
    text: str


class VideoTranscript(TypedDict):
    """Normalized YouTube transcript payload."""

    video_id: str
    language: str
    is_generated: bool
    text: str
    segments: list[TranscriptSegment]


async def search_channels_by_topic(topic: str) -> list[Channel]:
    """Search YouTube channels by topic and return verified channel feed records."""
    normalized_topic = topic.strip()
    if not normalized_topic:
        return []

    async with _build_http_client() as http_client:
        payload = await _youtube_get(
            "/search",
            {
                "part": "snippet",
                "type": "channel",
                "q": normalized_topic,
                "maxResults": YOUTUBE_SEARCH_LIMIT,
            },
            http_client=http_client,
        )

        channel_ids = _dedupe_strings(
            _extract_nested_str(item, "snippet", "channelId") or _extract_nested_str(item, "id", "channelId")
            for item in payload.get("items", [])
        )
        return await _fetch_verified_channels(channel_ids, http_client=http_client)


async def search_videos_by_topic(topic: str) -> list[Video]:
    """Search YouTube videos by topic and return results with verified parent channel feeds."""
    normalized_topic = topic.strip()
    if not normalized_topic:
        return []

    async with _build_http_client() as http_client:
        videos = await _search_videos_direct_by_topic(normalized_topic, http_client=http_client)
        channel_ids = _dedupe_strings(video["channel_id"] for video in videos)
        channels_by_id = await _fetch_channel_map(channel_ids, http_client=http_client)

        results: list[Video] = []
        for video in videos:
            channel = channels_by_id.get(video["channel_id"])
            if channel is None:
                continue
            results.append(
                Video(
                    video_id=video["video_id"],
                    video_title=video["video_title"],
                    published_at=video["published_at"],
                    channel_id=channel["channel_id"],
                    channel_name=channel["channel_name"],
                    description=video["description"],
                    feed_url=channel["feed_url"],
                )
            )

        return results


async def search_videos_direct_by_topic(topic: str) -> list[Video]:
    """Search YouTube videos by topic and return raw results in API relevance order."""
    normalized_topic = topic.strip()
    if not normalized_topic:
        return []

    async with _build_http_client() as http_client:
        return await _search_videos_direct_by_topic(normalized_topic, http_client=http_client)


async def get_channel_feed(channel_id_or_url: str) -> Channel | None:
    """Resolve a channel id or URL into a verified channel feed record."""
    value = channel_id_or_url.strip()
    if not value:
        return None

    async with _build_http_client() as http_client:
        channel_id = None
        if value.startswith("UC"):
            channel_id = value
        elif _looks_like_youtube_url(value):
            channel_id = await _resolve_channel_id_from_url(value, http_client=http_client)
        else:
            candidates = await _search_channels_by_name(value, http_client=http_client)
            return candidates[0] if candidates else None

        if not channel_id:
            return None

        channels = await _fetch_verified_channels([channel_id], http_client=http_client)
        return channels[0] if channels else None


async def get_channel_id(channel_name: str) -> str | None:
    """Resolve a channel-like name to the best matching verified YouTube channel id."""
    normalized_name = channel_name.strip()
    if not normalized_name:
        return None

    channels = await search_channels_by_topic(normalized_name)
    if not channels:
        return None
    return channels[0]["channel_id"]


async def get_channel_from_video(video_id: str) -> Channel | None:
    """Resolve a YouTube video id to its parent verified channel record."""
    normalized_video_id = video_id.strip()
    if not normalized_video_id:
        return None

    async with _build_http_client() as http_client:
        payload = await _youtube_get(
            "/videos",
            {
                "part": "snippet",
                "id": normalized_video_id,
            },
            http_client=http_client,
        )
        items = payload.get("items", [])
        if not items:
            return None

        channel_id = _extract_nested_str(items[0], "snippet", "channelId")
        if not channel_id:
            return None

        channels = await _fetch_verified_channels([channel_id], http_client=http_client)
        return channels[0] if channels else None


async def get_video_transcript(video_id: str) -> VideoTranscript | None:
    """Fetch a YouTube transcript, preferring Japanese, then English, then any available language."""
    normalized_video_id = video_id.strip()
    if not normalized_video_id:
        return None

    try:
        transcript = await asyncio.to_thread(_select_transcript, normalized_video_id)
    except Exception:
        return None

    if transcript is None:
        return None

    try:
        fetched_segments = await asyncio.to_thread(transcript.fetch)
    except Exception:
        return None

    segments = _normalize_transcript_segments(fetched_segments)
    if not segments:
        return None

    return VideoTranscript(
        video_id=normalized_video_id,
        language=str(getattr(transcript, "language_code", "") or "").strip(),
        is_generated=bool(getattr(transcript, "is_generated", False)),
        text="\n".join(segment["text"] for segment in segments).strip(),
        segments=segments,
    )


async def verify_feed(feed_url: str) -> bool:
    """Return `True` when a YouTube feed URL returns XML with at least one entry."""
    normalized_feed_url = feed_url.strip()
    if not normalized_feed_url:
        return False

    async with _build_http_client() as http_client:
        return await _verify_feed(feed_url=normalized_feed_url, http_client=http_client)


async def _fetch_verified_channels(
    channel_ids: list[str],
    *,
    http_client: httpx.AsyncClient,
) -> list[Channel]:
    channels_by_id = await _fetch_channel_map(channel_ids, http_client=http_client)
    return [channels_by_id[channel_id] for channel_id in channel_ids if channel_id in channels_by_id]


async def _search_videos_direct_by_topic(topic: str, *, http_client: httpx.AsyncClient) -> list[Video]:
    payload = await _youtube_get(
        "/search",
        {
            "part": "snippet",
            "type": "video",
            "q": topic,
            "maxResults": YOUTUBE_SEARCH_LIMIT,
        },
        http_client=http_client,
    )

    results: list[Video] = []
    for item in payload.get("items", []):
        video_id = _extract_nested_str(item, "id", "videoId")
        video_title = _extract_nested_str(item, "snippet", "title")
        published_at = _extract_nested_str(item, "snippet", "publishedAt")
        channel_id = _extract_nested_str(item, "snippet", "channelId")
        channel_name = _extract_nested_str(item, "snippet", "channelTitle")
        if not video_id or not video_title or not published_at or not channel_id or not channel_name:
            continue

        results.append(
            Video(
                video_id=video_id,
                video_title=video_title,
                published_at=published_at,
                channel_id=channel_id,
                channel_name=channel_name,
                description=_extract_nested_str(item, "snippet", "description") or "",
                feed_url=_channel_feed_url(channel_id),
            )
        )

    return results


async def _fetch_channel_map(
    channel_ids: list[str],
    *,
    http_client: httpx.AsyncClient,
) -> dict[str, Channel]:
    unique_ids = _dedupe_strings(channel_ids)
    if not unique_ids:
        return {}

    payload = await _youtube_get(
        "/channels",
        {
            "part": "snippet,statistics",
            "id": ",".join(unique_ids),
        },
        http_client=http_client,
    )

    candidates: list[Channel] = []
    for item in payload.get("items", []):
        channel_id = _extract_nested_str(item, "id")
        channel_name = _extract_nested_str(item, "snippet", "title")
        if not channel_id or not channel_name:
            continue

        feed_url = _channel_feed_url(channel_id)
        candidates.append(
            Channel(
                channel_id=channel_id,
                channel_name=channel_name,
                description=_extract_nested_str(item, "snippet", "description") or "",
                subscriber_count=_parse_optional_int(_extract_nested_str(item, "statistics", "subscriberCount")),
                feed_url=feed_url,
            )
        )

    verification_results = await asyncio.gather(
        *[_verify_feed(channel["feed_url"], http_client=http_client) for channel in candidates],
        return_exceptions=True,
    )

    verified_channels: dict[str, Channel] = {}
    for channel, is_valid in zip(candidates, verification_results, strict=False):
        if is_valid is True:
            verified_channels[channel["channel_id"]] = channel

    return verified_channels


async def _resolve_channel_id_from_url(
    url: str,
    *,
    http_client: httpx.AsyncClient,
) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    channel_id = _extract_channel_id_from_url_path(url)
    if channel_id:
        return channel_id

    if "channel_id" in query:
        candidate = (query.get("channel_id") or [""])[0].strip()
        if _is_channel_id(candidate):
            return candidate

    html = await _fetch_html(url, http_client=http_client)
    if html:
        meta_match = re.search(
            r'<meta[^>]+itemprop=["\']channelId["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            flags=re.IGNORECASE,
        )
        if meta_match and _is_channel_id(meta_match.group(1)):
            return meta_match.group(1)

        external_id_match = re.search(r'"externalId"\s*:\s*"(UC[^"]+)"', html)
        if external_id_match and _is_channel_id(external_id_match.group(1)):
            return external_id_match.group(1)

    fallback_query = _channel_name_query_from_url(url)
    if not fallback_query:
        return None

    channels = await _search_channels_by_name(fallback_query, http_client=http_client)
    if not channels:
        return None
    return channels[0]["channel_id"]


async def _search_channels_by_name(
    query: str,
    *,
    http_client: httpx.AsyncClient,
) -> list[Channel]:
    payload = await _youtube_get(
        "/search",
        {
            "part": "snippet",
            "type": "channel",
            "q": query,
            "maxResults": YOUTUBE_SEARCH_LIMIT,
        },
        http_client=http_client,
    )
    channel_ids = _dedupe_strings(
        _extract_nested_str(item, "id", "channelId") for item in payload.get("items", [])
    )
    return await _fetch_verified_channels(channel_ids, http_client=http_client)


async def _youtube_get(
    path: str,
    params: dict[str, str | int],
    *,
    http_client: httpx.AsyncClient,
) -> dict[str, Any]:
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        raise RuntimeError("YOUTUBE_API_KEY is not configured")

    response = await http_client.get(
        f"{YOUTUBE_API_BASE}{path}",
        params={**params, "key": api_key},
    )
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"YouTube API returned an unexpected payload for {path}")
    return payload


async def _fetch_html(url: str, *, http_client: httpx.AsyncClient) -> str:
    try:
        response = await http_client.get(url)
        response.raise_for_status()
    except Exception:
        return ""
    return response.text


async def _verify_feed(feed_url: str, *, http_client: httpx.AsyncClient) -> bool:
    try:
        response = await http_client.get(feed_url)
        response.raise_for_status()
    except Exception:
        return False

    return _xml_contains_entries(response.text)


def _select_transcript(video_id: str) -> Any | None:
    transcript_list = _build_transcript_api().list(video_id)

    for languages in (["en"]):
        try:
            return transcript_list.find_transcript(languages)
        except Exception:
            continue

    for transcript in transcript_list:
        return transcript

    return None


def _build_transcript_api() -> YouTubeTranscriptApi:
    proxy_username = str(os.getenv("PROXY_USERNAME", "")).strip()
    proxy_password = str(os.getenv("PROXY_PASSWORD", "")).strip()

    if proxy_username and proxy_password:
        return YouTubeTranscriptApi(
            proxy_config=WebshareProxyConfig(
                proxy_username=proxy_username,
                proxy_password=proxy_password,
            )
        )

    return YouTubeTranscriptApi()


def _normalize_transcript_segments(items: Any) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    for item in items or []:
        if isinstance(item, dict):
            start_value = item.get("start")
            duration_value = item.get("duration")
            text_value = item.get("text", "")
        else:
            start_value = getattr(item, "start", None)
            duration_value = getattr(item, "duration", None)
            text_value = getattr(item, "text", "")

        start = _to_float(start_value)
        duration = _to_float(duration_value)
        text = str(text_value or "").strip()
        if start is None or duration is None or not text:
            continue
        segments.append(
            TranscriptSegment(
                start=start,
                duration=duration,
                text=text,
            )
        )
    return segments


def _channel_feed_url(channel_id: str) -> str:
    return f"{YOUTUBE_FEED_BASE}{channel_id}"


def _extract_channel_id_from_url_path(url: str) -> str | None:
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0].lower() == "channel" and _is_channel_id(path_parts[1]):
        return path_parts[1]
    return None


def _channel_name_query_from_url(url: str) -> str:
    parsed = urlparse(url)
    path_parts = [unquote(part).strip() for part in parsed.path.split("/") if part]
    if not path_parts:
        return ""

    if path_parts[0].startswith("@"):
        return path_parts[0][1:]
    if path_parts[0].lower() in {"c", "user", "channel"} and len(path_parts) >= 2:
        return path_parts[1]
    return path_parts[-1]


def _looks_like_youtube_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and "youtube.com" in parsed.netloc.lower()


def _xml_contains_entries(xml_text: str) -> bool:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return False

    for element in root.iter():
        if element.tag.endswith("entry") or element.tag.endswith("item"):
            return True
    return False


def _extract_nested_str(payload: Any, *keys: str) -> str | None:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if current is None:
        return None
    value = str(current).strip()
    return value or None


def _parse_optional_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dedupe_strings(values: Any) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        results.append(normalized)
    return results


def _is_channel_id(value: str) -> bool:
    return bool(re.fullmatch(r"UC[0-9A-Za-z_-]{10,}", value.strip()))


def _build_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=YOUTUBE_TIMEOUT,
        headers={
            "accept": "application/json, application/xml, text/xml, */*",
            "user-agent": "feed-builder/1.0",
        },
    )


__all__ = [
    "Channel",
    "Video",
    "TranscriptSegment",
    "VideoTranscript",
    "get_channel_id",
    "get_channel_feed",
    "get_channel_from_video",
    "get_video_transcript",
    "search_channels_by_topic",
    "search_videos_direct_by_topic",
    "search_videos_by_topic",
    "verify_feed",
]
