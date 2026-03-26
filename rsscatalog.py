"""
Deterministic RSS Catalog scraping helpers.

Exports:
- `fetch_categories() -> list[RSSCatalogCategory]`
- `search_categories(query, limit=10) -> list[RSSCatalogCategory]`
- `get_category_feeds(category_slug_or_name, limit=None) -> list[RSSCatalogFeed]`

Behavior:
- Scrapes category links from the RSS Catalog homepage.
- Performs local query matching against scraped category names/slugs.
- Scrapes category pages for feed URLs, titles, and descriptions.
- Uses JSON-LD item lists as a fallback when the HTML feed cards are incomplete.
"""

from __future__ import annotations

import json
import re
from typing import TypedDict
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

RSSCATALOG_BASE_URL = "https://www.rsscatalog.com/"
RSSCATALOG_TIMEOUT = 15.0


class RSSCatalogCategory(TypedDict):
    """A category discovered from the RSS Catalog homepage."""

    name: str
    slug: str
    url: str


class RSSCatalogFeed(TypedDict):
    """A feed entry scraped from an RSS Catalog category page."""

    title: str
    description: str
    feed_url: str
    category_name: str
    category_slug: str
    category_url: str


async def fetch_categories() -> list[RSSCatalogCategory]:
    """Fetch all categories listed on the RSS Catalog homepage."""
    async with _build_http_client() as http_client:
        response = await http_client.get(RSSCATALOG_BASE_URL)
        response.raise_for_status()
        return _parse_categories(response.text)


async def search_categories(query: str, limit: int = 10) -> list[RSSCatalogCategory]:
    """Search scraped RSS Catalog categories locally by name and slug."""
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return []

    categories = await fetch_categories()
    scored: list[tuple[tuple[int, int, str], RSSCatalogCategory]] = []
    for category in categories:
        name = category["name"]
        slug = category["slug"]
        normalized_name = _normalize_text(name)
        normalized_slug = _normalize_text(slug.replace("+", " "))

        haystacks = [normalized_name, normalized_slug]
        if not any(normalized_query in haystack for haystack in haystacks):
            continue

        startswith_score = 0 if any(haystack.startswith(normalized_query) for haystack in haystacks) else 1
        length_score = min(len(normalized_name), len(normalized_slug))
        scored.append(((startswith_score, length_score, normalized_name), category))

    scored.sort(key=lambda item: item[0])
    return [category for _, category in scored[: max(1, limit)]]


async def get_category_feeds(category_slug_or_name: str, limit: int | None = None) -> list[RSSCatalogFeed]:
    """Fetch feed entries from one RSS Catalog category page."""
    normalized_input = category_slug_or_name.strip()
    if not normalized_input:
        return []

    async with _build_http_client() as http_client:
        category = await _resolve_category(normalized_input, http_client=http_client)
        if category is None:
            return []

        response = await http_client.get(category["url"])
        response.raise_for_status()
        feeds = _parse_category_feeds(
            response.text,
            category_name=category["name"],
            category_slug=category["slug"],
            category_url=category["url"],
        )
        if limit is None:
            return feeds
        return feeds[: max(1, limit)]


def _build_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=True, timeout=RSSCATALOG_TIMEOUT)


def _parse_categories(html: str) -> list[RSSCatalogCategory]:
    soup = BeautifulSoup(html, "html.parser")
    categories: list[RSSCatalogCategory] = []

    for link in soup.select("a.cat-card[href]"):
        href = str(link.get("href", "")).strip()
        name = link.get_text(" ", strip=True)
        if not href or not name:
            continue
        slug = _category_slug_from_href(href)
        categories.append(
            RSSCatalogCategory(
                name=name,
                slug=slug,
                url=urljoin(RSSCATALOG_BASE_URL, href),
            )
        )

    return _dedupe_categories(categories)


def _parse_category_feeds(
    html: str,
    *,
    category_name: str,
    category_slug: str,
    category_url: str,
) -> list[RSSCatalogFeed]:
    soup = BeautifulSoup(html, "html.parser")
    feeds: list[RSSCatalogFeed] = []

    for container in soup.select("div.feed"):
        title_text = container.select_one("h3")
        title = _clean_feed_title("" if title_text is None else title_text.get_text(" ", strip=True))

        paragraphs = container.find_all("p")
        description = ""
        feed_url = ""
        for paragraph in paragraphs:
            code_tag = paragraph.find("code")
            if code_tag is not None:
                feed_url = code_tag.get_text(" ", strip=True)
                continue
            text = paragraph.get_text(" ", strip=True)
            if text and not description:
                description = text

        if not feed_url:
            continue

        feeds.append(
            RSSCatalogFeed(
                title=title or feed_url,
                description=description,
                feed_url=feed_url,
                category_name=category_name,
                category_slug=category_slug,
                category_url=category_url,
            )
        )

    if feeds:
        return _dedupe_feeds(feeds)

    return _parse_category_feeds_from_json_ld(
        html,
        category_name=category_name,
        category_slug=category_slug,
        category_url=category_url,
    )


def _parse_category_feeds_from_json_ld(
    html: str,
    *,
    category_name: str,
    category_slug: str,
    category_url: str,
) -> list[RSSCatalogFeed]:
    soup = BeautifulSoup(html, "html.parser")
    feeds: list[RSSCatalogFeed] = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw_json = script.string or script.get_text(strip=True)
        if not raw_json:
            continue

        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            continue

        if not isinstance(parsed, dict) or parsed.get("@type") != "ItemList":
            continue

        items = parsed.get("itemListElement", [])
        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            feed_url = str(item.get("url", "")).strip()
            title = str(item.get("name", "")).strip()
            if not feed_url:
                continue
            feeds.append(
                RSSCatalogFeed(
                    title=title or feed_url,
                    description="",
                    feed_url=feed_url,
                    category_name=category_name,
                    category_slug=category_slug,
                    category_url=category_url,
                )
            )

    return _dedupe_feeds(feeds)


async def _resolve_category(value: str, *, http_client: httpx.AsyncClient) -> RSSCatalogCategory | None:
    categories = _parse_categories((await http_client.get(RSSCATALOG_BASE_URL)).text)
    exact_slug = _category_slug_from_href(value)
    exact_name = _normalize_text(value)

    for category in categories:
        if category["slug"] == exact_slug or _normalize_text(category["name"]) == exact_name:
            return category

    matches = await search_categories(value, limit=1)
    return matches[0] if matches else None


def _category_slug_from_href(href: str) -> str:
    parsed = urlparse(href)
    path = parsed.path.strip("/")
    if path:
        return path
    return quote_plus(href.strip())


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _clean_feed_title(value: str) -> str:
    text = value.strip()
    return re.sub(r"^[^\w]+", "", text)


def _dedupe_categories(categories: list[RSSCatalogCategory]) -> list[RSSCatalogCategory]:
    seen: set[str] = set()
    deduped: list[RSSCatalogCategory] = []
    for category in categories:
        slug = category["slug"]
        if slug in seen:
            continue
        seen.add(slug)
        deduped.append(category)
    return deduped


def _dedupe_feeds(feeds: list[RSSCatalogFeed]) -> list[RSSCatalogFeed]:
    seen: set[str] = set()
    deduped: list[RSSCatalogFeed] = []
    for feed in feeds:
        feed_url = feed["feed_url"]
        if feed_url in seen:
            continue
        seen.add(feed_url)
        deduped.append(feed)
    return deduped
