import asyncio
from typing import Any

from app.pipeline.llm_batching import run_pipeline_batch
from app.pipeline.schema import deserialize_pipeline

from app.agents.cache import fetch_articles_cached

from .logging import article_log_summary, log
from .types import SourceAgentOutput

SOURCE_VALIDATION_TIMEOUT_SECONDS = 600.0


async def evaluate_pipeline(
    selected_sources: list[dict[str, str]],
    blocks_json: list[dict[str, Any]],
    *,
    verbose: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blocks = deserialize_pipeline(blocks_json)
    log(verbose, "evaluation.sources", selected_sources)
    articles = await fetch_articles_for_evaluation(selected_sources, verbose=verbose)
    log(verbose, "evaluation.article_preview", [article_log_summary(article) for article in articles[:10]])

    results = await run_pipeline_batch(articles, blocks)
    passed: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    for article, result in zip(articles, results, strict=False):
        enriched_article = result["article"]
        if result["passed"]:
            passed.append(enriched_article)
        else:
            filtered.append(enriched_article)
    return passed, filtered


async def fetch_articles_for_evaluation(
    selected_sources: list[dict[str, str]],
    *,
    verbose: bool,
) -> list[dict[str, Any]]:
    fetched_batches = await asyncio.gather(
        *[fetch_articles_cached([source]) for source in selected_sources],
        return_exceptions=True,
    )

    articles: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for source, result in zip(selected_sources, fetched_batches, strict=False):
        if isinstance(result, Exception):
            error_payload = {
                "type": source["type"],
                "feed": source["feed"],
                "error_type": type(result).__name__,
                "error": str(result),
            }
            errors.append(error_payload)
            log(verbose, "evaluation.source_fetch_error", error_payload)
            continue
        articles.extend(result)

    if errors:
        log(
            verbose,
            "evaluation.source_fetch_summary",
            {
                "source_count": len(selected_sources),
                "success_count": len(selected_sources) - len(errors),
                "error_count": len(errors),
            },
        )

    if not articles:
        raise ValueError("Evaluation could not fetch articles from any selected source")

    return articles


async def validate_live_sources(
    sources: list[dict[str, str]],
    *,
    label: str,
    verbose: bool,
    max_concurrent: int = 3,
) -> tuple[list[dict[str, str]], list[tuple[dict[str, str], str]]]:
    """Validate sources concurrently.

    Returns (valid_sources, [(failed_source, reason), ...]).
    Does not raise — callers decide how to handle failures.

    A source is considered valid when it can be fetched successfully, even if
    it currently returns zero articles. Validation here is meant to catch
    structurally bad sources (unknown source types, missing feeds, nonexistent
    channels/subreddits, broken URLs, etc.), not transient emptiness.
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _validate_one(source: dict[str, str]) -> tuple[dict[str, str], str | None]:
        async with semaphore:
            log(verbose, "source_validation.start", {"label": label, "source": source})
            try:
                articles = await fetch_articles_cached([source])
            except Exception as exc:
                log(verbose, "source_validation.error", {"source": source, "error": str(exc)})
                return source, str(exc)
            if not articles:
                log(
                    verbose,
                    "source_validation.empty_but_valid",
                    {"label": label, "source": source},
                )
                return source, None
            log(
                verbose,
                "source_validation.success",
                {
                    "label": label,
                    "source": source,
                    "article_count": len(articles),
                    "preview": [article_log_summary(article) for article in articles[:3]],
                },
            )
            return source, None

    log(
        verbose,
        "source_validation.batch_start",
        {
            "label": label,
            "source_count": len(sources),
            "timeout_seconds": SOURCE_VALIDATION_TIMEOUT_SECONDS,
        },
    )
    try:
        outcomes = await asyncio.wait_for(
            asyncio.gather(*[_validate_one(s) for s in sources]),
            timeout=SOURCE_VALIDATION_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        log(
            verbose,
            "source_validation.batch_timeout",
            {
                "label": label,
                "source_count": len(sources),
                "timeout_seconds": SOURCE_VALIDATION_TIMEOUT_SECONDS,
            },
        )
        raise TimeoutError(
            f"{label} validation timed out after {SOURCE_VALIDATION_TIMEOUT_SECONDS:.0f}s"
        ) from exc

    valid: list[dict[str, str]] = []
    failed: list[tuple[dict[str, str], str]] = []
    for source, error in outcomes:
        if error is None:
            valid.append(source)
        else:
            failed.append((source, error))
    log(
        verbose,
        "source_validation.batch_done",
        {
            "label": label,
            "valid_count": len(valid),
            "failed_count": len(failed),
        },
    )
    return valid, failed


def merge_source_agent_outputs(source_agent_outputs: list[SourceAgentOutput]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for output in source_agent_outputs:
        for source in output["sources"]:
            key = (source["type"], source["feed"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(source)
    return merged
