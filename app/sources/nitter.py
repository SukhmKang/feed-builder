import base64
import asyncio
import hashlib
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

VISION_API_URL = "https://vision.googleapis.com/v1/images:annotate"

NITTER_BASE = os.environ.get("NITTER_BASE", "https://nitter.sknitterinstance.com")
DC_NS = "http://purl.org/dc/elements/1.1/"
NITTER_MAX_CONCURRENT_REQUESTS = 1
NITTER_MIN_INTERVAL_SECONDS = 1.0
NITTER_RETRY_STATUS_CODES = {429}
NITTER_MAX_RETRIES = 3
NITTER_RETRY_BACKOFF_SECONDS = 2.0

MediaType = Literal["image", "video", "gif"]

_NITTER_REQUEST_SEMAPHORE = asyncio.Semaphore(NITTER_MAX_CONCURRENT_REQUESTS)
_NITTER_PACING_LOCK = asyncio.Lock()
_NITTER_LAST_REQUEST_STARTED_AT = 0.0


@dataclass
class NitterMedia:
    content_type: MediaType       # "image", "video", or "gif"
    thumbnail_url: str            # small preview
    content_url: str | None = None  # full-res for images, HLS .m3u8 for video, MP4 for gif
    duration: str | None = None   # e.g. "2:25:58", videos only
    media_text: str | None = None  # OCR text extracted from image, None for video/gif


@dataclass
class NitterQuoteTweet:
    username: str
    display_name: str
    text: str
    url: str
    media: list[NitterMedia] = field(default_factory=list)


@dataclass
class NitterStats:
    replies: int = 0
    retweets: int = 0
    likes: int = 0
    views: int = 0


@dataclass
class NitterReply:
    url: str
    username: str
    display_name: str
    published: str | None
    text: str
    media: list[NitterMedia] = field(default_factory=list)
    stats: NitterStats = field(default_factory=NitterStats)


@dataclass
class NitterItem:
    title: str
    url: str
    username: str
    published: str | None
    text: str
    media: list[NitterMedia] = field(default_factory=list)
    quote_tweet: NitterQuoteTweet | None = None

    def to_article(self, *, feed_url: str) -> dict:
        """Convert this Nitter item into the normalized article shape used by the pipeline."""
        title = self.title.strip() or _derive_title(self.text)
        published_at = _normalize_published_at(self.published)
        return {
            "id": hashlib.md5(self.url.encode("utf-8")).hexdigest(),
            "title": title,
            "url": self.url,
            "published_at": published_at,
            "content": self.text.strip(),
            "full_text": self.text.strip(),
            "source_url": feed_url,
            "source_name": self.username,
            "source_type": "nitter",
            "raw": _to_plain_data(self),
        }


@dataclass
class NitterFeed:
    items: list[NitterItem]
    feed_url: str

    def to_articles(self) -> list[dict]:
        """Convert all items in the feed into normalized article dicts."""
        return [item.to_article(feed_url=self.feed_url) for item in self.items]


class NitterFetchError(RuntimeError):
    """Raised when a Nitter source cannot be fetched or normalized safely."""


def user_feed_url(username: str) -> str:
    return f"{NITTER_BASE}/{username}/rss"


def search_feed_url(query: str) -> str:
    return f"{NITTER_BASE}/search/rss?q={quote(query)}"


def _fix_url(url: str) -> str:
    if url.startswith("/"):
        return f"{NITTER_BASE}{url}"
    return re.sub(r"^http://localhost(?=/)", NITTER_BASE, url)


