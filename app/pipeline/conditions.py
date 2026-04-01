import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from app.llm import generate_text
from app.pipeline.core import (
    DEFAULT_KEYWORD_FIELDS,
    Condition,
    collect_search_text,
    copy_article,
    dedupe_strings,
    ensure_tags,
    find_matching_terms,
    flatten_text,
    is_string_list,
    normalize_for_keyword_search,
    parse_article_datetime,
    tag_matches_pattern,
    value_exists,
)
from app.pipeline.llm_config import LLMTier, VALID_LLM_TIERS, resolve_tier_model

LLM_CONDITION_MAX_ATTEMPTS = 2
LLM_CONDITION_SCHEMA_EXAMPLE = {
    "boolean": True,
    "justification": "One line explanation.",
}

PROMPT_PLACEHOLDERS = ("title", "content", "source_name", "tags")


@dataclass(slots=True)
class SourceTypeCondition:
    """Match on `article["source_type"]`."""

    type: str

    async def evaluate(self, article: dict[str, Any]) -> bool:
        return str(article.get("source_type", "")).strip().lower() == self.type.strip().lower()


@dataclass(slots=True)
class SourceNameCondition:
    """Match on `article["source_name"]`."""

    name: str

    async def evaluate(self, article: dict[str, Any]) -> bool:
        return str(article.get("source_name", "")).strip().lower() == self.name.strip().lower()


@dataclass(slots=True)
class SourceUrlCondition:
    """Match on `article["source_url"]`."""

    url: str

    async def evaluate(self, article: dict[str, Any]) -> bool:
        return str(article.get("source_url", "")).strip() == self.url.strip()


@dataclass(slots=True)
class DomainCondition:
    """Match on the domain of `article["url"]`."""

    domain: str

    async def evaluate(self, article: dict[str, Any]) -> bool:
        return _normalized_domain(article.get("url")) == _normalized_domain(self.domain)


@dataclass(slots=True)
class SourceDomainCondition:
    """Match on the domain of `article["source_url"]`."""

    domain: str

    async def evaluate(self, article: dict[str, Any]) -> bool:
        return _normalized_domain(article.get("source_url")) == _normalized_domain(self.domain)


@dataclass(slots=True)
class FieldEqualsCondition:
    """Require `article[field] == value` using string comparison."""

    field: str
    value: str

    async def evaluate(self, article: dict[str, Any]) -> bool:
        return str(article.get(self.field, "")).strip() == self.value


@dataclass(slots=True)
class FieldContainsCondition:
    """Require a punctuation-insensitive substring match within `article[field]`."""

    field: str
    value: str

    async def evaluate(self, article: dict[str, Any]) -> bool:
        haystack = normalize_for_keyword_search(flatten_text(article.get(self.field)))
        needle = normalize_for_keyword_search(self.value)
        return bool(needle) and needle in haystack


@dataclass(slots=True)
class FieldExistsCondition:
    """Require that `article[field]` is present and non-empty."""

    field: str

    async def evaluate(self, article: dict[str, Any]) -> bool:
        return value_exists(article.get(self.field))


@dataclass(slots=True)
class FieldMatchesRegexCondition:
    """Require that `article[field]` matches a regex pattern."""

    field: str
    pattern: str

    async def evaluate(self, article: dict[str, Any]) -> bool:
        value = flatten_text(article.get(self.field))
        return re.search(self.pattern, value) is not None


@dataclass(slots=True)
class TagExistsCondition:
    """Require that the article contains an exact tag."""

    tag: str

    async def evaluate(self, article: dict[str, Any]) -> bool:
        tags = ensure_tags(copy_article(article))
        return self.tag in tags


@dataclass(slots=True)
class TagCondition:
    """Tag predicate with `has` / `not_has` operators."""

    tag: str
    operator: str = "has"

    async def evaluate(self, article: dict[str, Any]) -> bool:
        tags = ensure_tags(copy_article(article))
        if self.operator == "has":
            return self.tag in tags
        if self.operator == "not_has":
            return self.tag not in tags
        raise ValueError(f"Unsupported TagCondition operator: {self.operator}")


@dataclass(slots=True)
class TagMatchesCondition:
    """Require that at least one tag matches a glob pattern such as `branch:*`."""

    pattern: str

    async def evaluate(self, article: dict[str, Any]) -> bool:
        tags = ensure_tags(copy_article(article))
        return any(tag_matches_pattern(tag, self.pattern) for tag in tags)


@dataclass(slots=True)
class KeywordCondition:
    """Search title/content for one or more normalized keywords.

    Operators:
    - `any`: at least one term must match
    - `all`: every term must match
    """

    terms: list[str]
    operator: str = "any"

    async def evaluate(self, article: dict[str, Any]) -> bool:
        haystack = collect_search_text(article, DEFAULT_KEYWORD_FIELDS)
        matches = find_matching_terms(self.terms, haystack)
        if self.operator == "any":
            return bool(matches)
        if self.operator == "all":
            expected = dedupe_strings([term.strip() for term in self.terms if term.strip()])
            return len(matches) == len(expected)
        raise ValueError(f"Unsupported KeywordCondition operator: {self.operator}")


@dataclass(slots=True)
class LengthCondition:
    """Require that the character length of `article[field]` falls within `[min, max]`."""

    field: str
    min: int
    max: int

    async def evaluate(self, article: dict[str, Any]) -> bool:
        length = len(flatten_text(article.get(self.field)).strip())
        return self.min <= length <= self.max


