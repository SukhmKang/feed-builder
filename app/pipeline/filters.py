import asyncio
import importlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.ai.llm import generate_text
from app.pipeline.core import (
    Block,
    BlockResult,
    collect_search_text,
    copy_article,
    cosine_similarity,
    embed_text,
    find_matching_terms,
    flatten_text,
    is_string_list,
    run_pipeline,
    truncate_for_llm_prompt,
)
from app.pipeline.conditions import Condition
from app.pipeline.llm_config import LLMTier, VALID_LLM_TIERS, resolve_tier_model

"""
Pipeline block contracts.

Every block implements:
- `async def run(article: dict) -> BlockResult`
"""

LLM_FILTER_MAX_TOKENS = 800
LLM_FILTER_MAX_ATTEMPTS = 2
LLM_FILTER_BATCH_MAX_TOKENS = 4000
LLM_FILTER_BATCH_MAX_CONCURRENCY = 3
LLM_FILTER_TITLE_MAX_CHARS = 300
LLM_FILTER_CONTENT_MAX_CHARS = 2500
LLM_FILTER_SOURCE_NAME_MAX_CHARS = 200
LLM_FILTER_BATCH_CONTENT_MAX_CHARS = 1200
LLM_FILTER_SCHEMA_EXAMPLE = {
    "pass": True,
    "criteria_met": ["example criterion"],
    "criteria_failed": [],
    "reasoning": "One line explanation.",
}
LLM_FILTER_BATCH_SCHEMA_EXAMPLE = {
    "results": [
        {
            "article_id": "abc123",
            "pass": True,
            "criteria_met": ["example criterion"],
            "criteria_failed": [],
            "reasoning": "One line explanation.",
        }
    ]
}

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class KeywordFilter:
    """Filter articles by keyword matches across selected fields.

    Contract:
    - If `include` is non-empty, at least one term must match.
    - No `exclude` term may match.
    - Matching is case-insensitive and punctuation-insensitive.
    """

    include: list[str]
    exclude: list[str] = field(default_factory=list)

    async def run(self, article: dict[str, Any]) -> BlockResult:
        """Run keyword inclusion/exclusion checks against the article."""

        working_article = copy_article(article)
        haystack = collect_search_text(working_article, ["title", "content", "full_text"])
        matched_include = find_matching_terms(self.include, haystack)
        matched_exclude = find_matching_terms(self.exclude, haystack)

        if matched_exclude:
            return {
                "passed": False,
                "article": working_article,
                "reason": f"Excluded by keywords: {', '.join(matched_exclude)}",
            }

        if self.include and not matched_include:
            return {
                "passed": False,
                "article": working_article,
                "reason": "No include keywords matched across article content",
            }

        return {
            "passed": True,
            "article": working_article,
            "reason": f"Matched include keywords: {', '.join(matched_include)}" if matched_include else "No exclude keywords matched",
        }


@dataclass(slots=True)
class SemanticSimilarity:
    """Score semantic similarity between an article and a target query.

    Contract:
    - Uses OpenAI embeddings.
    - Compares `query` against a caller-selected article field.
    - Stores the score in `article["similarity_score"]`.
    - Passes only when score >= `threshold`.
    """

    query: str
    field: str
    threshold: float = 0.6
    embedding_model: str = "text-embedding-3-small"

    async def run(self, article: dict[str, Any]) -> BlockResult:
        """Compute similarity, enrich the article, and return pass/fail."""

        working_article = copy_article(article)
        article_text = str(working_article.get(self.field, "")).strip()
        query_embedding, article_embedding = await asyncio.gather(
            embed_text(self.query, model=self.embedding_model),
            embed_text(article_text, model=self.embedding_model),
        )
        similarity = cosine_similarity(query_embedding, article_embedding)
        working_article["similarity_score"] = similarity

        passed = similarity >= self.threshold
        comparator = ">=" if passed else "<"
        return {
            "passed": passed,
            "article": working_article,
            "reason": f"Similarity {similarity:.3f} {comparator} threshold {self.threshold:.3f}",
        }


