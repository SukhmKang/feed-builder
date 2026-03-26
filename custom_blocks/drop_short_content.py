"""Example custom block that drops articles with very short content."""

from typing import Any


MIN_CONTENT_LENGTH = 120


async def run(article: dict[str, Any]) -> dict[str, Any]:
    working_article = dict(article)
    content = str(working_article.get("content", "")).strip()
    if len(content) < MIN_CONTENT_LENGTH:
        return {
            "passed": False,
            "article": working_article,
            "reason": f"Content length {len(content)} is below minimum {MIN_CONTENT_LENGTH}",
        }

    return {
        "passed": True,
        "article": working_article,
        "reason": f"Content length {len(content)} meets minimum {MIN_CONTENT_LENGTH}",
    }
