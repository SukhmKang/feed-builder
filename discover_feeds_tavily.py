import anthropic
import argparse
import asyncio
import json
import os
import random
import re
import dotenv
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

dotenv.load_dotenv()

SEARCH_RESULT_LIMIT = 8
DISCOVERY_LLM_MODEL = "claude-haiku-4-5-20251001"

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
class WebSearchResult:
    url: str
    title: str
    snippet: str


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


def _normalize_search_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


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


async def _generate_text_haiku(prompt: str, max_tokens: int = 300) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")
    client = anthropic.Anthropic(api_key=api_key)
    response = await asyncio.to_thread(
        client.messages.create,
        model=DISCOVERY_LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    blocks = getattr(response, "content", []) or []
    parts = [block.text for block in blocks if getattr(block, "text", None)]
    raw = "\n\n".join(parts).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
    return fenced.group(1).strip() if fenced else raw


async def _pick_homepage_with_haiku(source_name: str, candidates: list[WebSearchResult]) -> str | None:
    prompt = "\n".join(
        [
            "Pick the official homepage URL for this source.",
            "Return JSON only in this shape:",
            json.dumps({"homepage_url": "https://example.com"}, indent=2),
            "You may choose one of the candidate URLs below, or provide a different homepage URL if the right one is obvious.",
            "Do not return Wikipedia, Reddit, Feedspot, social media, or feed/discovery directories as the homepage.",
            f"Source name: {source_name}",
            "Candidates:",
            json.dumps(
                [{"url": c.url, "title": c.title, "snippet": c.snippet} for c in candidates],
                indent=2,
            ),
        ]
    )
    try:
        raw_text = await _generate_text_haiku(prompt)
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


async def _search_web_results(query: str, *, max_results: int = SEARCH_RESULT_LIMIT) -> list[WebSearchResult]:
    from tavily import AsyncTavilyClient
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
        candidates.append(WebSearchResult(url=homepage, title=result.title, snippet=result.snippet))

    if not candidates:
        return None

    llm_choice = await _pick_homepage_with_haiku(source_name, candidates)
    return llm_choice or candidates[0].url


async def _tavily_search(
    query: str,
    n: int = SEARCH_RESULT_LIMIT,
    include_domains: list[str] | None = None,
) -> list[str]:
    from tavily import AsyncTavilyClient
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


_FAKE_TLDS = {".fake", ".test", ".example", ".invalid", ".localhost", ".local"}

def _extract_feed_urls_from_content(raw_content: str) -> list[str]:
    """Pull likely RSS/Atom feed URLs out of raw page text."""
    param_urls = re.findall(r'site:(https?://[^\s&")\]]+)', unquote(raw_content))
    feed_path_urls = re.findall(r'https?://[^\s")\]]+/(?:feed|rss|atom)[/\w\-\.]*', raw_content)
    xml_urls = re.findall(r'https?://[^\s")\]]+\.(?:xml|rss|atom)', raw_content)
    all_urls = param_urls + feed_path_urls + xml_urls
    return [
        u for u in all_urls
        if "feedspot.com" not in u
        and not any(u.lower().split("/")[2].endswith(tld) for tld in _FAKE_TLDS)
    ]


async def _gather_feedspot(source_name: str, homepage: str | None) -> list[str]:
    """Search Feedspot for the source's feed URLs, filtered to the source's domain."""
    try:
        from tavily import AsyncTavilyClient
        results = await _tavily_search(
            f'"{source_name}" RSS feeds', n=5, include_domains=["rss.feedspot.com"]
        )
        results = [r for r in results if "feedspot.com" in r]
        if not results:
            return []

        normalized_source = _normalize_search_text(source_name).replace(" ", "")
        source_specific = next(
            (r for r in results if normalized_source in r.rstrip("/").rsplit("/", 1)[-1].replace("_", "")),
            None,
        )
        feedspot_url = source_specific or results[0]
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

        is_generic_page = source_specific is None

        if is_generic_page:
            if not homepage:
                return []
            domain = _extract_domain(homepage)
            if not domain:
                return []
            return [
                url for url in all_urls
                if _extract_domain(url) == domain or _extract_domain(url).endswith(f".{domain}")
            ]

        return all_urls

    except Exception:
        return []


async def _gather_from_site_search(source_name: str, homepage: str) -> list[str]:
    """Search directly on the source's own domain for pages mentioning RSS feeds,
    then extract feed URLs from those pages.

    This complements the Feedspot strategy and handles cases where feeds live on
    a different subdomain (e.g. feeds.bbci.co.uk vs bbc.co.uk).
    """
    try:
        from tavily import AsyncTavilyClient
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

        return _dedupe_urls(feed_urls)

    except Exception:
        return []


async def discover_feeds(source_name: str) -> list[str]:
    homepage = await _resolve_homepage(source_name)

    tasks: list = [_gather_feedspot(source_name, homepage)]
    if homepage:
        tasks.append(_gather_from_site_search(source_name, homepage))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_urls: list[str] = []
    for result in results:
        if isinstance(result, list):
            all_urls.extend(result)

    return _dedupe_urls(all_urls)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Discover RSS feeds for a source via Feedspot + Tavily")
    parser.add_argument("sources", nargs="+", help="Source name(s) to look up")
    args = parser.parse_args()

    async def run():
        for source in args.sources:
            print(f"\n=== {source} ===")
            feeds = await discover_feeds(source)
            if feeds:
                for url in feeds:
                    print(url)
            else:
                print("No feeds found.")

    asyncio.run(run())