@dataclass(slots=True)
class LLMFilter:
    """Filter articles with an LLM that must return a fixed JSON schema.

    Contract:
    - The caller provides the task prompt template.
    - Output schema instructions are injected via the system prompt.
    - Article fields are interpolated into the user prompt.
    - JSON responses are validated locally and retried once if malformed.
    """

    prompt: str
    tier: LLMTier = "mini"
    batch_prompt: str | None = None
    batch_size: int = 10

    def __post_init__(self) -> None:
        if self.tier not in VALID_LLM_TIERS:
            raise ValueError(f"Unsupported LLMFilter tier: {self.tier}")
        if int(self.batch_size) < 1:
            raise ValueError("LLMFilter.batch_size must be at least 1")
        self.batch_size = int(self.batch_size)

    async def run(self, article: dict[str, Any]) -> BlockResult:
        """Run the prompt-driven JSON classifier and validate its output."""

        working_article = copy_article(article)
        article_id = str(working_article.get("id", "")).strip()
        article_title = str(working_article.get("title", "")).strip()
        article_context = _build_single_article_context(working_article)

        validation_error = ""
        raw_response = ""
        parsed: dict[str, Any] | None = None

        for _ in range(LLM_FILTER_MAX_ATTEMPTS):
            system_prompt = _build_llm_filter_system_prompt()
            prompt = _build_llm_filter_task_prompt(self.prompt, article_context, validation_error, raw_response)
            provider, model = resolve_tier_model(self.tier)
            logger.info(
                "LLMFilter request article_id=%s tier=%s provider=%s model=%s title=%r",
                article_id,
                self.tier,
                provider,
                model,
                article_title[:120],
            )
            logger.info("LLMFilter system prompt article_id=%s:\n%s", article_id, system_prompt)
            logger.info("LLMFilter final task prompt article_id=%s:\n%s", article_id, prompt)
            raw_response = await generate_text(
                prompt,
                provider=provider,
                model=model,
                max_tokens=LLM_FILTER_MAX_TOKENS,
                system=system_prompt,
                json_output=True,
            )
            logger.info("LLMFilter raw response article_id=%s:\n%s", article_id, raw_response)
            try:
                parsed = _validate_llm_filter_response(raw_response)
                break
            except ValueError as exc:
                validation_error = str(exc)
                logger.warning(
                    "LLMFilter response validation failed article_id=%s error=%s",
                    article_id,
                    validation_error,
                )

        if parsed is None:
            raise ValueError(f"LLMFilter returned malformed JSON after retry: {validation_error}")

        reason = str(parsed["reasoning"]).strip()
        return {
            "passed": bool(parsed["pass"]),
            "article": working_article,
            "reason": reason,
        }

    async def run_batch(self, articles: list[dict[str, Any]]) -> list[BlockResult]:
        """Run the filter against a batch of articles when a batch prompt is available."""

        if not articles:
            return []

        if not self.batch_prompt or self.batch_size <= 1 or len(articles) == 1:
            return await asyncio.gather(*[self.run(article) for article in articles])

        chunks = [articles[start : start + self.batch_size] for start in range(0, len(articles), self.batch_size)]
        semaphore = asyncio.Semaphore(LLM_FILTER_BATCH_MAX_CONCURRENCY)

        async def _run_chunk(chunk_articles: list[dict[str, Any]]) -> list[BlockResult]:
            async with semaphore:
                return await self._run_batch_chunk(chunk_articles)

        chunk_results = await asyncio.gather(*[_run_chunk(chunk) for chunk in chunks])
        flattened: list[BlockResult] = []
        for results in chunk_results:
            flattened.extend(results)
        return flattened

    async def _run_batch_chunk(self, articles: list[dict[str, Any]]) -> list[BlockResult]:
        working_articles = [copy_article(article) for article in articles]
        payload_articles = [_build_batch_article_payload(article, index) for index, article in enumerate(working_articles)]
        expected_ids = [item["article_id"] for item in payload_articles]

        validation_error = ""
        raw_response = ""
        parsed: dict[str, Any] | None = None

        for _ in range(LLM_FILTER_MAX_ATTEMPTS):
            system_prompt = _build_llm_filter_batch_system_prompt()
            prompt = _build_llm_filter_batch_task_prompt(
                batch_prompt=self.batch_prompt or "",
                articles_payload=payload_articles,
                validation_error=validation_error,
                raw_response=raw_response,
            )
            provider, model = resolve_tier_model(self.tier)
            logger.info(
                "LLMFilter batch request tier=%s provider=%s model=%s article_count=%s article_ids=%s",
                self.tier,
                provider,
                model,
                len(payload_articles),
                expected_ids,
            )
            logger.info("LLMFilter batch system prompt:\n%s", system_prompt)
            logger.info("LLMFilter batch prompt:\n%s", self.batch_prompt or "")
            logger.info("LLMFilter batch task prompt:\n%s", prompt)
            raw_response = await generate_text(
                prompt,
                provider=provider,
                model=model,
                max_tokens=LLM_FILTER_BATCH_MAX_TOKENS,
                system=system_prompt,
                json_output=True,
            )
            logger.info("LLMFilter batch raw response:\n%s", raw_response)
            try:
                parsed = _validate_llm_filter_batch_response(raw_response, expected_ids=expected_ids)
                break
            except ValueError as exc:
                validation_error = str(exc)
                logger.warning("LLMFilter batch response validation failed error=%s", validation_error)

        if parsed is None:
            logger.warning(
                "LLMFilter batch failed after retries; falling back to per-article execution for %s articles",
                len(articles),
            )
            return await asyncio.gather(*[self.run(article) for article in articles])

        results_by_id = {item["article_id"]: item for item in parsed["results"]}
        block_results: list[BlockResult] = []
        for article, payload in zip(working_articles, payload_articles, strict=False):
            item = results_by_id[payload["article_id"]]
            block_results.append(
                {
                    "passed": bool(item["pass"]),
                    "article": article,
                    "reason": str(item["reasoning"]).strip(),
                }
            )
        return block_results


