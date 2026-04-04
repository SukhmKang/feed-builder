import json
from dataclasses import dataclass

from tavily import AsyncTavilyClient

from app.ai.llm import generate_text
from app.sources._utils import (
    _dedupe_urls,
    _extract_domain,
    _normalize_search_text,
    _pick_tavily_api_key,
)

DISCOVERY_LLM_PROVIDER = "anthropic"
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


def _normalize_homepage_candidate(url: str) -> str | None:
    from urllib.parse import urlparse
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


async def _search_web_results(query: str, *, max_results: int = 8) -> list[WebSearchResult]:
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


async def search_web_urls(query: str, *, max_results: int = 8) -> list[str]:
    results = await _search_web_results(query, max_results=max_results)
    return [result.url for result in results]


async def _pick_homepage_with_haiku(source_name: str, candidates: list[WebSearchResult]) -> str | None:
    prompt = "\n".join([
        "Pick the official homepage URL for this source.",
        "Return JSON only in this shape:",
        json.dumps({"homepage_url": "https://example.com"}, indent=2),
        'If you are not confident, return {"homepage_url": null} rather than guessing.',
        "You may choose one of the candidate URLs below, or provide a different homepage URL if the right one is obvious.",
        "Do not return Wikipedia, Reddit, Feedspot, social media, or feed/discovery directories as the homepage.",
        f"Source name: {source_name}",
        "Candidates:",
        json.dumps(
            [{"url": c.url, "title": c.title, "snippet": c.snippet} for c in candidates],
            indent=2,
        ),
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
        return None

    if not isinstance(parsed, dict):
        return None

    homepage_value = parsed.get("homepage_url")
    if homepage_value is None:  # explicit null from LLM
        return None

    homepage = _normalize_homepage_candidate(str(homepage_value).strip())
    if not homepage:
        return None

    domain = _extract_domain(homepage)
    if not domain or domain in HOMEPAGE_BLOCKED_DOMAINS:
        return None

    return homepage


async def _resolve_homepage(source_name: str) -> str | None:
    search_results = await _search_web_results(
        f'{source_name} official site homepage', max_results=5,
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
    if llm_choice:
        return llm_choice

    return candidates[0].url
