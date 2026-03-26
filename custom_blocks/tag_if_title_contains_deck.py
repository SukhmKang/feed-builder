"""Example custom block that tags Steam Deck-related articles by title."""

from typing import Any


async def run(article: dict[str, Any]) -> dict[str, Any]:
    working_article = dict(article)
    title = str(working_article.get("title", "")).lower()
    tags = list(working_article.get("tags", []))

    if "steam deck" in title and "steam-deck" not in tags:
        tags.append("steam-deck")
        working_article["tags"] = tags
        return {
            "passed": True,
            "article": working_article,
            "reason": "Tagged article as steam-deck based on title match",
        }

    working_article["tags"] = tags
    return {
        "passed": True,
        "article": working_article,
        "reason": "No steam deck title match",
    }