@dataclass(slots=True)
class Conditional:
    """Branch into one of two nested block lists based on a condition tree.

    Contract:
    - Runs only the matching branch.
    - Pass/fail equals the result of the branch that ran.
    """

    condition: Condition
    if_true: list[Block] = field(default_factory=list)
    if_false: list[Block] = field(default_factory=list)

    async def run(self, article: dict[str, Any]) -> BlockResult:
        """Evaluate the condition and run the selected branch."""

        working_article = copy_article(article)
        branch_true = await self.condition.evaluate(working_article)
        branch_label = "branch:true" if branch_true else "branch:false"

        branch_blocks = self.if_true if branch_true else self.if_false
        branch_result = await run_pipeline(working_article, branch_blocks)
        branch_article = branch_result["article"]
        if not branch_blocks:
            return {
                "passed": True,
                "article": branch_article,
                "reason": f"{branch_label} with no nested blocks",
            }

        if branch_result["passed"]:
            return {
                "passed": True,
                "article": branch_article,
                "reason": f"{branch_label} passed {len(branch_blocks)} nested blocks",
            }

        dropped_at = branch_result["dropped_at"] or "unknown"
        return {
            "passed": False,
            "article": branch_article,
            "reason": f"{branch_label} dropped by {dropped_at}",
        }


