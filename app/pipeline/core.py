import asyncio
import copy
import fnmatch
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, TypedDict

import openai
from dotenv import load_dotenv

load_dotenv()

"""
Pipeline core contracts.

Shared types:
- `BlockResult`
- `PipelineResult`
- `Block`
- `Condition`

Pipeline behavior:
- `run_pipeline(article, blocks)` runs blocks in order
- each block receives the enriched article from the previous block
- execution stops at the first block that returns `passed=False`
- tags and other enrichment fields accumulate on the article across blocks
"""

DEFAULT_KEYWORD_FIELDS = ["title", "content"]
logger = logging.getLogger(__name__)


class BlockResult(TypedDict):
    """Result returned by a single block execution."""

    passed: bool
    article: dict[str, Any]
    reason: str


class PipelineResult(TypedDict):
    """Result returned by the pipeline runner."""

    passed: bool
    article: dict[str, Any]
    block_results: list[BlockResult]
    dropped_at: str | None


class Block(Protocol):
    """Protocol shared by all pipeline blocks."""

    async def run(self, article: dict[str, Any]) -> BlockResult:
        """Evaluate one article and return a standardized block result."""

        ...


class Condition(Protocol):
    """Protocol shared by all composable conditional nodes.

    Every condition implements:
    - `async def evaluate(article: dict[str, Any]) -> bool`
    """

    async def evaluate(self, article: dict[str, Any]) -> bool:
        """Return whether the article satisfies this condition."""

        ...


async def run_pipeline(article: dict[str, Any], blocks: list[Block]) -> PipelineResult:
    """Run blocks in sequence until one fails or all pass.

    Input:
    - `article`: the article dict to evaluate
    - `blocks`: ordered list of block instances

    Output:
    - `passed`: whether the article passed the full pipeline
    - `article`: the fully enriched article after all executed blocks
    - `block_results`: one result per executed block
    - `dropped_at`: the block class name that first failed, else `None`
    """

    working_article = copy_article(article)
    block_results: list[BlockResult] = []
    article_id = str(working_article.get("id", "")).strip()
    article_title = str(working_article.get("title", "")).strip()

    for index, block in enumerate(blocks, start=1):
        logger.info(
            "Pipeline block start article_id=%s block_index=%s block_type=%s title=%r",
            article_id,
            index,
            block.__class__.__name__,
            article_title[:120],
        )
        result = await block.run(working_article)
        working_article = result["article"]
        block_results.append(result)
        logger.info(
            "Pipeline block result article_id=%s block_index=%s block_type=%s passed=%s reason=%r",
            article_id,
            index,
            block.__class__.__name__,
            result["passed"],
            str(result.get("reason", ""))[:300],
        )
        if not result["passed"]:
            logger.info(
                "Pipeline article dropped article_id=%s dropped_at=%s executed_blocks=%s",
                article_id,
                block.__class__.__name__,
                len(block_results),
            )
            return {
                "passed": False,
                "article": working_article,
                "block_results": block_results,
                "dropped_at": block.__class__.__name__,
            }

    logger.info(
        "Pipeline article passed article_id=%s executed_blocks=%s",
        article_id,
        len(block_results),
    )
    return {
        "passed": True,
        "article": working_article,
        "block_results": block_results,
        "dropped_at": None,
    }


_openai_client: openai.OpenAI | None = None


def get_openai_client() -> openai.OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.OpenAI()
    return _openai_client


async def embed_text(text: str, *, model: str) -> list[float]:
    normalized_text = normalize_for_embedding(text)
    if not normalized_text:
        normalized_text = " "

    client = get_openai_client()
    response = await asyncio.to_thread(
        client.embeddings.create,
        model=model,
        input=normalized_text,
    )
    return list(response.data[0].embedding)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Embedding vectors must have the same dimension")

    dot_product = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot_product / (left_norm * right_norm)


def is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def collect_search_text(article: dict[str, Any], fields: list[str]) -> str:
    parts: list[str] = []
    for field_name in fields:
        value = article.get(field_name)
        parts.append(flatten_text(value))
    return "\n".join(part for part in parts if part)


def find_matching_terms(terms: list[str], haystack: str) -> list[str]:
    normalized_haystack = normalize_for_keyword_search(haystack)
    matched: list[str] = []
    for term in terms:
        normalized_term = term.strip()
        searchable_term = normalize_for_keyword_search(normalized_term)
        if searchable_term and searchable_term in normalized_haystack:
            matched.append(normalized_term)
    return dedupe_strings(matched)


def flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(flatten_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(flatten_text(item) for item in value.values())
    return str(value)


def normalize_for_keyword_search(text: str) -> str:
    lowered = flatten_text(text).lower()
    punctuation_as_space = "".join(char if char.isalnum() or char.isspace() else " " for char in lowered)
    return " ".join(punctuation_as_space.split())


def normalize_for_embedding(text: str) -> str:
    flattened = flatten_text(text)
    punctuation_as_space = "".join(char if char.isalnum() or char.isspace() else " " for char in flattened)
    return " ".join(punctuation_as_space.split())


def value_exists(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def parse_article_datetime(value: Any) -> datetime | None:
    text = flatten_text(value).strip()
    if not text:
        return None

    try:
        parsed = parsedate_to_datetime(text)
    except Exception:
        parsed = None

    if parsed is not None:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def copy_article(article: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(article)


def ensure_tags(article: dict[str, Any]) -> list[str]:
    tags = article.get("tags")
    if not isinstance(tags, list):
        tags = []
        article["tags"] = tags
    return tags


def merge_tags(article: dict[str, Any], tags: list[str]) -> None:
    existing_tags = ensure_tags(article)
    deduped = dedupe_strings(existing_tags + [tag for tag in tags if tag and tag.strip()])
    article["tags"] = deduped


def dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def tag_matches_pattern(tag: str, pattern: str) -> bool:
    return fnmatch.fnmatch(tag, pattern)


__all__ = [
    "Block",
    "BlockResult",
    "Condition",
    "DEFAULT_KEYWORD_FIELDS",
    "PipelineResult",
    "collect_search_text",
    "copy_article",
    "cosine_similarity",
    "dedupe_strings",
    "embed_text",
    "ensure_tags",
    "find_matching_terms",
    "flatten_text",
    "is_string_list",
    "merge_tags",
    "normalize_for_embedding",
    "normalize_for_keyword_search",
    "parse_article_datetime",
    "run_pipeline",
    "tag_matches_pattern",
    "value_exists",
]
