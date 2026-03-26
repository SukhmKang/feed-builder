import asyncio
import importlib
import json
from dataclasses import dataclass, field
from typing import Any

from llm import generate_text
from pipeline.core import (
    Block,
    BlockResult,
    collect_search_text,
    copy_article,
    cosine_similarity,
    embed_text,
    ensure_tags,
    find_matching_terms,
    is_string_list,
    merge_tags,
    run_pipeline,
)
from pipeline.conditions import Condition
from pipeline.llm_config import LLMTier, TIER_MAP, resolve_tier_model

"""
Pipeline block contracts.

Every block implements:
- `async def run(article: dict) -> BlockResult`
"""

LLM_FILTER_MAX_TOKENS = 800
LLM_FILTER_MAX_ATTEMPTS = 2
LLM_FILTER_SCHEMA_EXAMPLE = {
    "pass": True,
    "criteria_met": ["example criterion"],
    "criteria_failed": [],
    "tags": ["example-tag"],
    "reasoning": "One line explanation.",
}


@dataclass(slots=True)
class KeywordFilter:
    """Filter articles by keyword matches across selected fields.

    Contract:
    - At least one `include` term must match.
    - No `exclude` term may match.
    - Matching is case-insensitive and punctuation-insensitive.
    - Included matches are added to `article["tags"]`.
    """

    include: list[str]
    exclude: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.include:
            raise ValueError("KeywordFilter.include must contain at least one term")

    async def run(self, article: dict[str, Any]) -> BlockResult:
        """Run keyword inclusion/exclusion checks against the article."""

        working_article = copy_article(article)
        haystack = collect_search_text(working_article, ["title", "content", "full_text"])
        matched_include = find_matching_terms(self.include, haystack)
        matched_exclude = find_matching_terms(self.exclude, haystack)

        merge_tags(working_article, matched_include)

        if matched_exclude:
            return {
                "passed": False,
                "article": working_article,
                "reason": f"Excluded by keywords: {', '.join(matched_exclude)}",
            }

        if not matched_include:
            return {
                "passed": False,
                "article": working_article,
                "reason": "No include keywords matched across article content",
            }

        return {
            "passed": True,
            "article": working_article,
            "reason": f"Matched include keywords: {', '.join(matched_include)}",
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
    - Returned tags are merged into `article["tags"]`.
    """

    prompt: str
    tier: LLMTier = "mini"

    def __post_init__(self) -> None:
        if self.tier not in TIER_MAP:
            raise ValueError(f"Unsupported LLMFilter tier: {self.tier}")

    async def run(self, article: dict[str, Any]) -> BlockResult:
        """Run the prompt-driven JSON classifier and validate its output."""

        working_article = copy_article(article)
        rendered_prompt = self.prompt.format(
            title=working_article.get("title", ""),
            content=working_article.get("content", ""),
            source_name=working_article.get("source_name", ""),
            tags=", ".join(ensure_tags(working_article)),
        )

        validation_error = ""
        raw_response = ""
        parsed: dict[str, Any] | None = None

        for _ in range(LLM_FILTER_MAX_ATTEMPTS):
            system_prompt = _build_llm_filter_system_prompt()
            prompt = _build_llm_filter_task_prompt(rendered_prompt, validation_error, raw_response)
            provider, model = resolve_tier_model(self.tier)
            raw_response = await generate_text(
                prompt,
                provider=provider,
                model=model,
                max_tokens=LLM_FILTER_MAX_TOKENS,
                system=system_prompt,
                json_output=True,
            )
            try:
                parsed = _validate_llm_filter_response(raw_response)
                break
            except ValueError as exc:
                validation_error = str(exc)

        if parsed is None:
            raise ValueError(f"LLMFilter returned malformed JSON after retry: {validation_error}")

        merge_tags(working_article, parsed["tags"])
        reason = str(parsed["reasoning"]).strip()
        return {
            "passed": bool(parsed["pass"]),
            "article": working_article,
            "reason": reason,
        }


@dataclass(slots=True)
class Conditional:
    """Branch into one of two nested block lists based on a condition tree.

    Contract:
    - Adds either `branch:true` or `branch:false` to article tags.
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
        merge_tags(working_article, [branch_label])

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
    - Adds `switch:<index>` tags for matched branches and `switch:default` otherwise.
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

            merge_tags(working_article, [f"switch:{index}"])
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

        merge_tags(working_article, ["switch:default"])
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
class CustomBlock:
    """Load and execute a custom block from the `custom_blocks` package.

    Contract:
    - Loads `custom_blocks.<name>` at initialization time.
    - Expects that module to expose `async def run(article: dict) -> BlockResult`.
    - Delegates execution to that `run(...)` function unchanged.
    """

    name: str
    _fn: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._fn = self._load(self.name)

    def _load(self, name: str) -> Any:
        module = importlib.import_module(f"custom_blocks.{name}")
        run_fn = getattr(module, "run", None)
        if run_fn is None:
            raise ValueError(f"custom_blocks.{name} does not define a run(article) function")
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
            "- 'criteria_met', 'criteria_failed', and 'tags' must each be lists of strings.",
            "- 'reasoning' must be a non-empty string.",
            "- If 'pass' is true, 'criteria_met' must be non-empty.",
            "- If 'pass' is false, 'criteria_failed' must be non-empty.",
            "Do not include markdown fences, prose, or any text outside the JSON object.",
        ]
    )


def _build_llm_filter_task_prompt(rendered_prompt: str, validation_error: str, raw_response: str) -> str:
    prompt_parts = [
        "Use the following task instructions and article data to produce the JSON result.",
        rendered_prompt.strip(),
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

    required_keys = {"pass", "criteria_met", "criteria_failed", "tags", "reasoning"}
    missing_keys = required_keys.difference(parsed.keys())
    if missing_keys:
        raise ValueError(f"Response is missing keys: {', '.join(sorted(missing_keys))}")

    if not isinstance(parsed["pass"], bool):
        raise ValueError("'pass' must be a boolean")
    if not is_string_list(parsed["criteria_met"]):
        raise ValueError("'criteria_met' must be a list of strings")
    if not is_string_list(parsed["criteria_failed"]):
        raise ValueError("'criteria_failed' must be a list of strings")
    if not is_string_list(parsed["tags"]):
        raise ValueError("'tags' must be a list of strings")
    if not isinstance(parsed["reasoning"], str) or not parsed["reasoning"].strip():
        raise ValueError("'reasoning' must be a non-empty string")

    if parsed["pass"] and not parsed["criteria_met"]:
        raise ValueError("'criteria_met' must be non-empty when 'pass' is true")
    if not parsed["pass"] and not parsed["criteria_failed"]:
        raise ValueError("'criteria_failed' must be non-empty when 'pass' is false")

    return parsed


__all__ = [
    "CustomBlock",
    "Conditional",
    "KeywordFilter",
    "LLMFilter",
    "SemanticSimilarity",
    "Switch",
    "TIER_MAP",
]
