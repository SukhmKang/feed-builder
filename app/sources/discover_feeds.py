import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx
from dotenv import load_dotenv

from app.sources._feed_validator import (
    FEED_REQUEST_TIMEOUT,
    DiscoveredFeed,
    _validate_candidates,
)
from app.sources._homepage_resolver import _resolve_homepage
from app.sources._strategies import (
    _strategy_common_paths,
    _strategy_feedspot,
    _strategy_llm_fallback,
    _strategy_reddit_search,
    _strategy_site_autodiscovery,
    _strategy_site_search,
    _strategy_site_search_own_domain,
    _strategy_third_party_mentions,
)

load_dotenv()


@dataclass
class DiscoverFeedsResult:
    feeds: list[DiscoveredFeed]
    homepage: str | None
    attempts_run: int
    failures: dict[str, str]


@dataclass(frozen=True)
class DiscoveryStrategy:
    name: str
    description: str
    get_candidates: Callable[[str, str | None, dict[str, str], httpx.AsyncClient], Awaitable[list[str]]]


async def discover_feeds_detailed(source_name: str) -> DiscoverFeedsResult:
    attempted_urls: set[str] = set()
    failures: dict[str, str] = {}
    homepage = await _resolve_homepage(source_name)
    strategies = [
        DiscoveryStrategy(
            name="site_search",
            description="Same-site search-engine candidates for likely feed endpoints",
            get_candidates=_strategy_site_search,
        ),
        DiscoveryStrategy(
            name="site_autodiscovery",
            description="First-party website autodiscovery links",
            get_candidates=_strategy_site_autodiscovery,
        ),
        DiscoveryStrategy(
            name="common_paths",
            description="First-party common RSS/Atom path probing",
            get_candidates=_strategy_common_paths,
        ),
        DiscoveryStrategy(
            name="feedspot",
            description="Feedspot directory pages that expose canonical site URLs",
            get_candidates=_strategy_feedspot,
        ),
        DiscoveryStrategy(
            name="site_search_own_domain",
            description="Search the source's own domain for pages mentioning feed URLs",
            get_candidates=_strategy_site_search_own_domain,
        ),
        DiscoveryStrategy(
            name="reddit_search",
            description="Reddit posts and comments that mention feed URLs for the source",
            get_candidates=_strategy_reddit_search,
        ),
        DiscoveryStrategy(
            name="third_party_mentions",
            description="Third-party mentions on the broader web",
            get_candidates=_strategy_third_party_mentions,
        ),
        DiscoveryStrategy(
            name="llm_fallback",
            description="Final lightweight LLM fallback for likely native feed URLs",
            get_candidates=_strategy_llm_fallback,
        ),
    ]

    # Strategies up to and including site_search_own_domain accumulate feeds
    # across all attempts. After that checkpoint, return if anything was found.
    # The remaining strategies (reddit, third_party, llm_fallback) only run
    # when we still have zero feeds, and exit as soon as one of them succeeds.
    ACCUMULATE_THROUGH = "site_search_own_domain"
    accumulate_through_idx = next(
        i for i, s in enumerate(strategies, start=1) if s.name == ACCUMULATE_THROUGH
    )

    async with httpx.AsyncClient(follow_redirects=True, timeout=FEED_REQUEST_TIMEOUT) as http_client:
        accumulated: list[DiscoveredFeed] = []
        for strategy_index, strategy in enumerate(strategies, start=1):
            candidates = await strategy.get_candidates(
                source_name=source_name,
                homepage=homepage,
                failures=failures,
                http_client=http_client,
            )
            verified = await _validate_candidates(
                candidates,
                attempted_urls,
                failures,
                http_client,
                strategy_name=strategy.name,
                strategy_index=strategy_index,
            )
            accumulated.extend(verified)

            if strategy_index == accumulate_through_idx:
                if accumulated:
                    return DiscoverFeedsResult(
                        feeds=accumulated,
                        homepage=homepage,
                        attempts_run=strategy_index,
                        failures=failures.copy(),
                    )
            elif strategy_index > accumulate_through_idx and accumulated:
                return DiscoverFeedsResult(
                    feeds=accumulated,
                    homepage=homepage,
                    attempts_run=strategy_index,
                    failures=failures.copy(),
                )

    return DiscoverFeedsResult(
        feeds=[],
        homepage=homepage,
        attempts_run=len(strategies),
        failures=failures.copy(),
    )


async def discover_feeds(source_name: str) -> list[str]:
    result = await discover_feeds_detailed(source_name)
    return [feed.url for feed in result.feeds]


async def main():
    result = await discover_feeds_detailed("Pitchfork")
    print(json.dumps(
        {
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
    ))


if __name__ == "__main__":
    asyncio.run(main())
