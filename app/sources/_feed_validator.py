import asyncio
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.sources._utils import _dedupe_urls, _extract_domain

FEED_CONTENT_TYPES = {
    "application/rss+xml",
    "application/atom+xml",
    "application/feed+json",
    "application/xml",
    "text/xml",
}

COMMON_FEED_PATHS = [
    "/feed",
    "/rss",
    "/rss.xml",
    "/feed.xml",
    "/atom.xml",
    "/index.xml",
    "/feeds/posts/default",
]

FEED_REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
FEED_REQUEST_DEADLINE_SECONDS = 12.0

_FEED_BLOCKED_DOMAINS = {
    "news.google.com",
    "feedburner.com",
}


@dataclass
class FeedValidationResult:
    url: str
    is_feed: bool
    final_url: str | None = None
    feed_format: str | None = None
    content_type: str | None = None
    reason: str | None = None


@dataclass
class DiscoveredFeed:
    url: str
    strategy: str
    attempt: int
    feed_format: str | None = None
    content_type: str | None = None


def _xml_tag_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1].lower()
    return tag.lower()


def _looks_like_json_feed(payload: str) -> bool:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, dict):
        return False
    version = str(parsed.get("version", "")).lower()
    has_items = isinstance(parsed.get("items"), list)
    return "jsonfeed.org/version/" in version and has_items


def _looks_like_xml_feed(payload: str) -> tuple[bool, str | None, str | None]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        return False, None, f"response is not parseable XML ({exc})"

    root_name = _xml_tag_name(root.tag)

    if root_name == "rss":
        channel = next((child for child in root if _xml_tag_name(child.tag) == "channel"), None)
        if channel is None:
            return False, None, "RSS document is missing a channel element"
        return True, "rss", None

    if root_name == "feed":
        return True, "atom", None

    if root_name == "rdf":
        child_names = {_xml_tag_name(child.tag) for child in root}
        if "channel" in child_names or "item" in child_names:
            return True, "rss", None
        return False, None, "RDF document does not look like an RSS 1.0 feed"

    return False, None, f"root tag <{root_name}> does not look like a feed"


def _looks_like_feed_link(link_type: str, href: str) -> bool:
    normalized_type = link_type.strip().lower()
    normalized_href = href.strip().lower()

    if normalized_type in FEED_CONTENT_TYPES:
        return True
    if "rss" in normalized_type or "atom" in normalized_type or "json" in normalized_type:
        return True

    feed_like_tokens = (
        "/feed", "/rss", "rss.xml", "atom.xml", "feed.xml", "index.xml", "feeds/posts/default",
    )
    return any(token in normalized_href for token in feed_like_tokens)


async def validate_feed_url(url: str, http_client: httpx.AsyncClient | None = None) -> FeedValidationResult:
    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=FEED_REQUEST_TIMEOUT,
            headers={"accept": "application/rss+xml, application/atom+xml, application/feed+json, application/xml, text/xml"},
        )

    try:
        response = await asyncio.wait_for(http_client.get(url), timeout=FEED_REQUEST_DEADLINE_SECONDS)
        if response.status_code != 200:
            return FeedValidationResult(
                url=url, is_feed=False, final_url=str(response.url),
                content_type=response.headers.get("content-type"),
                reason=f"HTTP {response.status_code}",
            )

        payload = response.text.strip()
        if not payload:
            return FeedValidationResult(
                url=url, is_feed=False, final_url=str(response.url),
                content_type=response.headers.get("content-type"),
                reason="empty response body",
            )

        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()

        if _looks_like_json_feed(payload):
            return FeedValidationResult(
                url=url, is_feed=True, final_url=str(response.url),
                feed_format="json", content_type=content_type,
            )

        is_xml_feed, feed_format, reason = _looks_like_xml_feed(payload)
        if is_xml_feed:
            return FeedValidationResult(
                url=url, is_feed=True, final_url=str(response.url),
                feed_format=feed_format, content_type=content_type,
            )

        if content_type in FEED_CONTENT_TYPES:
            reason = f"{reason}; content-type looked feed-like but body did not validate"

        return FeedValidationResult(
            url=url, is_feed=False, final_url=str(response.url),
            content_type=content_type, reason=reason,
        )
    except Exception as exc:
        return FeedValidationResult(url=url, is_feed=False, reason=str(exc))
    finally:
        if owns_client:
            await http_client.aclose()


async def verify_feed(url: str) -> bool:
    result = await validate_feed_url(url)
    return result.is_feed


async def html_autodiscovery(url: str, http_client: httpx.AsyncClient | None = None) -> list[str]:
    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(follow_redirects=True, timeout=10)

    try:
        response = await http_client.get(url)
        soup = BeautifulSoup(response.text, "html.parser")
        discovered: list[str] = []
        for link in soup.find_all("link"):
            rel_values = link.get("rel", [])
            if isinstance(rel_values, str):
                rel_tokens = [rel_values.lower()]
            else:
                rel_tokens = [str(value).lower() for value in rel_values]

            if "alternate" not in rel_tokens:
                continue

            href = str(link.get("href", "")).strip()
            link_type = str(link.get("type", "")).strip()
            if not href or not _looks_like_feed_link(link_type, href):
                continue

            discovered.append(urljoin(url, href))

        return _dedupe_urls(discovered)
    except Exception:
        return []
    finally:
        if owns_client:
            await http_client.aclose()


async def try_common_paths(url: str, http_client: httpx.AsyncClient | None = None) -> list[str]:
    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(follow_redirects=True, timeout=10)

    found = []
    try:
        for path in COMMON_FEED_PATHS:
            candidate = urljoin(url.rstrip("/") + "/", path.lstrip("/"))
            result = await validate_feed_url(candidate, http_client=http_client)
            if result.is_feed:
                found.append(result.final_url or candidate)
    finally:
        if owns_client:
            await http_client.aclose()

    return found


async def _validate_candidates(
    candidates: list[str],
    attempted_urls: set[str],
    failures: dict[str, str],
    http_client: httpx.AsyncClient,
    strategy_name: str,
    strategy_index: int,
) -> list[DiscoveredFeed]:
    verified: list[DiscoveredFeed] = []
    for candidate in _dedupe_urls(candidates):
        if candidate in attempted_urls:
            continue
        if _extract_domain(candidate) in _FEED_BLOCKED_DOMAINS:
            continue

        attempted_urls.add(candidate)
        result = await validate_feed_url(candidate, http_client=http_client)
        if result.is_feed:
            verified.append(
                DiscoveredFeed(
                    url=result.final_url or candidate,
                    strategy=strategy_name,
                    attempt=strategy_index,
                    feed_format=result.feed_format,
                    content_type=result.content_type,
                )
            )
            continue

        failures[candidate] = result.reason or "did not validate as a feed"

    deduped: list[DiscoveredFeed] = []
    seen = set()
    for feed in verified:
        if feed.url in seen:
            continue
        seen.add(feed.url)
        deduped.append(feed)
    return deduped