@dataclass(slots=True)
class Switch:
    """Run the first matching branch from an ordered list of condition/block pairs.

    Contract:
    - Evaluates branches in order and runs only the first matching branch.
    - Falls back to `default` when no branch condition matches.
    - Pass/fail equals the result of the executed branch.
    """

    branches: list[tuple[Condition, list[Block]]]
    default: list[Block] = field(default_factory=list)

    async def run(self, article: dict[str, Any]) -> BlockResult:
        """Evaluate switch branches in order and run the selected branch."""

        working_article = copy_article(article)

        for index, (condition, blocks) in enumerate(self.branches, start=1):
            if not await condition.evaluate(working_article):
                continue

            branch_result = await run_pipeline(working_article, blocks)
            branch_article = branch_result["article"]
            if not blocks:
                return {
                    "passed": True,
                    "article": branch_article,
                    "reason": f"switch branch {index} matched with no nested blocks",
                }
            if branch_result["passed"]:
                return {
                    "passed": True,
                    "article": branch_article,
                    "reason": f"switch branch {index} passed {len(blocks)} nested blocks",
                }

            dropped_at = branch_result["dropped_at"] or "unknown"
            return {
                "passed": False,
                "article": branch_article,
                "reason": f"switch branch {index} dropped by {dropped_at}",
            }

        default_result = await run_pipeline(working_article, self.default)
        default_article = default_result["article"]
        if not self.default:
            return {
                "passed": True,
                "article": default_article,
                "reason": "switch default branch with no nested blocks",
            }
        if default_result["passed"]:
            return {
                "passed": True,
                "article": default_article,
                "reason": f"switch default branch passed {len(self.default)} nested blocks",
            }

        dropped_at = default_result["dropped_at"] or "unknown"
        return {
            "passed": False,
            "article": default_article,
            "reason": f"switch default branch dropped by {dropped_at}",
        }


@dataclass(slots=True)
class RegexFilter:
    """Filter articles by matching a regex pattern against a single field.

    Contract:
    - `mode="include"`: article passes only if the pattern matches.
    - `mode="exclude"`: article passes only if the pattern does NOT match.
    - Matching uses re.search (partial match, not full).
    - The `re.IGNORECASE` flag is always applied.
    """

    field: str
    pattern: str
    mode: str = "include"

    def __post_init__(self) -> None:
        if self.mode not in {"include", "exclude"}:
            raise ValueError("RegexFilter.mode must be 'include' or 'exclude'")
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise ValueError(f"RegexFilter.pattern is not a valid regex: {exc}") from exc

    async def run(self, article: dict[str, Any]) -> BlockResult:
        """Match the compiled pattern against the chosen field."""
        working_article = copy_article(article)
        value = flatten_text(working_article.get(self.field))
        matched = re.search(self.pattern, value, re.IGNORECASE) is not None

        if self.mode == "include":
            return {
                "passed": matched,
                "article": working_article,
                "reason": f"Pattern {self.pattern!r} {'matched' if matched else 'did not match'} {self.field!r}",
            }
        else:
            return {
                "passed": not matched,
                "article": working_article,
                "reason": f"Pattern {self.pattern!r} {'matched (excluded)' if matched else 'did not match (passed)'} {self.field!r}",
            }


@dataclass(slots=True)
class CustomBlock:
    """Load and execute a custom block from the `custom_blocks` package.

    Contract:
    - Loads `app.custom_blocks.<name>` at initialization time.
    - Expects that module to expose `async def run(article: dict) -> BlockResult`.
    - Delegates execution to that `run(...)` function unchanged.
    """

    name: str
    _fn: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._fn = self._load(self.name)

    def _load(self, name: str) -> Any:
        module = importlib.import_module(f"app.custom_blocks.{name}")
        run_fn = getattr(module, "run", None)
        if run_fn is None:
            raise ValueError(f"app.custom_blocks.{name} does not define a run(article) function")
        return run_fn

    async def run(self, article: dict[str, Any]) -> BlockResult:
        """Execute the loaded custom block implementation."""

        return await self._fn(article)


