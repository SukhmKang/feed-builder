"""Shared helpers for Claude Agent SDK tool responses and previews."""

import inspect
import json
import time
from typing import Any

MAX_PREVIEW_LIMIT = 10
DEFAULT_PREVIEW_LIMIT = 5

SOURCE_SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "type": {"type": "string"},
        "feed": {"type": "string"},
    },
    "required": ["type", "feed"],
}


def success(payload: Any) -> dict[str, Any]:
    _log_tool_result("success", payload)
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, indent=2, ensure_ascii=True),
            }
        ]
    }


def error(message: str) -> dict[str, Any]:
    _log_tool_result("error", message)
    return {
        "content": [{"type": "text", "text": message}],
        "is_error": True,
    }


def _log_tool_result(status: str, payload: Any) -> None:
    caller_name = _get_tool_caller_name()
    print(f"[agent_tool] {caller_name}.{status}")
    if isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=True, default=str))
    print()


def _get_tool_caller_name() -> str:
    for frame_info in inspect.stack()[2:]:
        function_name = frame_info.function
        if function_name.endswith("_tool"):
            return function_name
    return "unknown_tool"


def log_tool_event(event: str, payload: Any, *, tool_name: str | None = None) -> None:
    resolved_tool_name = tool_name or _get_tool_caller_name()
    print(f"[agent_tool] {resolved_tool_name}.{event}")
    if isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=True, default=str))
    print()


def tool_timer(*, tool_name: str | None = None) -> tuple[str, float]:
    return (tool_name or _get_tool_caller_name(), time.perf_counter())


def log_tool_done(timer: tuple[str, float], payload: Any) -> None:
    name, started_at = timer
    elapsed = time.perf_counter() - started_at
    log_tool_event("done", {"elapsed_seconds": round(elapsed, 2), **payload}, tool_name=name)


def clamp_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = DEFAULT_PREVIEW_LIMIT
    return max(1, min(limit, MAX_PREVIEW_LIMIT))


def truncate_text(value: Any, *, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def article_preview(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(article.get("title", "")).strip(),
        "published_at": str(article.get("published_at", "")).strip(),
        "source_name": str(article.get("source_name", "")).strip(),
        "content_preview": truncate_text(article.get("content", ""), max_chars=100),
    }


def articles_preview_payload(
    *,
    label: str,
    input_payload: Any,
    articles: list[dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    preview_items = [article_preview(article) for article in articles[:limit]]
    return {
        "label": label,
        "input": input_payload,
        "articles": preview_items,
    }


def reddit_article_to_post(article: dict[str, Any]) -> dict[str, Any]:
    raw_value = article.get("raw")
    subreddit_name = ""
    if isinstance(raw_value, dict):
        subreddit_name = str(raw_value.get("tags", "")).strip()
    return {
        "title": str(article.get("title", "")).strip(),
        "url": str(article.get("url", "")).strip(),
        "published_at": str(article.get("published_at", "")).strip(),
        "subreddit": subreddit_name,
        "content_preview": truncate_text(article.get("content", ""), max_chars=100),
    }


def youtube_video_preview(video: dict[str, Any]) -> dict[str, Any]:
    return {
        "video_id": str(video.get("video_id", "")).strip(),
        "video_title": str(video.get("video_title", "")).strip(),
        "channel_id": str(video.get("channel_id", "")).strip(),
        "channel_name": str(video.get("channel_name", "")).strip(),
    }
