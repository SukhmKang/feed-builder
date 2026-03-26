import argparse
import asyncio
import json
from datetime import datetime, timezone

from replay import replay_articles


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", required=True, help="Source type")
    parser.add_argument("--feed", required=True, help="Source feed/query")
    parser.add_argument("--start", required=True, help="ISO datetime, e.g. 2026-03-01T00:00:00Z")
    parser.add_argument("--end", required=True, help="ISO datetime, e.g. 2026-03-31T23:59:59Z")
    parser.add_argument("--limit", type=int, default=5, help="How many articles to print")
    args = parser.parse_args()

    result = await replay_articles(
        sources=[{"type": args.type, "feed": args.feed}],
        start=parse_datetime(args.start),
        end=parse_datetime(args.end),
    )

    print(
        json.dumps(
            {
                "source": {"type": args.type, "feed": args.feed},
                "article_count": len(result.articles),
                "skipped_sources": result.skipped_sources,
                "articles": [
                    {
                        "id": article.get("id"),
                        "title": article.get("title"),
                        "url": article.get("url"),
                        "published_at": article.get("published_at"),
                        "source_name": article.get("source_name"),
                        "source_type": article.get("source_type"),
                    }
                    for article in result.articles[: args.limit]
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