def _build_llm_filter_system_prompt() -> str:
    return "\n\n".join(
        [
            "You are a strict JSON classifier.",
            "Return JSON only.",
            "Your response must be a single JSON object that exactly matches this schema:",
            json.dumps(LLM_FILTER_SCHEMA_EXAMPLE, indent=2),
            "Validation rules:",
            "- 'pass' must be a boolean.",
            "- 'criteria_met' and 'criteria_failed' must each be lists of strings.",
            "- 'reasoning' must be a non-empty string.",
            "- If 'pass' is true, 'criteria_met' must be non-empty.",
            "- If 'pass' is false, 'criteria_failed' must be non-empty.",
            "Do not include markdown fences, prose, or any text outside the JSON object.",
        ]
    )


def _build_llm_filter_batch_system_prompt() -> str:
    return "\n\n".join(
        [
            "You are a strict JSON batch classifier.",
            "Return JSON only.",
            "You will classify a list of articles in one response.",
            "Your response must be a single JSON object that exactly matches this schema:",
            json.dumps(LLM_FILTER_BATCH_SCHEMA_EXAMPLE, indent=2),
            "Validation rules:",
            "- 'results' must be a list of objects.",
            "- Each result object must include: article_id, pass, criteria_met, criteria_failed, reasoning.",
            "- 'article_id' must exactly match one of the provided article ids.",
            "- 'pass' must be a boolean.",
            "- 'criteria_met' and 'criteria_failed' must each be lists of strings.",
            "- 'reasoning' must be a non-empty string.",
            "- If 'pass' is true, 'criteria_met' must be non-empty.",
            "- If 'pass' is false, 'criteria_failed' must be non-empty.",
            "- Return exactly one result per provided article id.",
            "Do not include markdown fences, prose, or any text outside the JSON object.",
        ]
    )


def _build_llm_filter_task_prompt(instructions: str, article_context: str, validation_error: str, raw_response: str) -> str:
    prompt_parts = [
        "Use the following task instructions and article data to produce the JSON result.",
        instructions.strip(),
        f"Article:\n{article_context}",
    ]

    if validation_error:
        prompt_parts.extend(
            [
                "Your previous response could not be parsed or did not conform to the required format.",
                f"Validation error: {validation_error}",
                "Rewrite the answer so it strictly matches the required JSON schema.",
                f"Previous response: {raw_response}",
            ]
        )

    return "\n\n".join(prompt_parts)


def _build_llm_filter_batch_task_prompt(
    *,
    batch_prompt: str,
    articles_payload: list[dict[str, Any]],
    validation_error: str,
    raw_response: str,
) -> str:
    prompt_parts = [
        "Use the following task instructions to classify the provided article list.",
        batch_prompt.strip(),
        "Articles JSON:",
        json.dumps(articles_payload, indent=2, ensure_ascii=True),
    ]

    if validation_error:
        prompt_parts.extend(
            [
                "Your previous response could not be parsed or did not conform to the required format.",
                f"Validation error: {validation_error}",
                "Rewrite the answer so it strictly matches the required JSON schema.",
                f"Previous response: {raw_response}",
            ]
        )

    return "\n\n".join(prompt_parts)


