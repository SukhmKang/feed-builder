import asyncio
import json
import os
import random
import re
from urllib.parse import unquote, urljoin

import httpx
from tavily import AsyncTavilyClient

from app.ai.llm import generate_text
from app.sources._feed_validator import html_autodiscovery, try_common_paths, validate_feed_url
from app.sources._homepage_resolver import (
    DISCOVERY_LLM_MODEL,
    DISCOVERY_LLM_PROVIDER,
    WebSearchResult,
    search_web_urls,
)
from app.sources._utils import (
    _dedupe_urls,
    _extract_domain,
    _extract_urls,
    _filter_same_site_urls,
    _looks_like_probable_feed_url,
    _normalize_search_text,
    _pick_tavily_api_key,
    _recent_failure_urls,
)

SEARCH_RESULT_LIMIT = 8
REDDIT_MAX_RETRIES = 4
REDDIT_BACKOFF_BASE_SECONDS = 1.5
REDDIT_MAX_CANDIDATES = 20
WEBSHARE_PROXY_HOST = "p.webshare.io"
WEBSHARE_PROXY_PORT = 80

_FAKE_TLDS = {".fake", ".test", ".example", ".invalid", ".localhost", ".local"}


# ---------------------------------------------------------------------------
# Tavily search
# ---------------------------------------------------------------------------

async def _tavily_search(
    query: str,
    n: int = SEARCH_RESULT_LIMIT,
    include_domains: list[str] | None = None,
) -> list[str]:
    client = AsyncTavilyClient(api_key=_pick_tavily_api_key())
    kwargs: dict = dict(
        query=query,
        max_results=n,
        include_answer=False,
        include_raw_content=False,
    )
    if include_domains:
        kwargs["include_domains"] = include_domains
    response = await client.search(**kwargs)
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


# ---------------------------------------------------------------------------
# Proxy / HTTP helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------

