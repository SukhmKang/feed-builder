import asyncio
from typing import Any

from app.pipeline import run_pipeline
from app.pipeline_schema import deserialize_pipeline
from app.runner import fetch_articles

from .logging import article_log_summary, log
from .types import SourceAgentOutput


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

    results = await asyncio.gather(*[run_pipeline(article, blocks) for article in articles])
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
        *[fetch_articles([source]) for source in selected_sources],
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
) -> None:
    for source in sources:
        log(verbose, "source_validation.start", {"label": label, "source": source})
        try:
            articles = await fetch_articles([source])
        except Exception as exc:
            raise ValueError(f"{label} failed validation for {source['type']}:{source['feed']}: {exc}") from exc
        if not articles:
            raise ValueError(f"{label} returned no articles during validation for {source['type']}:{source['feed']}")
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
