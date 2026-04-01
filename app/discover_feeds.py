import asyncio
import json
import os
import random
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tavily import AsyncTavilyClient

from app.llm import generate_text

SEARCH_RESULT_LIMIT = 8
REDDIT_MAX_RETRIES = 4
REDDIT_BACKOFF_BASE_SECONDS = 1.5
REDDIT_MAX_CANDIDATES = 20
WEBSHARE_PROXY_HOST = "p.webshare.io"
WEBSHARE_PROXY_PORT = 80
FEED_REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
FEED_REQUEST_DEADLINE_SECONDS = 12.0
DISCOVERY_LLM_PROVIDER = "anthropic"
DISCOVERY_LLM_MODEL = "claude-haiku-4-5-20251001"

load_dotenv()

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

HOMEPAGE_BLOCKED_DOMAINS = {
    "en.wikipedia.org",
    "wikipedia.org",
    "reddit.com",
    "www.reddit.com",
    "feedspot.com",
    "rss.feedspot.com",
    "rss.app",
    "feeder.co",
    "github.com",
    "x.com",
    "twitter.com",
    "facebook.com",
    "www.facebook.com",
    "instagram.com",
    "www.instagram.com",
    "linkedin.com",
    "www.linkedin.com",
    "youtube.com",
    "www.youtube.com",
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


@dataclass
class DiscoverFeedsResult:
    feeds: list[DiscoveredFeed]
    homepage: str | None
    attempts_run: int
    failures: dict[str, str]


@dataclass
class WebSearchResult:
    url: str
    title: str
    snippet: str


@dataclass(frozen=True)
class DiscoveryStrategy:
    name: str
    description: str
    get_candidates: Callable[[str, str | None, dict[str, str], httpx.AsyncClient], Awaitable[list[str]]]

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
                url=url,
                is_feed=False,
                final_url=str(response.url),
                content_type=response.headers.get("content-type"),
                reason=f"HTTP {response.status_code}",
            )

        payload = response.text.strip()
        if not payload:
            return FeedValidationResult(
                url=url,
                is_feed=False,
                final_url=str(response.url),
                content_type=response.headers.get("content-type"),
                reason="empty response body",
            )

        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()

        if _looks_like_json_feed(payload):
            return FeedValidationResult(
                url=url,
                is_feed=True,
                final_url=str(response.url),
                feed_format="json",
                content_type=content_type,
            )

        is_xml_feed, feed_format, reason = _looks_like_xml_feed(payload)
        if is_xml_feed:
            return FeedValidationResult(
                url=url,
                is_feed=True,
                final_url=str(response.url),
                feed_format=feed_format,
                content_type=content_type,
            )

        if content_type in FEED_CONTENT_TYPES:
            reason = f"{reason}; content-type looked feed-like but body did not validate"

        return FeedValidationResult(
            url=url,
            is_feed=False,
            final_url=str(response.url),
            content_type=content_type,
            reason=reason,
        )
    except Exception as exc:
        return FeedValidationResult(url=url, is_feed=False, reason=str(exc))
    finally:
        if owns_client:
            await http_client.aclose()


async def verify_feed(url: str) -> bool:
    result = await validate_feed_url(url)
    return result.is_feed


async def search_web_urls(query: str, *, max_results: int = SEARCH_RESULT_LIMIT) -> list[str]:
    results = await _search_web_results(query, max_results=max_results)
    return [result.url for result in results]


async def _search_web_results(query: str, *, max_results: int = SEARCH_RESULT_LIMIT) -> list[WebSearchResult]:
    client = AsyncTavilyClient(api_key=_pick_tavily_api_key())
    response = await client.search(
        query=query,
        max_results=max_results,
        include_answer=False,
        include_raw_content=False,
    )
    results = response.get("results", [])
    if not isinstance(results, list):
        return []

    normalized: list[WebSearchResult] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        title = str(item.get("title", "")).strip()
        snippet = str(item.get("content", "") or item.get("snippet", "")).strip()
        if url:
            normalized.append(WebSearchResult(url=url, title=title, snippet=snippet))

    seen: set[str] = set()
    deduped: list[WebSearchResult] = []
    for result in normalized:
        if result.url in seen:
            continue
        seen.add(result.url)
        deduped.append(result)
    return deduped


def _looks_like_feed_link(link_type: str, href: str) -> bool:
    normalized_type = link_type.strip().lower()
    normalized_href = href.strip().lower()

    if normalized_type in FEED_CONTENT_TYPES:
        return True

    if "rss" in normalized_type or "atom" in normalized_type or "json" in normalized_type:
        return True

    feed_like_tokens = (
        "/feed",
        "/rss",
        "rss.xml",
        "atom.xml",
        "feed.xml",
        "index.xml",
        "feeds/posts/default",
    )
    return any(token in normalized_href for token in feed_like_tokens)


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