async def _reddit_get_with_backoff(
    http_client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str],
) -> httpx.Response:
    last_response: httpx.Response | None = None
    for attempt in range(1, REDDIT_MAX_RETRIES + 1):
        response = await http_client.get(
            url, params=params, headers={"user-agent": "feed-discovery/1.0"},
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


async def _fetch_reddit_listing(
    query: str,
    http_client: httpx.AsyncClient,
    limit: int = 10,
) -> list[dict]:
    response = await _reddit_get_with_backoff(
        http_client,
        "https://www.reddit.com/search.json",
        params={"q": query, "limit": str(limit), "sort": "relevance", "t": "all", "raw_json": "1"},
    )
    response.raise_for_status()
    payload = response.json()
    children = payload.get("data", {}).get("children", [])
    return [child.get("data", {}) for child in children if isinstance(child, dict)]


async def _fetch_reddit_post_comments(
    permalink: str,
    http_client: httpx.AsyncClient,
) -> list[dict]:
    from urllib.parse import urljoin
    response = await _reddit_get_with_backoff(
        http_client,
        urljoin("https://www.reddit.com", permalink) + ".json",
        params={"raw_json": "1"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    children = payload[1].get("data", {}).get("children", [])
    return [child.get("data", {}) for child in children if isinstance(child, dict)]


async def _gather_reddit(
    source_name: str,
    homepage: str | None,
    client: httpx.AsyncClient,
    max_posts_per_query: int = 5,
    max_comments_per_post: int = 5,
) -> list[str]:
    domain = _extract_domain(homepage or "")
    queries = [f'"{source_name}" "rss"', f'"{source_name}" "feed"']
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
        same_site = [
            url for url in filtered
            if _extract_domain(url) == domain or _extract_domain(url).endswith(f".{domain}")
        ]
        if same_site:
            return same_site[:REDDIT_MAX_CANDIDATES]
    return filtered[:REDDIT_MAX_CANDIDATES]


# ---------------------------------------------------------------------------
# Feedspot + site-search gathering
# ---------------------------------------------------------------------------

def _extract_feed_urls_from_content(raw_content: str) -> list[str]:
    from urllib.parse import unquote
    param_urls = re.findall(r'site:(https?://[^\s&")\]]+)', unquote(raw_content))
    feed_path_urls = re.findall(r'https?://[^\s")\]]+/(?:feed|rss|atom)[/\w\-\.]*', raw_content)
    xml_urls = re.findall(r'https?://[^\s")\]]+\.(?:xml|rss|atom)', raw_content)
    all_urls = param_urls + feed_path_urls + xml_urls
    return [
        u for u in all_urls
        if "feedspot.com" not in u
        and not any(u.lower().split("/")[2].endswith(tld) for tld in _FAKE_TLDS)
    ]


import re  # noqa: E402 — placed here to avoid circular import with unquote usage above


async def _filter_off_domain_feeds_with_haiku(
    source_name: str, homepage: str, feed_urls: list[str]
) -> list[str]:
    prompt = "\n".join([
        f"You are helping find RSS/Atom feed URLs for the source \"{source_name}\" (homepage: {homepage}).",
        "Some of the candidate feed URLs below may belong to unrelated sources (e.g. foreign-language editions of a different outlet, competitor sites, or completely unrelated domains).",
        "Return only the feed URLs that plausibly belong to this source.",
        "Return JSON only in this shape:",
        json.dumps({"feed_urls": ["https://example.com/feed"]}, indent=2),
        "Candidate URLs:",
        json.dumps(feed_urls, indent=2),
    ])
    try:
        raw_text = await generate_text(
            prompt,
            provider=DISCOVERY_LLM_PROVIDER,
            model=DISCOVERY_LLM_MODEL,
            max_tokens=500,
            json_output=True,
        )
        parsed = json.loads(raw_text)
        urls = parsed.get("feed_urls", [])
        if not isinstance(urls, list):
            return feed_urls
        filtered = [u for u in urls if isinstance(u, str) and u.strip()]
        return filtered if filtered else feed_urls
    except Exception:
        return feed_urls


async def _gather_feedspot(
    source_name: str, homepage: str | None, http_client: httpx.AsyncClient
) -> list[str]:
    del http_client
    try:
        results = await _tavily_search(
            f'"{source_name}" RSS feeds', n=5, include_domains=["rss.feedspot.com"]
        )
        results = [r for r in results if "feedspot.com" in r]
        if not results:
            return []

        normalized_source = _normalize_search_text(source_name).replace(" ", "")
        source_specific_matches = [
            r for r in results
            if normalized_source in r.rstrip("/").rsplit("/", 1)[-1].replace("_", "")
        ]
        source_specific = min(source_specific_matches, key=lambda r: len(r), default=None)
        feedspot_url = source_specific or results[0]
        is_generic_page = source_specific is None

        client = AsyncTavilyClient(api_key=_pick_tavily_api_key())
        response = await client.extract(urls=[feedspot_url])

        raw_content = ""
        for result in response.get("results", []):
            if result.get("url") == feedspot_url:
                raw_content = result.get("raw_content", "")
                break

        if not raw_content:
            return []

        all_urls = _dedupe_urls(_extract_feed_urls_from_content(raw_content))

        if is_generic_page:
            if not homepage:
                return []
            domain = _extract_domain(homepage)
            if not domain:
                return []
            return [
                u for u in all_urls
                if _extract_domain(u) == domain or _extract_domain(u).endswith(f".{domain}")
            ]

        if homepage:
            domain = _extract_domain(homepage)
            off_domain = [
                u for u in all_urls
                if _extract_domain(u) != domain and not _extract_domain(u).endswith(f".{domain}")
            ]
            if off_domain:
                all_urls = await _filter_off_domain_feeds_with_haiku(source_name, homepage, all_urls)

        return all_urls

    except Exception:
        return []


async def _gather_from_site_search(source_name: str, homepage: str) -> list[str]:
    domain = _extract_domain(homepage)
    if not domain:
        return []

    result_urls = await _tavily_search(
        f'"{source_name}" RSS feeds', n=3, include_domains=[domain]
    )
    if not result_urls:
        return []

    client = AsyncTavilyClient(api_key=_pick_tavily_api_key())
    response = await client.extract(urls=result_urls[:2])

    feed_urls: list[str] = []
    for result in response.get("results", []):
        raw_content = result.get("raw_content", "")
        if raw_content:
            feed_urls.extend(_extract_feed_urls_from_content(raw_content))

    deduped = _dedupe_urls(feed_urls)
    off_domain = [
        u for u in deduped
        if _extract_domain(u) != domain and not _extract_domain(u).endswith(f".{domain}")
    ]
    if off_domain:
        deduped = await _filter_off_domain_feeds_with_haiku(source_name, homepage, deduped)
    return deduped


# ---------------------------------------------------------------------------
# LLM fallback
# ---------------------------------------------------------------------------

async def _suggest_feed_urls_with_haiku(
    source_name: str,
    homepage: str | None,
    failures: dict[str, str],
) -> list[str]:
    prompt = "\n".join([
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
    ])

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


# ---------------------------------------------------------------------------
# Strategy wrappers
# ---------------------------------------------------------------------------

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


async def _strategy_feedspot(
    source_name: str,
    homepage: str | None,
    failures: dict[str, str],
    http_client: httpx.AsyncClient,
) -> list[str]:
    failed = set(failures.keys())
    candidates = await _gather_feedspot(source_name, homepage, http_client)
    return [c for c in candidates if c not in failed]


async def _strategy_site_search_own_domain(
    source_name: str,
    homepage: str | None,
    failures: dict[str, str],
    http_client: httpx.AsyncClient,
) -> list[str]:
    del failures, http_client
    if not homepage:
        return []
    return await _gather_from_site_search(source_name, homepage)


async def _strategy_reddit_search(
    source_name: str,
    homepage: str | None,
    failures: dict[str, str],
    http_client: httpx.AsyncClient,
) -> list[str]:
    failed = set(failures.keys())
    candidates = await _gather_reddit(source_name, homepage, http_client)
    return [c for c in candidates if c not in failed]


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


async def _strategy_llm_fallback(
    source_name: str,
    homepage: str | None,
    failures: dict[str, str],
    http_client: httpx.AsyncClient,
) -> list[str]:
    del http_client
    return await _suggest_feed_urls_with_haiku(source_name, homepage, failures)