@dataclass(slots=True)
class PublishedAfterCondition:
    """Require `published_at` to be within the last `days_ago` days."""

    days_ago: int

    async def evaluate(self, article: dict[str, Any]) -> bool:
        published_at = parse_article_datetime(article.get("published_at"))
        if published_at is None:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.days_ago)
        return published_at >= cutoff


@dataclass(slots=True)
class PublishedBeforeCondition:
    """Require `published_at` to be older than `days_ago` days."""

    days_ago: int

    async def evaluate(self, article: dict[str, Any]) -> bool:
        published_at = parse_article_datetime(article.get("published_at"))
        if published_at is None:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.days_ago)
        return published_at <= cutoff


@dataclass(slots=True)
class SimilarityScoreCondition:
    """Compare `article["similarity_score"]` against a threshold."""

    threshold: float
    operator: str = "gt"

    async def evaluate(self, article: dict[str, Any]) -> bool:
        score = article.get("similarity_score")
        if not isinstance(score, (int, float)):
            return False
        if self.operator == "gt":
            return float(score) > self.threshold
        if self.operator == "lt":
            return float(score) < self.threshold
        raise ValueError(f"Unsupported SimilarityScoreCondition operator: {self.operator}")


@dataclass(slots=True)
class LLMCondition:
    """Escape-hatch condition evaluated by an LLM returning JSON.

    Contract:
    - Uses a system prompt to enforce a fixed JSON schema
    - Uses a task prompt for the caller's prompt and article context
    - Validates the response locally and retries once if malformed
    - Expects a JSON object with `boolean` and `justification`
    """

    prompt: str
    tier: LLMTier = "mini"

    def __post_init__(self) -> None:
        if self.tier not in VALID_LLM_TIERS:
            raise ValueError(f"Unsupported LLMCondition tier: {self.tier}")

    async def evaluate(self, article: dict[str, Any]) -> bool:
        rendered_prompt = _render_prompt_template(
            self.prompt,
            {
                "title": str(article.get("title", "")),
                "content": str(article.get("content", "")),
                "source_name": str(article.get("source_name", "")),
                "tags": ", ".join(ensure_tags(copy_article(article))),
            },
        )

        validation_error = ""
        raw_response = ""
        parsed: dict[str, Any] | None = None
        for _ in range(LLM_CONDITION_MAX_ATTEMPTS):
            provider, model = resolve_tier_model(self.tier)
            raw_response = await generate_text(
                _build_llm_condition_task_prompt(rendered_prompt, validation_error, raw_response),
                provider=provider,
                model=model,
                max_tokens=400,
                system=_build_llm_condition_system_prompt(),
                json_output=True,
            )
            try:
                parsed = _validate_llm_condition_response(raw_response)
                break
            except ValueError as exc:
                validation_error = str(exc)

        if parsed is None:
            raise ValueError(f"LLMCondition returned malformed JSON after retry: {validation_error}")
        return bool(parsed["boolean"])


@dataclass(slots=True)
class And:
    """Pass only if every nested condition passes."""

    conditions: list[Condition]

    async def evaluate(self, article: dict[str, Any]) -> bool:
        for condition in self.conditions:
            if not await condition.evaluate(article):
                return False
        return True


@dataclass(slots=True)
class Or:
    """Pass if any nested condition passes."""

    conditions: list[Condition]

    async def evaluate(self, article: dict[str, Any]) -> bool:
        for condition in self.conditions:
            if await condition.evaluate(article):
                return True
        return False


@dataclass(slots=True)
class Not:
    """Invert a nested condition."""

    condition: Condition

    async def evaluate(self, article: dict[str, Any]) -> bool:
        return not await self.condition.evaluate(article)


def _normalized_domain(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    hostname = (parsed.hostname or "").strip().lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname


def _render_prompt_template(template: str, values: dict[str, str]) -> str:
    rendered = str(template)
    for placeholder in PROMPT_PLACEHOLDERS:
        rendered = rendered.replace("{" + placeholder + "}", values.get(placeholder, ""))
    return rendered


def _build_llm_condition_system_prompt() -> str:
    return "\n\n".join(
        [
            "You are a strict JSON boolean evaluator.",
            "Return JSON only.",
            "Your response must be a single JSON object that exactly matches this schema:",
            json.dumps(LLM_CONDITION_SCHEMA_EXAMPLE, indent=2),
            "Validation rules:",
            "- 'boolean' must be a boolean.",
            "- 'justification' must be a non-empty string.",
            "Do not include markdown fences, prose, or any text outside the JSON object.",
        ]
    )


def _build_llm_condition_task_prompt(rendered_prompt: str, validation_error: str, raw_response: str) -> str:
    prompt_parts = [
        "Use the following task instructions and article data to decide whether the condition passes.",
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


def _validate_llm_condition_response(raw_response: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Response was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("Response must be a JSON object")
    if not isinstance(parsed.get("boolean"), bool):
        raise ValueError("'boolean' must be a boolean")
    if not isinstance(parsed.get("justification"), str) or not parsed["justification"].strip():
        raise ValueError("'justification' must be a non-empty string")
    return parsed


__all__ = [
    "And",
    "Condition",
    "DomainCondition",
    "FieldContainsCondition",
    "FieldEqualsCondition",
    "FieldExistsCondition",
    "FieldMatchesRegexCondition",
    "KeywordCondition",
    "LLMCondition",
    "LengthCondition",
    "Not",
    "Or",
    "PublishedAfterCondition",
    "PublishedBeforeCondition",
    "SimilarityScoreCondition",
    "SourceDomainCondition",
    "SourceNameCondition",
    "SourceTypeCondition",
    "SourceUrlCondition",
    "TagCondition",
    "TagExistsCondition",
    "TagMatchesCondition",
]