def _extract_urls(text: str) -> list[str]:
    if not text:
        return []
    raw_urls = re.findall(r'https?://[^\s"\'<>]+', text)
    cleaned: list[str] = []
    for url in raw_urls:
        cleaned_url = url.rstrip(").,!?;:'\"]}")
        if cleaned_url:
            cleaned.append(cleaned_url)
    return cleaned


def _looks_like_probable_feed_url(url: str) -> bool:
    normalized = url.lower().strip()
    if not normalized.startswith(("http://", "https://")):
        return False
    if any(domain in normalized for domain in ("reddit.com/", "redd.it/", "wikipedia.org/", "feedspot.com/", "rsscatalog.com/")):
        return False
    if normalized.endswith((".xml", ".rss", ".atom", "/feed", "/feed/", "/rss", "/rss/")):
        return True
    if any(token in normalized for token in ("/feed", "/rss", "rss.xml", "atom.xml", "feed.xml", "index.xml", "feeds/posts/default", "rss?")):
        return True
    return False


async def _fetch_reddit_listing(
    query: str,
    http_client: httpx.AsyncClient,
    limit: int = 10,
) -> list[dict]:
    response = await _reddit_get_with_backoff(
        http_client,
        "https://www.reddit.com/search.json",
        params={
            "q": query,
            "limit": str(limit),
            "sort": "relevance",
            "t": "all",
            "raw_json": "1",
        },
    )
    response.raise_for_status()
    payload = response.json()
    children = payload.get("data", {}).get("children", [])
    return [child.get("data", {}) for child in children if isinstance(child, dict)]


