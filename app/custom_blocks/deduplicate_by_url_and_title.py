"""Deduplicate articles by URL and normalized title within a pipeline run."""

import re
import unicodedata
from typing import Any


# Module-level sets shared across all articles in one pipeline run.
_seen_urls: set[str] = set()
_seen_titles: set[str] = set()


def _normalize_title(title: str) -> str:
    """Lowercase, strip accents, collapse whitespace, remove punctuation."""
    nfd = unicodedata.normalize("NFD", title)
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    lowered = stripped.lower()
    no_punct = re.sub(r"[^\w\s]", "", lowered)
    return re.sub(r"\s+", " ", no_punct).strip()


async def run(article: dict[str, Any]) -> dict[str, Any]:
    working = dict(article)

    url: str = str(working.get("url") or working.get("link") or "").strip()
    raw_title: str = str(working.get("title") or "").strip()
    norm_title: str = _normalize_title(raw_title)

    # URL dedup
    if url and url in _seen_urls:
        return {
            "passed": False,
            "article": working,
            "reason": f"Duplicate URL already seen: {url}",
        }

    # Title dedup (only for titles long enough to be meaningful)
    if norm_title and len(norm_title) >= 20 and norm_title in _seen_titles:
        return {
            "passed": False,
            "article": working,
            "reason": f"Duplicate title already seen: {raw_title!r}",
        }

    # First time — register this article
    if url:
        _seen_urls.add(url)
    if norm_title and len(norm_title) >= 20:
        _seen_titles.add(norm_title)

    return {
        "passed": True,
        "article": working,
        "reason": "Article is unique",
    }
