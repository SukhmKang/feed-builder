"""Fetches articles from feed sources and runs them through the pipeline."""

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pipeline.core import run_pipeline  # noqa: E402
from app.pipeline_schema import deserialize_pipeline  # noqa: E402
from app.runner import fetch_articles  # noqa: E402


async def fetch_and_filter(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch articles from sources and evaluate each through the pipeline.

    Returns a list of dicts:
      {
        "article": <normalized article dict>,
        "passed": bool,
        "pipeline_result": { passed, dropped_at, block_results: [{passed, reason}] }
      }
    """
    sources = config.get("sources", [])
    pipeline_blocks_json = config.get("pipeline", [])

    articles = await fetch_articles(sources)
    blocks = deserialize_pipeline(pipeline_blocks_json)

    results: list[dict[str, Any]] = []
    for article in articles:
        pr = await run_pipeline(article, blocks)
        results.append(
            {
                "article": pr["article"],
                "passed": pr["passed"],
                "pipeline_result": {
                    "passed": pr["passed"],
                    "dropped_at": pr["dropped_at"],
                    "block_results": [
                        {"passed": br["passed"], "reason": br["reason"]}
                        for br in pr["block_results"]
                    ],
                },
            }
        )
    return results
