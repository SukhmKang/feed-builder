import os
import random
import re
from urllib.parse import urlparse


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


def _extract_domain(url: str) -> str:
    host = urlparse(url).netloc.lower().strip()
    if host.startswith("www."):
        return host[4:]
    return host


def _normalize_search_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


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


def _recent_failure_urls(failures: dict[str, str], limit: int = 8) -> list[str]:
    return [url for url in list(failures.keys())[-limit:]]


def _pick_tavily_api_key() -> str:
    raw_value = os.getenv("TAVILY_API_KEYS", "").strip()
    if not raw_value:
        raise RuntimeError("TAVILY_API_KEYS is not configured")
    candidates = [item.strip() for item in raw_value.split(",") if item.strip()]
    if not candidates:
        raise RuntimeError("TAVILY_API_KEYS does not contain any usable API keys")
    return random.choice(candidates)