async def _fetch_reddit_post_comments(
    permalink: str,
    http_client: httpx.AsyncClient,
) -> list[dict]:
    response = await _reddit_get_with_backoff(
        http_client,
        urljoin("https://www.reddit.com", permalink) + ".json",
        params={"raw_json": "1"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    comments_listing = payload[1]
    children = comments_listing.get("data", {}).get("children", [])
    return [child.get("data", {}) for child in children if isinstance(child, dict)]


async def _gather_reddit(
    source_name: str,
    homepage: str | None,
    client: httpx.AsyncClient,
    max_posts_per_query: int = 5,
    max_comments_per_post: int = 5,
) -> list[str]:
    domain = _extract_domain(homepage or "")
    queries = [
        f'"{source_name}" "rss"',
        f'"{source_name}" "feed"',
    ]
    if domain:
        queries = [
            f'"{source_name}" "{domain}" "rss"',
            f'"{source_name}" "{domain}" "feed"',
            f'"{source_name}" "rss"',
            f'"{source_name}" "feed"',
        ]
    urls: list[str] = []

    for query in queries:
        try:
            posts = await _fetch_reddit_listing(query, http_client=client, limit=max_posts_per_query)
        except Exception:
            continue

        for post in posts:
            for text in [
                post.get("selftext", ""),
                post.get("url_overridden_by_dest", ""),
                post.get("url", ""),
                post.get("title", ""),
            ]:
                urls.extend(_extract_urls(text))

            permalink = post.get("permalink")
            if not permalink:
                continue

            try:
                comments = await _fetch_reddit_post_comments(permalink, http_client=client)
            except Exception:
                continue

            for comment in comments[:max_comments_per_post]:
                urls.extend(_extract_urls(comment.get("body", "")))

    filtered = [url for url in _dedupe_urls(urls) if _looks_like_probable_feed_url(url)]
    if domain:
        same_site = [url for url in filtered if _extract_domain(url) == domain or _extract_domain(url).endswith(f".{domain}")]
        if same_site:
            return same_site[:REDDIT_MAX_CANDIDATES]
    return filtered[:REDDIT_MAX_CANDIDATES]


async def _resolve_homepage(source_name: str) -> str | None:
    search_results = await _search_web_results(
        f'{source_name} official site homepage',
        max_results=5,
    )
    if not search_results:
        return None

    candidates: list[WebSearchResult] = []
    for result in search_results:
        homepage = _normalize_homepage_candidate(result.url)
        if not homepage:
            continue
        score = _score_homepage_candidate(source_name, result, homepage)
        if score is None:
            continue
        candidates.append(
            WebSearchResult(
                url=homepage,
                title=result.title,
                snippet=result.snippet,
            )
        )

    if not candidates:
        return None

    llm_choice = await _pick_homepage_with_haiku(source_name, candidates)
    if llm_choice:
        return llm_choice

    return candidates[0].url


async def _gather_feedspot(source_name: str, client: httpx.AsyncClient) -> list[str]:
    try:
        results = await _tavily_search(f'"{source_name}" site:rss.feedspot.com', n=1)
        if not results:
            return []

        feedspot_url = results[0]
        if "feedspot.com" not in feedspot_url:
            return []

        response = await _get_feedspot_page(feedspot_url)
        if response is None or response.status_code != 200:
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        urls: list[str] = []
        for paragraph in soup.find_all("p", class_="trow"):
            data_site = paragraph.get("data-site", "")
            if data_site:
                urls.append(unquote(data_site))

        return _dedupe_urls(urls)
    except Exception:
        return []


async def _strategy_site_search(
    source_name: str,
    homepage: str | None,
    failures: dict[str, str],
    http_client: httpx.AsyncClient,
) -> list[str]:
    del failures, http_client
    if not homepage:
        return []

    homepage_domain = _extract_domain(homepage)
    if not homepage_domain:
        return []

    query = f'site:{homepage_domain} "{source_name}" RSS OR Atom OR "feed url"'
    candidates = await search_web_urls(query)
    return _filter_same_site_urls(candidates, homepage)


async def _strategy_site_autodiscovery(
    source_name: str,
    homepage: str | None,
    failures: dict[str, str],
    http_client: httpx.AsyncClient,
) -> list[str]:
    del source_name, failures
    if not homepage:
        return []
    return await html_autodiscovery(homepage, http_client=http_client)


async def _strategy_common_paths(
    source_name: str,
    homepage: str | None,
    failures: dict[str, str],
    http_client: httpx.AsyncClient,
) -> list[str]:
    del source_name, failures
    if not homepage:
        return []
    return await try_common_paths(homepage, http_client=http_client)


async def _strategy_third_party_mentions(
    source_name: str,
    homepage: str | None,
    failures: dict[str, str],
    http_client: httpx.AsyncClient,
) -> list[str]:
    del failures, http_client
    homepage_hint = f" official homepage {homepage}" if homepage else ""
    fallback_query = f'"{source_name}" Reddit RSS OR Atom OR "feed url" {homepage_hint}'
    return await search_web_urls(fallback_query.strip())


async def _strategy_reddit_search(
    source_name: str,
    homepage: str | None,
    failures: dict[str, str],
    http_client: httpx.AsyncClient,
) -> list[str]:
    failed = set(failures.keys())
    candidates = await _gather_reddit(source_name, homepage, http_client)
    return [candidate for candidate in candidates if candidate not in failed]


async def _strategy_feedspot(
    source_name: str,
    homepage: str | None,
    failures: dict[str, str],
    http_client: httpx.AsyncClient,
) -> list[str]:
    del homepage
    failed = set(failures.keys())
    candidates = await _gather_feedspot(source_name, http_client)
    return [candidate for candidate in candidates if candidate not in failed]


async def _strategy_llm_fallback(
    source_name: str,
    homepage: str | None,
    failures: dict[str, str],
    http_client: httpx.AsyncClient,
) -> list[str]:
    del http_client
    return await _suggest_feed_urls_with_haiku(source_name, homepage, failures)


def _pick_tavily_api_key() -> str:
    raw_value = os.getenv("TAVILY_API_KEYS", "").strip()
    if not raw_value:
        raise RuntimeError("TAVILY_API_KEYS is not configured")
    candidates = [item.strip() for item in raw_value.split(",") if item.strip()]
    if not candidates:
        raise RuntimeError("TAVILY_API_KEYS does not contain any usable API keys")
    return random.choice(candidates)


def _extract_domain(url: str) -> str:
    host = urlparse(url).netloc.lower().strip()
    if host.startswith("www."):
        return host[4:]
    return host


def _normalize_homepage_candidate(url: str) -> str | None:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _score_homepage_candidate(source_name: str, result: WebSearchResult, homepage: str) -> int | None:
    domain = _extract_domain(homepage)
    if not domain or domain in HOMEPAGE_BLOCKED_DOMAINS:
        return None

    normalized_source = _normalize_search_text(source_name)
    normalized_title = _normalize_search_text(result.title)
    normalized_snippet = _normalize_search_text(result.snippet)
    normalized_domain = _normalize_search_text(domain.replace(".", " "))

    score = 0
    if normalized_source and normalized_source in normalized_domain:
        score += 5
    if normalized_source and normalized_source in normalized_title:
        score += 4
    if normalized_source and normalized_source in normalized_snippet:
        score += 2
    if "official" in normalized_title or "official" in normalized_snippet:
        score += 2
    if any(token in domain for token in ("feed", "rss", "reddit", "wikipedia")):
        score -= 3

    return score


async def _pick_homepage_with_haiku(source_name: str, candidates: list[WebSearchResult]) -> str | None:
    prompt = "\n".join(
        [
            "Pick the official homepage URL for this source.",
            "Return JSON only in this shape:",
            json.dumps({"homepage_url": "https://example.com"}, indent=2),
            'You may choose one of the candidate URLs below, or provide a different homepage URL if the right one is obvious.',
            "Do not return Wikipedia, Reddit, Feedspot, social media, or feed/discovery directories as the homepage.",
            f"Source name: {source_name}",
            "Candidates:",
            json.dumps(
                [
                    {
                        "url": candidate.url,
                        "title": candidate.title,
                        "snippet": candidate.snippet,
                    }
                    for candidate in candidates
                ],
                indent=2,
            ),
        ]
    )

    try:
        raw_text = await generate_text(
            prompt,
            provider=DISCOVERY_LLM_PROVIDER,
            model=DISCOVERY_LLM_MODEL,
            max_tokens=300,
            json_output=True,
        )
        parsed = json.loads(raw_text)
    except Exception:
        return None

    if not isinstance(parsed, dict):
        return None

    homepage_value = str(parsed.get("homepage_url", "")).strip()
    homepage = _normalize_homepage_candidate(homepage_value)
    if not homepage:
        return None

    domain = _extract_domain(homepage)
    if not domain or domain in HOMEPAGE_BLOCKED_DOMAINS:
        return None

    return homepage


async def _suggest_feed_urls_with_haiku(
    source_name: str,
    homepage: str | None,
    failures: dict[str, str],
) -> list[str]:
    prompt = "\n".join(
        [
            "Suggest likely native RSS, Atom, or JSON feed URLs for this source.",
            "Return JSON only in this shape:",
            json.dumps({"feed_urls": ["https://example.com/feed"]}, indent=2),
            "Rules:",
            "- Suggest only likely feed URLs.",
            "- Prefer first-party URLs on the homepage domain or closely related brand-owned feed hosts.",
            "- Do not return Wikipedia, Reddit, Feedspot, social media, or generic feed directory pages.",
            "- Keep the list short and high-confidence.",
            f"Source name: {source_name}",
            f"Homepage: {homepage or ''}",
            "Recently failed candidate URLs:",
            json.dumps(_recent_failure_urls(failures), indent=2),
        ]
    )

    try:
        raw_text = await generate_text(
            prompt,
            provider=DISCOVERY_LLM_PROVIDER,
            model=DISCOVERY_LLM_MODEL,
            max_tokens=300,
            json_output=True,
        )
        parsed = json.loads(raw_text)
    except Exception:
        return []

    if not isinstance(parsed, dict):
        return []

    urls = parsed.get("feed_urls", [])
    if not isinstance(urls, list):
        return []

    normalized: list[str] = []
    for value in urls:
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if candidate:
            normalized.append(candidate)
    return _dedupe_urls(normalized)


def _normalize_search_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _recent_failure_urls(failures: dict[str, str], limit: int = 8) -> list[str]:
    return [url for url in list(failures.keys())[-limit:]]


def _filter_same_site_urls(urls: list[str], homepage: str) -> list[str]:
    homepage_domain = _extract_domain(homepage)
    if not homepage_domain:
        return []

    filtered: list[str] = []
    for url in urls:
        candidate_domain = _extract_domain(url)
        if not candidate_domain:
            continue
        if candidate_domain == homepage_domain or candidate_domain.endswith(f".{homepage_domain}"):
            filtered.append(url)
    return _dedupe_urls(filtered)


async def _tavily_search(query: str, n: int = SEARCH_RESULT_LIMIT) -> list[str]:
    client = AsyncTavilyClient(api_key=_pick_tavily_api_key())
    response = await client.search(
        query=query,
        max_results=n,
        include_answer=False,
        include_raw_content=False,
    )
    results = response.get("results", [])
    if not isinstance(results, list):
        return []

    urls: list[str] = []
    for item in results[:n]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        if url:
            urls.append(url)
    return _dedupe_urls(urls)


def _build_rotating_proxy_url() -> str | None:
    proxy_username = str(os.getenv("PROXY_USERNAME", "")).strip()
    proxy_password = str(os.getenv("PROXY_PASSWORD", "")).strip()
    if not proxy_username or not proxy_password:
        return None

    username = proxy_username.removesuffix("-rotate")
    session_id = random.randint(100000, 999999)
    return (
        f"http://{username}-rotate-session-{session_id}:{proxy_password}"
        f"@{WEBSHARE_PROXY_HOST}:{WEBSHARE_PROXY_PORT}/"
    )


async def _get_feedspot_page(url: str) -> httpx.Response | None:
    proxy_url = _build_rotating_proxy_url()
    client_kwargs: dict[str, object] = {
        "follow_redirects": True,
        "timeout": 10,
        "headers": {"connection": "close"},
    }
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    async with httpx.AsyncClient(**client_kwargs) as proxy_client:
        return await proxy_client.get(url)


async def _reddit_get_with_backoff(
    http_client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str],
) -> httpx.Response:
    last_response: httpx.Response | None = None
    for attempt in range(1, REDDIT_MAX_RETRIES + 1):
        response = await http_client.get(
            url,
            params=params,
            headers={"user-agent": "feed-discovery/1.0"},
        )
        last_response = response

        if response.status_code != 429:
            return response

        retry_after_header = response.headers.get("retry-after", "").strip()
        retry_after_seconds: float | None = None
        if retry_after_header:
            try:
                retry_after_seconds = float(retry_after_header)
            except ValueError:
                retry_after_seconds = None

        backoff_seconds = retry_after_seconds or (REDDIT_BACKOFF_BASE_SECONDS * attempt)
        await asyncio.sleep(backoff_seconds)

    if last_response is None:
        raise RuntimeError(f"Reddit request failed before receiving a response for {url}")
    return last_response


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen = set()
    deduped = []
    for url in urls:
        if not isinstance(url, str):
            continue
        normalized = url.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


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


async def discover_feeds_detailed(source_name: str) -> DiscoverFeedsResult:
    attempted_urls: set[str] = set()
    failures: dict[str, str] = {}
    homepage = await _resolve_homepage(source_name)
    strategies = [
        DiscoveryStrategy(
            name="site_search",
            description="Same-site search-engine candidates for likely feed endpoints",
            get_candidates=_strategy_site_search,
        ),
        DiscoveryStrategy(
            name="site_autodiscovery",
            description="First-party website autodiscovery links",
            get_candidates=_strategy_site_autodiscovery,
        ),
        DiscoveryStrategy(
            name="common_paths",
            description="First-party common RSS/Atom path probing",
            get_candidates=_strategy_common_paths,
        ),
        DiscoveryStrategy(
            name="feedspot",
            description="Feedspot directory pages that expose canonical site URLs",
            get_candidates=_strategy_feedspot,
        ),
        DiscoveryStrategy(
            name="reddit_search",
            description="Reddit posts and comments that mention feed URLs for the source",
            get_candidates=_strategy_reddit_search,
        ),
        DiscoveryStrategy(
            name="third_party_mentions",
            description="Third-party mentions on the broader web",
            get_candidates=_strategy_third_party_mentions,
        ),
        DiscoveryStrategy(
            name="llm_fallback",
            description="Final lightweight LLM fallback for likely native feed URLs",
            get_candidates=_strategy_llm_fallback,
        ),
    ]

    async with httpx.AsyncClient(follow_redirects=True, timeout=FEED_REQUEST_TIMEOUT) as http_client:
        for strategy_index, strategy in enumerate(strategies, start=1):
            candidates = await strategy.get_candidates(
                source_name=source_name,
                homepage=homepage,
                failures=failures,
                http_client=http_client,
            )
            verified = await _validate_candidates(
                candidates,
                attempted_urls,
                failures,
                http_client,
                strategy_name=strategy.name,
                strategy_index=strategy_index,
            )
            if verified:
                return DiscoverFeedsResult(
                    feeds=verified,
                    homepage=homepage,
                    attempts_run=strategy_index,
                    failures=failures.copy(),
                )

    return DiscoverFeedsResult(
        feeds=[],
        homepage=homepage,
        attempts_run=len(strategies),
        failures=failures.copy(),
    )


async def discover_feeds(source_name: str) -> list[str]:
    result = await discover_feeds_detailed(source_name)
    return [feed.url for feed in result.feeds]


async def main():
    result = await discover_feeds_detailed("Pitchfork")
    print(json.dumps(
        {
            "homepage": result.homepage,
            "attempts_run": result.attempts_run,
            "feeds": [
                {
                    "url": feed.url,
                    "strategy": feed.strategy,
                    "attempt": feed.attempt,
                    "feed_format": feed.feed_format,
                    "content_type": feed.content_type,
                }
                for feed in result.feeds
            ],
            "failures": result.failures,
        },
        indent=2,
    ))


if __name__ == "__main__":
    asyncio.run(main())
