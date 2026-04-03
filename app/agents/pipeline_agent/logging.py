import json
import time
from contextlib import asynccontextmanager
from typing import Any


def log(enabled: bool, event: str, payload: Any) -> None:
    if not enabled:
        return
    print(f"[pipeline_agent] {event}")
    if isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=True, default=str))
    print()


@asynccontextmanager
async def log_timed(enabled: bool, event: str):
    """Async context manager that logs how long a stage took."""
    if enabled:
        print(f"[pipeline_agent] {event}.start")
        print()
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        if enabled:
            print(f"[pipeline_agent] {event}.done  ({elapsed:.1f}s)")
            print()


def article_log_summary(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(article.get("title", "")).strip(),
        "url": str(article.get("url", "")).strip(),
        "published_at": str(article.get("published_at", "")).strip(),
        "source_name": str(article.get("source_name", "")).strip(),
        "source_type": str(article.get("source_type", "")).strip(),
    }


def should_retry_agent_error(exc: Exception) -> bool:
    text = str(exc).lower()
    retry_markers = (
        "529",
        "overloaded",
        "rate limit",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "server error",
        "connection reset",
    )
    return any(marker in text for marker in retry_markers)
