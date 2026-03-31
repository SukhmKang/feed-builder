import asyncio
import json

from discover_feeds import discover_feeds_detailed


async def main() -> None:
    source_name = "Zawya"
    result = await discover_feeds_detailed(source_name)
    print(
        json.dumps(
            {
                "source_name": source_name,
                "homepage": result.homepage,
                "attempts_run": result.attempts_run,
                "feeds": [
                    {
                        "url": feed.url,
                        "strategy": feed.strategy,
                        "attempt": feed.attempt,
                        "feed_format": feed.feed_format,
                        "content_type": feed.content_type,
                    }
                    for feed in result.feeds
                ],
                "failures": result.failures,
            },
            indent=2,
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