async def ocr_image(image_url: str, http_client: httpx.AsyncClient | None = None) -> str | None:
    """Extract text from an image URL using Google Cloud Vision OCR."""
    api_key = os.environ.get("GOOGLE_VISION_API_KEY")
    if not api_key:
        return None

    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(follow_redirects=True, timeout=15)

    try:
        img_response = await http_client.get(image_url)
        img_response.raise_for_status()
        image_b64 = base64.b64encode(img_response.content).decode("utf-8")

        result = await http_client.post(
            VISION_API_URL,
            params={"key": api_key},
            json={
                "requests": [{
                    "image": {"content": image_b64},
                    "features": [{"type": "TEXT_DETECTION"}],
                }]
            },
        )
        result.raise_for_status()
        responses = result.json().get("responses", [])
        annotations = responses[0].get("textAnnotations", []) if responses else []
        return annotations[0]["description"].strip() if annotations else None
    except Exception:
        return None
    finally:
        if owns_client:
            await http_client.aclose()


def _extract_media(soup_el) -> list[NitterMedia]:
    media = []
    for a in soup_el.find_all("a", href=True):
        img = a.find("img")
        if img and img.get("src"):
            is_video = "Video" in a.get_text()
            media.append(NitterMedia(
                content_type="video" if is_video else "image",
                thumbnail_url=_fix_url(img["src"]),
            ))
    for img in soup_el.find_all("img"):
        if img.parent.name != "a" and img.get("src"):
            media.append(NitterMedia(content_type="image", thumbnail_url=_fix_url(img["src"])))
    return media


def _extract_text(soup_el) -> str:
    for bq in soup_el.find_all("blockquote"):
        bq.decompose()
    for hr in soup_el.find_all("hr"):
        hr.decompose()
    for a in soup_el.find_all("a", href=True):
        if "nitter.net" in a.get("href", ""):
            a.decompose()
    return soup_el.get_text(separator="\n").strip()


def _parse_description(html: str) -> tuple[str, list[NitterMedia], NitterQuoteTweet | None]:
    soup = BeautifulSoup(html, "html.parser")

    quote_tweet = None
    blockquote = soup.find("blockquote")
    if blockquote:
        author_tag = blockquote.find("b")
        author_text = author_tag.get_text(strip=True) if author_tag else ""
        match = re.match(r"^(.*?)\s*\(@(\w+)\)$", author_text)
        display_name = match.group(1).strip() if match else author_text
        username = match.group(2) if match else ""

        footer = blockquote.find("footer")
        quote_url = ""
        if footer:
            link = footer.find("a", href=True)
            if link:
                quote_url = _fix_url(link["href"])
            footer.decompose()

        quote_media = _extract_media(blockquote)

        if author_tag:
            author_tag.decompose()
        for a in blockquote.find_all("a"):
            a.decompose()
        quote_text = blockquote.get_text(separator="\n").strip()

        if display_name or quote_text:
            quote_tweet = NitterQuoteTweet(
                username=username,
                display_name=display_name,
                text=quote_text,
                url=quote_url,
                media=quote_media,
            )
        blockquote.decompose()

    media = _extract_media(soup)
    text = _extract_text(soup)

    return text, media, quote_tweet


