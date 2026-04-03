"""Fetches articles from feed sources and runs them through the pipeline."""
import logging
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pipeline.core import parse_article_datetime  # noqa: E402
from app.pipeline.llm_batching import infer_pipeline_execution_chunk_size, run_pipeline_batch  # noqa: E402
from app.pipeline.schema import deserialize_pipeline  # noqa: E402
from app.sources.runner import fetch_articles  # noqa: E402

logger = logging.getLogger(__name__)


async def fetch_and_filter(config: dict[str, Any], *, max_article_age_hours: int | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    async for item in iter_fetch_and_filter(
        config,
        max_article_age_hours=max_article_age_hours,
    ):
        results.append(item)
    return results


async def iter_fetch_and_filter(
    config: dict[str, Any],
    *,
    max_article_age_hours: int | None = None,
    existing_article_ids: set[str] | None = None,
):
    """Fetch articles from sources and evaluate each through the pipeline.

    Returns a list of dicts:
      {
        "article": <normalized article dict>,
        "passed": bool,
        "pipeline_result": { passed, dropped_at, block_results: [{passed, reason}] }
      }
    """
    sources = config.get("sources", [])
    # Newer configs store pipeline definitions under "blocks". Keep the
    # older "pipeline" key as a fallback for legacy configs.
    pipeline_blocks_json = config.get("blocks")
    if not isinstance(pipeline_blocks_json, list):
        pipeline_blocks_json = config.get("pipeline", [])

    articles = await fetch_articles(sources)
    original_article_count = len(articles)
    if max_article_age_hours is not None:
        articles = _filter_recent_articles(articles, max_article_age_hours=max_article_age_hours)
        logger.info(
            "Article age prefilter kept %s of %s articles using max_age_hours=%s",
            len(articles),
            original_article_count,
            max_article_age_hours,
        )
    if existing_article_ids:
        before_dedup_count = len(articles)
        articles = _filter_existing_articles(articles, existing_article_ids=existing_article_ids)
        logger.info(
            "Article duplicate prefilter kept %s of %s articles using existing_article_ids=%s",
            len(articles),
            before_dedup_count,
            len(existing_article_ids),
        )
    blocks = deserialize_pipeline(pipeline_blocks_json)
    chunk_size = infer_pipeline_execution_chunk_size(pipeline_blocks_json)

    for chunk in _chunked(articles, chunk_size):
        chunk_results = await run_pipeline_batch(chunk, blocks)
        for pr in chunk_results:
            yield {
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


def _chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    normalized_size = max(int(size), 1)
    return [items[start : start + normalized_size] for start in range(0, len(items), normalized_size)]


def _filter_recent_articles(articles: list[dict[str, Any]], *, max_article_age_hours: int) -> list[dict[str, Any]]:
    normalized_hours = max(int(max_article_age_hours), 1)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=normalized_hours)
    filtered: list[dict[str, Any]] = []
    skipped_count = 0

    for article in articles:
        published_at = parse_article_datetime(article.get("published_at"))
        if published_at is None:
            filtered.append(article)
            continue
        if published_at >= cutoff:
            filtered.append(article)
            continue
        skipped_count += 1

    if skipped_count:
        logger.info(
            "Skipped %s stale articles older than %s hours before pipeline execution",
            skipped_count,
            normalized_hours,
        )
    return filtered


def _filter_existing_articles(
    articles: list[dict[str, Any]],
    *,
    existing_article_ids: set[str],
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    skipped_count = 0

    for article in articles:
        article_id = str(article.get("id", "")).strip()
        if article_id and article_id in existing_article_ids:
            skipped_count += 1
            continue
        filtered.append(article)

    if skipped_count:
        logger.info(
            "Skipped %s already-stored articles before pipeline execution",
            skipped_count,
        )
    return filtered
