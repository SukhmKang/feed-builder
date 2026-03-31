import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline import run_pipeline
from pipeline_schema import deserialize_pipeline
from replay import replay_from_saved_output

REPLAY_PIPELINE_CONCURRENCY = 8


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_saved_blocks(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))

    final_config = payload.get("final_config")
    if isinstance(final_config, dict):
        blocks = final_config.get("blocks")
        if isinstance(blocks, list):
            return blocks

    blocks = payload.get("blocks_json")
    if isinstance(blocks, list):
        return blocks

    raise ValueError("Saved output did not contain pipeline blocks. Expected final_config.blocks or blocks_json.")


def _article_preview(article: dict[str, Any], *, dropped_at: str | None = None) -> dict[str, Any]:
    preview = {
        "title": str(article.get("title", "")).strip(),
        "url": str(article.get("url", "")).strip(),
        "published_at": str(article.get("published_at", "")).strip(),
        "source_name": str(article.get("source_name", "")).strip(),
        "source_type": str(article.get("source_type", "")).strip(),
    }
    if dropped_at:
        preview["dropped_at"] = dropped_at
    return preview


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Replay a saved pipeline run against historical articles")
    parser.add_argument("input", help="Path to output.json from run_pipeline_agent.py")
    parser.add_argument("--start", required=True, help="ISO datetime, e.g. 2026-03-01T00:00:00Z")
    parser.add_argument("--end", required=True, help="ISO datetime, e.g. 2026-03-31T23:59:59Z")
    parser.add_argument("--limit", type=int, default=10, help="How many passed/filtered examples to print")
    args = parser.parse_args()

    start = _parse_datetime(args.start)
    end = _parse_datetime(args.end)

    replay_result = await replay_from_saved_output(args.input, start, end)
    blocks_json = _load_saved_blocks(args.input)
    blocks = deserialize_pipeline(blocks_json)

    pipeline_results = await _run_pipeline_batch_limited(
        replay_result.articles,
        blocks,
        concurrency=REPLAY_PIPELINE_CONCURRENCY,
    )

    passed_articles: list[dict[str, Any]] = []
    filtered_articles: list[dict[str, Any]] = []
    dropped_at_counts: Counter[str] = Counter()
    passed_by_source_type: Counter[str] = Counter()
    filtered_by_source_type: Counter[str] = Counter()

    for result in pipeline_results:
        article = result["article"]
        source_type = str(article.get("source_type", "")).strip() or "unknown"
        if result["passed"]:
            passed_articles.append(article)
            passed_by_source_type[source_type] += 1
        else:
            filtered_articles.append(article)
            filtered_by_source_type[source_type] += 1
            dropped_at = str(result.get("dropped_at") or "").strip() or "unknown"
            dropped_at_counts[dropped_at] += 1

    summary = {
        "input": str(Path(args.input).expanduser()),
        "article_count": len(replay_result.articles),
        "passed_count": len(passed_articles),
        "filtered_count": len(filtered_articles),
        "skipped_sources": replay_result.skipped_sources,
        "passed_by_source_type": dict(passed_by_source_type),
        "filtered_by_source_type": dict(filtered_by_source_type),
        "dropped_at_counts": dict(dropped_at_counts),
        "passed_preview": [_article_preview(article) for article in passed_articles[: args.limit]],
        "filtered_preview": [
            _article_preview(result["article"], dropped_at=result.get("dropped_at"))
            for result in pipeline_results
            if not result["passed"]
        ][: args.limit],
    }

    print(json.dumps(summary, indent=2, ensure_ascii=True))


async def _run_pipeline_batch_limited(
    articles: list[dict[str, Any]],
    blocks: list[Any],
    *,
    concurrency: int,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    total = len(articles)
    completed = 0
    progress_lock = asyncio.Lock()
    results: list[dict[str, Any] | None] = [None] * total

    async def worker(index: int, article: dict[str, Any]) -> None:
        nonlocal completed
        async with semaphore:
            results[index] = await run_pipeline(article, blocks)
        async with progress_lock:
            completed += 1
            if completed == total or completed % 25 == 0:
                print(f"[test_pipeline_replay] pipeline_progress {completed}/{total}")

    await asyncio.gather(*[worker(index, article) for index, article in enumerate(articles)])
    return [result for result in results if result is not None]


if __name__ == "__main__":
    asyncio.run(_main())
