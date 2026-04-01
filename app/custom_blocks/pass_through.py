"""Example custom block that always passes the article through unchanged."""

from typing import Any


async def run(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "passed": True,
        "article": dict(article),
        "reason": "Pass-through custom block",
    }
