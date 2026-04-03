"""
Deterministic Google News RSS helpers.

Exports:
- `google_news_search_feed_url(query) -> str`
- `is_google_news_search_feed_url(value) -> bool`

Behavior:
- Builds a Google News RSS search URL for a query string.
- Intended for synthetic fallback feeds when a source is valuable but has no
  discoverable native RSS/Atom feed.
"""

from urllib.parse import quote_plus

GOOGLE_NEWS_SEARCH_BASE = "https://news.google.com/rss/search?q="


def google_news_search_feed_url(query: str) -> str:
    """Build a Google News RSS search URL for the provided query."""
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("query must be non-empty")
    return f"{GOOGLE_NEWS_SEARCH_BASE}{quote_plus(normalized_query)}"


def is_google_news_search_feed_url(value: str) -> bool:
    """Return `True` when the value already looks like a Google News RSS search URL."""
    return value.strip().startswith(GOOGLE_NEWS_SEARCH_BASE)
