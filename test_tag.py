import asyncio
import json

from discover_feeds import _strategy_llm_fallback


async def main() -> None:
    candidates = await _strategy_llm_fallback(
        source_name="BBC",
        homepage="https://www.bbc.com",
        failures={},
        attempt=1,
        http_client=None,  # unused by this strategy
    )
    print(json.dumps({"candidates": candidates}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
