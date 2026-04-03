"""
CLI test script for app.sources.discover_feeds.

Usage:
    python test_discover_feeds.py "BBC" "The Verge" "Zawya"
    python test_discover_feeds.py --json "BBC" "Reuters"
"""

import argparse
import asyncio
import json
import sys
import time

import dotenv

dotenv.load_dotenv()

from app.sources.discover_feeds import discover_feeds_detailed


async def run(sources: list[str], as_json: bool) -> None:
    results = []

    for source in sources:
        print(f"\n{'='*60}", flush=True)
        print(f"  {source}", flush=True)
        print(f"{'='*60}", flush=True)

        t0 = time.monotonic()
        result = await discover_feeds_detailed(source)
        elapsed = time.monotonic() - t0

        if as_json:
            results.append({
                "source": source,
                "homepage": result.homepage,
                "attempts_run": result.attempts_run,
                "elapsed_seconds": round(elapsed, 2),
                "feeds": [
                    {
                        "url": f.url,
                        "strategy": f.strategy,
                        "feed_format": f.feed_format,
                    }
                    for f in result.feeds
                ],
                "failures": result.failures,
            })
        else:
            print(f"Homepage : {result.homepage or '(none)'}")
            print(f"Strategy : stopped at attempt {result.attempts_run}")
            print(f"Time     : {elapsed:.1f}s")
            if result.feeds:
                print(f"Feeds ({len(result.feeds)}):")
                for feed in result.feeds:
                    fmt = f"  [{feed.feed_format}]" if feed.feed_format else ""
                    print(f"  {feed.url}{fmt}  (via {feed.strategy})")
            else:
                print("Feeds    : none found")
            if result.failures:
                print(f"Failures : {len(result.failures)} URL(s) tried and rejected")

    if as_json:
        print(json.dumps(results, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test discover_feeds against one or more sources"
    )
    parser.add_argument("sources", nargs="+", help="Source name(s) to look up")
    parser.add_argument(
        "--json", action="store_true", help="Output results as JSON"
    )
    args = parser.parse_args()

    if not args.sources:
        parser.print_help()
        sys.exit(1)

    asyncio.run(run(args.sources, as_json=args.json))


if __name__ == "__main__":
    main()