def _parse_rss(xml_text: str, feed_url: str) -> NitterFeed:
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return NitterFeed(items=[], feed_url=feed_url)

    items = []
    for item in channel.findall("item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        pub_date = item.findtext("pubDate")
        description = item.findtext("description") or ""
        creator = item.findtext(f"{{{DC_NS}}}creator") or ""
        username = creator.lstrip("@")

        text, media, quote_tweet = _parse_description(description)
        items.append(NitterItem(
            title=title,
            url=_fix_url(link),
            username=username,
            published=pub_date,
            text=text,
            media=media,
            quote_tweet=quote_tweet,
        ))

    return NitterFeed(items=items, feed_url=feed_url)


async def fetch_user_feed(username: str, http_client: httpx.AsyncClient | None = None) -> NitterFeed:
    url = user_feed_url(username)
    return await _fetch_feed(url, http_client, require_items=True)


async def fetch_search_feed(query: str, http_client: httpx.AsyncClient | None = None) -> NitterFeed:
    url = search_feed_url(query)
    return await _fetch_feed(url, http_client, require_items=False)


async def fetch_tweet_media(tweet_url: str, http_client: httpx.AsyncClient | None = None) -> list[NitterMedia]:
    """Fetch full media (including HLS video URLs) for a single tweet by scraping the tweet page."""
    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(follow_redirects=True, timeout=10)

    path = tweet_url.replace(NITTER_BASE, "").split("#")[0]

    try:
        await _nitter_request(http_client, "POST", f"{NITTER_BASE}/enablehls", data={"referer": path + "#m"})

        response = await _nitter_request(http_client, "GET", f"{NITTER_BASE}{path}")
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        media = []

        for gallery in soup.find_all(class_="gallery-video"):
            video_el = gallery.find("video")
            img_el = gallery.find("img")
            duration_el = gallery.find(class_="overlay-duration")

            poster = video_el.get("poster", "") if video_el else ""
            thumbnail = _fix_url(poster) if poster else (_fix_url(img_el["src"]) if img_el and img_el.get("src") else "")
            raw_data_url = video_el.get("data-url", "") if video_el else ""
            content_url = f"{NITTER_BASE}{raw_data_url}" if raw_data_url else None
            duration = duration_el.get_text(strip=True) if duration_el else None

            media.append(NitterMedia(
                content_type="video",
                thumbnail_url=thumbnail,
                content_url=content_url,
                duration=duration,
            ))

        for gallery in soup.find_all(class_="gallery-row"):
            a_el = gallery.find("a", class_="still-image")
            img_el = gallery.find("img")
            if img_el and img_el.get("src"):
                full_url = _fix_url(a_el["href"]) if a_el and a_el.get("href") else _fix_url(img_el["src"])
                media.append(NitterMedia(
                    content_type="image",
                    thumbnail_url=_fix_url(img_el["src"]),
                    content_url=full_url,
                ))

        for gif_el in soup.find_all(class_="media-gif"):
            video = gif_el.find("video")
            if video:
                source = video.find("source")
                poster = video.get("poster", "")
                content_url = _fix_url(source["src"]) if source and source.get("src") else None
                media.append(NitterMedia(
                    content_type="gif",
                    thumbnail_url=_fix_url(poster) if poster else "",
                    content_url=content_url,
                ))

        return media
    finally:
        if owns_client:
            await http_client.aclose()


def _parse_stat(text: str) -> int:
    text = text.strip().replace(",", "")
    if not text:
        return 0
    try:
        if text.endswith("K"):
            return int(float(text[:-1]) * 1000)
        if text.endswith("M"):
            return int(float(text[:-1]) * 1_000_000)
        return int(text)
    except ValueError:
        return 0


def _parse_reply_media(attachment_el) -> list[NitterMedia]:
    media = []
    for video in attachment_el.find_all("video"):
        source = video.find("source")
        poster = video.get("poster", "")
        content_url = _fix_url(source["src"]) if source and source.get("src") else None
        is_gif = "gif" in (video.get("class") or [])
        media.append(NitterMedia(
            content_type="gif" if is_gif else "video",
            thumbnail_url=_fix_url(poster) if poster else "",
            content_url=content_url,
        ))
    for img in attachment_el.find_all("img"):
        if img.get("src") and "profile_images" not in img["src"]:
            media.append(NitterMedia(content_type="image", thumbnail_url=_fix_url(img["src"])))
    return media


async def fetch_tweet_replies(tweet_url: str, http_client: httpx.AsyncClient | None = None) -> list[NitterReply]:
    """Scrape replies from a tweet's conversation page."""
    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(follow_redirects=True, timeout=10)

    path = tweet_url.replace(NITTER_BASE, "").split("#")[0]

    try:
        response = await _nitter_request(http_client, "GET", f"{NITTER_BASE}{path}")
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        replies = []
        for reply_el in soup.find_all(class_="reply"):
            item_el = reply_el.find(class_="timeline-item")
            if not item_el:
                continue

            link_el = item_el.find(class_="tweet-link")
            url = _fix_url(link_el["href"]) if link_el and link_el.get("href") else ""

            fullname_el = item_el.find(class_="fullname")
            username_el = item_el.find(class_="username")
            display_name = fullname_el.get_text(strip=True) if fullname_el else ""
            username = username_el.get_text(strip=True).lstrip("@") if username_el else ""

            date_el = item_el.find(class_="tweet-date")
            published = date_el.find("a")["title"] if date_el and date_el.find("a") else None

            content_el = item_el.find(class_="tweet-content")
            text = content_el.get_text(separator="\n").strip() if content_el else ""

            attachments_el = item_el.find(class_="attachments")
            media = _parse_reply_media(attachments_el) if attachments_el else []

            stat_els = item_el.find_all(class_="tweet-stat")
            stat_values = [_parse_stat(el.get_text()) for el in stat_els]
            while len(stat_values) < 4:
                stat_values.append(0)
            stats = NitterStats(
                replies=stat_values[0],
                retweets=stat_values[1],
                likes=stat_values[2],
                views=stat_values[3],
            )

            replies.append(NitterReply(
                url=url,
                username=username,
                display_name=display_name,
                published=published,
                text=text,
                media=media,
                stats=stats,
            ))

        return replies
    finally:
        if owns_client:
            await http_client.aclose()


async def _fetch_feed(
    url: str,
    http_client: httpx.AsyncClient | None = None,
    require_items: bool = True,
) -> NitterFeed:
    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(follow_redirects=True, timeout=10)

    try:
        response = await _nitter_request(http_client, "GET", url)
        response.raise_for_status()
        feed = _parse_rss(response.text, feed_url=url)
        if require_items and not feed.items:
            raise NitterFetchError(f"Nitter feed {url} did not contain any parseable items")
        return feed
    finally:
        if owns_client:
            await http_client.aclose()


async def _nitter_request(
    http_client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs,
) -> httpx.Response:
    async with _NITTER_REQUEST_SEMAPHORE:
        for attempt in range(1, NITTER_MAX_RETRIES + 1):
            await _wait_for_nitter_request_slot()
            response = await http_client.request(method, url, **kwargs)
            if response.status_code not in NITTER_RETRY_STATUS_CODES:
                return response

            if attempt >= NITTER_MAX_RETRIES:
                return response

            retry_after = _parse_retry_after_seconds(response)
            backoff = retry_after if retry_after is not None else NITTER_RETRY_BACKOFF_SECONDS * attempt
            await asyncio.sleep(backoff)

        raise RuntimeError(f"Nitter request retry loop exhausted for {method} {url}")


async def _wait_for_nitter_request_slot() -> None:
    global _NITTER_LAST_REQUEST_STARTED_AT

    async with _NITTER_PACING_LOCK:
        now = time.monotonic()
        wait_seconds = (_NITTER_LAST_REQUEST_STARTED_AT + NITTER_MIN_INTERVAL_SECONDS) - now
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        _NITTER_LAST_REQUEST_STARTED_AT = time.monotonic()


def _parse_retry_after_seconds(response: httpx.Response) -> float | None:
    raw_value = str(response.headers.get("Retry-After", "")).strip()
    if not raw_value:
        return None
    try:
        return max(float(raw_value), 0.0)
    except ValueError:
        return None


def _derive_title(text: str, limit: int = 120) -> str:
    collapsed = " ".join(text.split()).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def _normalize_published_at(value: str | None) -> str:
    if not value:
        raise ValueError("Nitter item is missing a usable published timestamp")

    try:
        parsed = parsedate_to_datetime(value)
    except Exception:
        parsed = None

    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat()

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Could not parse Nitter timestamp value: {value}")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat()


def _to_plain_data(value):
    if isinstance(value, dict):
        return {str(key): _to_plain_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_plain_data(item) for item in value]
    if isinstance(value, tuple):
        return [_to_plain_data(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {
            key: _to_plain_data(getattr(value, key))
            for key in value.__dataclass_fields__.keys()
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "tm_year") and hasattr(value, "tm_mon"):
        return list(value)
    return str(value)