def _validate_llm_filter_response(raw_response: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Response was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("Response must be a JSON object")

    required_keys = {"pass", "criteria_met", "criteria_failed", "reasoning"}
    missing_keys = required_keys.difference(parsed.keys())
    if missing_keys:
        raise ValueError(f"Response is missing keys: {', '.join(sorted(missing_keys))}")

    if not isinstance(parsed["pass"], bool):
        raise ValueError("'pass' must be a boolean")
    if not is_string_list(parsed["criteria_met"]):
        raise ValueError("'criteria_met' must be a list of strings")
    if not is_string_list(parsed["criteria_failed"]):
        raise ValueError("'criteria_failed' must be a list of strings")
    if not isinstance(parsed["reasoning"], str) or not parsed["reasoning"].strip():
        raise ValueError("'reasoning' must be a non-empty string")

    if parsed["pass"] and not parsed["criteria_met"]:
        raise ValueError("'criteria_met' must be non-empty when 'pass' is true")
    if not parsed["pass"] and not parsed["criteria_failed"]:
        raise ValueError("'criteria_failed' must be non-empty when 'pass' is false")

    return parsed


def _validate_llm_filter_batch_response(raw_response: str, *, expected_ids: list[str]) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Response was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("Response must be a JSON object")

    results = parsed.get("results")
    if not isinstance(results, list):
        raise ValueError("'results' must be a list")

    seen_ids: set[str] = set()
    expected_id_set = set(expected_ids)
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            raise ValueError(f"results[{index}] must be an object")

        required_keys = {"article_id", "pass", "criteria_met", "criteria_failed", "reasoning"}
        missing_keys = required_keys.difference(item.keys())
        if missing_keys:
            raise ValueError(f"results[{index}] is missing keys: {', '.join(sorted(missing_keys))}")

        article_id = item["article_id"]
        if not isinstance(article_id, str) or not article_id.strip():
            raise ValueError(f"results[{index}].article_id must be a non-empty string")
        if article_id not in expected_id_set:
            raise ValueError(f"results[{index}].article_id was not in the requested batch: {article_id}")
        if article_id in seen_ids:
            raise ValueError(f"Duplicate article_id in batch response: {article_id}")
        seen_ids.add(article_id)

        if not isinstance(item["pass"], bool):
            raise ValueError(f"results[{index}].pass must be a boolean")
        if not is_string_list(item["criteria_met"]):
            raise ValueError(f"results[{index}].criteria_met must be a list of strings")
        if not is_string_list(item["criteria_failed"]):
            raise ValueError(f"results[{index}].criteria_failed must be a list of strings")
        if not isinstance(item["reasoning"], str) or not item["reasoning"].strip():
            raise ValueError(f"results[{index}].reasoning must be a non-empty string")
        if item["pass"] and not item["criteria_met"]:
            raise ValueError(f"results[{index}].criteria_met must be non-empty when pass is true")
        if not item["pass"] and not item["criteria_failed"]:
            raise ValueError(f"results[{index}].criteria_failed must be non-empty when pass is false")

    missing_ids = expected_id_set.difference(seen_ids)
    if missing_ids:
        raise ValueError(f"Batch response is missing article ids: {', '.join(sorted(missing_ids))}")

    return parsed


def _build_single_article_context(article: dict[str, Any]) -> str:
    parts = []
    if title := truncate_for_llm_prompt(article.get("title", ""), max_chars=LLM_FILTER_TITLE_MAX_CHARS):
        parts.append(f"Title: {title}")
    if source := truncate_for_llm_prompt(article.get("source_name", ""), max_chars=LLM_FILTER_SOURCE_NAME_MAX_CHARS):
        parts.append(f"Source: {source}")
    if content := truncate_for_llm_prompt(article.get("content", ""), max_chars=LLM_FILTER_CONTENT_MAX_CHARS):
        parts.append(f"Content:\n{content}")
    return "\n\n".join(parts)


def _build_batch_article_payload(article: dict[str, Any], position: int) -> dict[str, Any]:
    article_id = str(article.get("id", "")).strip() or f"batch-article-{position + 1}"
    return {
        "article_id": article_id,
        "title": truncate_for_llm_prompt(article.get("title", ""), max_chars=LLM_FILTER_TITLE_MAX_CHARS),
        "content": truncate_for_llm_prompt(article.get("content", ""), max_chars=LLM_FILTER_BATCH_CONTENT_MAX_CHARS),
        "source_name": truncate_for_llm_prompt(article.get("source_name", ""), max_chars=LLM_FILTER_SOURCE_NAME_MAX_CHARS),
    }


__all__ = [
    "CustomBlock",
    "Conditional",
    "KeywordFilter",
    "LLMFilter",
    "SemanticSimilarity",
    "Switch",
    "VALID_LLM_TIERS",
]
