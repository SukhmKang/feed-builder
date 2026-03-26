import json
from typing import Any

from pipeline import (
    And,
    Conditional,
    CustomBlock,
    FieldContainsCondition,
    FieldEqualsCondition,
    FieldExistsCondition,
    FieldMatchesRegexCondition,
    KeywordCondition,
    KeywordFilter,
    LLMCondition,
    LLMFilter,
    LengthCondition,
    Not,
    Or,
    PublishedAfterCondition,
    PublishedBeforeCondition,
    SemanticSimilarity,
    SimilarityScoreCondition,
    SourceNameCondition,
    SourceTypeCondition,
    SourceUrlCondition,
    Switch,
    TagCondition,
    TagExistsCondition,
    TagMatchesCondition,
)
from pipeline.core import Block, Condition

PIPELINE_SCHEMA_PROMPT = """
You must return a JSON array of pipeline blocks.

General rules:
- Return valid JSON only.
- The top-level value must be a list.
- Each item in the list must be one block object.
- Every block object must contain a string field called `type`.
- For `conditional`, `if_true` and `if_false` must each be lists of block objects.
- For `switch`, `branches` must be a list of branch objects with `condition` and `blocks`.
- Conditions are nested JSON objects with their own `type` field.

Block schema:

1. keyword_filter
{
  "type": "keyword_filter",
  "include": ["term1", "term2"],
  "exclude": ["term3"]
}

Rules:
- `include` is required and must be a non-empty array of strings.
- `exclude` is optional and must be an array of strings if present.
- Keyword matching always searches the full article text surface automatically.

2. semantic_similarity
{
  "type": "semantic_similarity",
  "query": "ideal article description in plain english",
  "field": "content",
  "threshold": 0.6,
  "embedding_model": "text-embedding-3-small"
}

Rules:
- `query` is required and must be a string.
- `field` is required and must be a string.
- `threshold` is optional and must be a number if present.
- `embedding_model` is optional and must be a string if present.

3. llm_filter
{
  "type": "llm_filter",
  "prompt": "Your prompt here. It must make the LLM return JSON with fields: pass (bool), criteria_met (list[str]), criteria_failed (list[str]), tags (list[str]), reasoning (str)",
  "tier": "mini"
}

Rules:
- `prompt` is required and must be a string.
- `tier` is optional and must be one of `mini`, `medium`, or `high` if present.

4. conditional
{
  "type": "conditional",
  "condition": { ...condition object... },
  "if_true": [ ...block objects... ],
  "if_false": [ ...block objects... ]
}

Rules:
- `condition` is required and must be a condition object.
- `if_true` is optional and defaults to [].
- `if_false` is optional and defaults to [].

5. switch
{
  "type": "switch",
  "branches": [
    {
      "condition": { "type": "source_type", "value": "reddit" },
      "blocks": [
        {
          "type": "llm_filter",
          "prompt": "Pass only if this Reddit post is a meaningful game update.",
          "tier": "mini"
        }
      ]
    }
  ],
  "default": []
}

Rules:
- `branches` is required and must be a non-empty array.
- Each branch must contain:
  - `condition`: a condition object
  - `blocks`: a list of block objects
- `default` is optional and defaults to [].

6. custom_block
{
  "type": "custom_block",
  "name": "pass_through"
}

Rules:
- `name` is required and must be a string.
- It must match a module name in the `custom_blocks` package.

Condition schema:

Logical operators:
{"type": "and", "conditions": [ ...condition objects... ]}
{"type": "or", "conditions": [ ...condition objects... ]}
{"type": "not", "condition": { ...condition object... }}

Leaf conditions:
{"type": "source_type", "value": "rss"}
{"type": "source_type", "value": "reddit"}
{"type": "source_type", "value": "youtube"}
{"type": "source_type", "value": "nitter"}
{"type": "source_name", "value": "Kotaku"}
{"type": "source_url", "value": "https://example.com/feed"}
{"type": "field_equals", "field": "source_name", "value": "Kotaku"}
{"type": "field_contains", "field": "title", "value": "steam deck"}
{"type": "field_exists", "field": "published_at"}
{"type": "field_matches_regex", "field": "title", "pattern": "(?i)review|preview"}
{"type": "tag_exists", "tag": "gaming"}
{"type": "tag_condition", "tag": "gaming", "operator": "has"}
{"type": "tag_condition", "tag": "sponsored", "operator": "not_has"}
{"type": "tag_matches", "pattern": "branch:*"}
{"type": "keyword", "terms": ["steam", "deck"], "operator": "all"}
{"type": "length", "field": "content", "min": 200, "max": 10000}
{"type": "published_after", "days_ago": 7}
{"type": "published_before", "days_ago": 30}
{"type": "similarity_score", "threshold": 0.8, "operator": "gt"}
{"type": "llm", "prompt": "Return true only if this article is clearly about handheld PC gaming hardware."}
{"type": "llm", "prompt": "Return true only if this article is clearly about handheld PC gaming hardware.", "tier": "mini"}

Complete example pipeline:
[
  {
    "type": "keyword_filter",
    "include": ["steam deck", "handheld pc"],
    "exclude": ["giveaway", "sponsored"]
  },
  {
    "type": "semantic_similarity",
    "query": "news and analysis about handheld gaming PCs and portable PC hardware",
    "field": "content",
    "threshold": 0.6
  },
  {
    "type": "conditional",
    "condition": {
      "type": "and",
      "conditions": [
        {"type": "source_type", "value": "rss"},
        {"type": "tag_exists", "tag": "steam deck"}
      ]
    },
    "if_true": [
      {
        "type": "llm_filter",
        "prompt": "Pass only if the article is primarily about handheld gaming PCs rather than general console news. Title: {title}\nContent: {content}\nSource: {source_name}\nTags: {tags}",
        "tier": "mini"
      }
    ],
    "if_false": []
  },
  {
    "type": "switch",
    "branches": [
      {
        "condition": {"type": "source_type", "value": "reddit"},
        "blocks": [
          {
            "type": "llm_filter",
            "prompt": "Pass only if this Reddit post is specifically about handheld PC gaming hardware rather than general gaming chatter. Title: {title}\nContent: {content}\nSource: {source_name}\nTags: {tags}",
            "tier": "mini"
          }
        ]
      },
      {
        "condition": {"type": "source_type", "value": "google_news"},
        "blocks": [
          {
            "type": "semantic_similarity",
            "query": "news and analysis about handheld gaming PCs and portable PC hardware",
            "field": "content",
            "threshold": 0.6
          }
        ]
      }
    ],
    "default": []
  },
  {
    "type": "custom_block",
    "name": "drop_short_content"
  }
]
""".strip()


def deserialize_pipeline(blocks_json: list[dict[str, Any]]) -> list[Block]:
    _validate_pipeline_definition(blocks_json)
    return [deserialize_block(block_json) for block_json in blocks_json]


def deserialize_block(block_json: dict[str, Any]) -> Block:
    _require_dict(block_json, "Block")
    block_type = _require_string(block_json, "type", "Block")

    if block_type == "keyword_filter":
        include = _require_string_list(block_json, "include", "keyword_filter", non_empty=True)
        exclude = _optional_string_list(block_json, "exclude", default=[])
        return KeywordFilter(include=include, exclude=exclude)

    if block_type == "semantic_similarity":
        query = _require_string(block_json, "query", "semantic_similarity")
        field = _require_string(block_json, "field", "semantic_similarity")
        threshold = _optional_number(block_json, "threshold", default=0.6)
        embedding_model = _optional_string(block_json, "embedding_model", default="text-embedding-3-small")
        return SemanticSimilarity(
            query=query,
            field=field,
            threshold=float(threshold),
            embedding_model=embedding_model,
        )

    if block_type == "llm_filter":
        prompt = _require_string(block_json, "prompt", "llm_filter")
        tier = _optional_string(block_json, "tier", default="mini")
        if tier not in {"mini", "medium", "high"}:
            raise ValueError("llm_filter.tier must be one of: mini, medium, high")
        return LLMFilter(
            prompt=prompt,
            tier=tier,
        )

    if block_type == "conditional":
        condition_json = _require_dict_value(block_json, "condition", "conditional")
        if_true_json = _optional_block_list(block_json, "if_true")
        if_false_json = _optional_block_list(block_json, "if_false")
        return Conditional(
            condition=deserialize_condition(condition_json),
            if_true=[deserialize_block(item) for item in if_true_json],
            if_false=[deserialize_block(item) for item in if_false_json],
        )

    if block_type == "switch":
        branches_json = _require_switch_branch_list(block_json, "branches", "switch")
        default_json = _optional_block_list(block_json, "default")
        branches: list[tuple[Condition, list[Block]]] = []
        for branch_json in branches_json:
            condition_json = _require_dict_value(branch_json, "condition", "switch branch")
            blocks_json = _optional_block_list(branch_json, "blocks")
            branches.append(
                (
                    deserialize_condition(condition_json),
                    [deserialize_block(item) for item in blocks_json],
                )
            )
        return Switch(
            branches=branches,
            default=[deserialize_block(item) for item in default_json],
        )

    if block_type == "custom_block":
        return CustomBlock(name=_require_string(block_json, "name", "custom_block"))

    raise ValueError(f"Unknown block type: {block_type}")


def deserialize_condition(condition_json: dict[str, Any]) -> Condition:
    _require_dict(condition_json, "Condition")
    condition_type = _require_string(condition_json, "type", "Condition")

    if condition_type == "and":
        conditions = _require_condition_list(condition_json, "conditions", "and")
        return And([deserialize_condition(item) for item in conditions])

    if condition_type == "or":
        conditions = _require_condition_list(condition_json, "conditions", "or")
        return Or([deserialize_condition(item) for item in conditions])

    if condition_type == "not":
        nested = _require_dict_value(condition_json, "condition", "not")
        return Not(deserialize_condition(nested))

    if condition_type == "source_type":
        return SourceTypeCondition(type=_require_string(condition_json, "value", "source_type"))

    if condition_type == "source_name":
        return SourceNameCondition(name=_require_string(condition_json, "value", "source_name"))

    if condition_type == "source_url":
        return SourceUrlCondition(url=_require_string(condition_json, "value", "source_url"))

    if condition_type == "field_equals":
        return FieldEqualsCondition(
            field=_require_string(condition_json, "field", "field_equals"),
            value=_require_string(condition_json, "value", "field_equals"),
        )

    if condition_type == "field_contains":
        return FieldContainsCondition(
            field=_require_string(condition_json, "field", "field_contains"),
            value=_require_string(condition_json, "value", "field_contains"),
        )

    if condition_type == "field_exists":
        return FieldExistsCondition(field=_require_string(condition_json, "field", "field_exists"))

    if condition_type == "field_matches_regex":
        return FieldMatchesRegexCondition(
            field=_require_string(condition_json, "field", "field_matches_regex"),
            pattern=_require_string(condition_json, "pattern", "field_matches_regex"),
        )

    if condition_type == "tag_exists":
        return TagExistsCondition(tag=_require_string(condition_json, "tag", "tag_exists"))

    if condition_type == "tag_condition":
        operator = _require_string(condition_json, "operator", "tag_condition")
        if operator not in {"has", "not_has"}:
            raise ValueError("tag_condition.operator must be 'has' or 'not_has'")
        return TagCondition(
            tag=_require_string(condition_json, "tag", "tag_condition"),
            operator=operator,
        )

    if condition_type == "tag_matches":
        return TagMatchesCondition(pattern=_require_string(condition_json, "pattern", "tag_matches"))

    if condition_type == "keyword":
        terms = _require_string_list(condition_json, "terms", "keyword", non_empty=True)
        operator = _require_string(condition_json, "operator", "keyword")
        if operator not in {"any", "all"}:
            raise ValueError("keyword.operator must be 'any' or 'all'")
        return KeywordCondition(terms=terms, operator=operator)

    if condition_type == "length":
        return LengthCondition(
            field=_require_string(condition_json, "field", "length"),
            min=int(_require_number(condition_json, "min", "length")),
            max=int(_require_number(condition_json, "max", "length")),
        )

    if condition_type == "published_after":
        return PublishedAfterCondition(days_ago=int(_require_number(condition_json, "days_ago", "published_after")))

    if condition_type == "published_before":
        return PublishedBeforeCondition(days_ago=int(_require_number(condition_json, "days_ago", "published_before")))

    if condition_type == "similarity_score":
        operator = _require_string(condition_json, "operator", "similarity_score")
        if operator not in {"gt", "lt"}:
            raise ValueError("similarity_score.operator must be 'gt' or 'lt'")
        return SimilarityScoreCondition(
            threshold=float(_require_number(condition_json, "threshold", "similarity_score")),
            operator=operator,
        )

    if condition_type == "llm":
        tier = _optional_string(condition_json, "tier", default="mini")
        if tier not in {"mini", "medium", "high"}:
            raise ValueError("llm.tier must be one of: mini, medium, high")
        return LLMCondition(
            prompt=_require_string(condition_json, "prompt", "llm"),
            tier=tier,
        )

    raise ValueError(f"Unknown condition type: {condition_type}")


def is_valid_pipeline_definition(blocks_json: Any) -> bool:
    try:
        _validate_pipeline_definition(blocks_json)
    except ValueError:
        return False
    return True


def _validate_pipeline_definition(blocks_json: Any) -> None:
    if not isinstance(blocks_json, list):
        raise ValueError("Pipeline definition must be a list of block objects")
    for index, block_json in enumerate(blocks_json):
        try:
            deserialize_block(block_json)
        except ValueError as exc:
            raise ValueError(f"Invalid block at index {index}: {exc}") from exc


def _require_dict(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")


def _require_dict_value(mapping: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    if key not in mapping:
        raise ValueError(f"{label} is missing required field: {key}")
    value = mapping[key]
    if not isinstance(value, dict):
        raise ValueError(f"{label}.{key} must be an object")
    return value


def _require_string(mapping: dict[str, Any], key: str, label: str) -> str:
    if key not in mapping:
        raise ValueError(f"{label} is missing required field: {key}")
    value = mapping[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.{key} must be a non-empty string")
    return value


def _require_string_list(mapping: dict[str, Any], key: str, label: str, *, non_empty: bool = False) -> list[str]:
    if key not in mapping:
        raise ValueError(f"{label} is missing required field: {key}")
    value = mapping[key]
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{label}.{key} must be a list of non-empty strings")
    if non_empty and not value:
        raise ValueError(f"{label}.{key} must not be empty")
    return value


def _optional_string_list(mapping: dict[str, Any], key: str, *, default: list[str]) -> list[str]:
    if key not in mapping:
        return list(default)
    value = mapping[key]
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return value


def _optional_string(mapping: dict[str, Any], key: str, *, default: str) -> str:
    if key not in mapping:
        return default
    value = mapping[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _require_number(mapping: dict[str, Any], key: str, label: str) -> float:
    if key not in mapping:
        raise ValueError(f"{label} is missing required field: {key}")
    value = mapping[key]
    if not isinstance(value, (int, float)):
        raise ValueError(f"{label}.{key} must be a number")
    return float(value)


def _optional_number(mapping: dict[str, Any], key: str, *, default: float) -> float:
    if key not in mapping:
        return float(default)
    value = mapping[key]
    if not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    return float(value)


def _optional_dict(mapping: dict[str, Any], key: str, *, default: dict[str, Any]) -> dict[str, Any]:
    if key not in mapping:
        return dict(default)
    value = mapping[key]
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _optional_block_list(mapping: dict[str, Any], key: str) -> list[dict[str, Any]]:
    if key not in mapping:
        return []
    value = mapping[key]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{key} must be a list of block objects")
    return value


def _require_condition_list(mapping: dict[str, Any], key: str, label: str) -> list[dict[str, Any]]:
    if key not in mapping:
        raise ValueError(f"{label} is missing required field: {key}")
    value = mapping[key]
    if not isinstance(value, list) or not value or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{label}.{key} must be a non-empty list of condition objects")
    return value


def _require_switch_branch_list(mapping: dict[str, Any], key: str, label: str) -> list[dict[str, Any]]:
    if key not in mapping:
        raise ValueError(f"{label} is missing required field: {key}")
    value = mapping[key]
    if not isinstance(value, list) or not value or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{label}.{key} must be a non-empty list of branch objects")
    for item in value:
        if "condition" not in item:
            raise ValueError(f"{label}.{key} branch is missing required field: condition")
        if "blocks" not in item:
            raise ValueError(f"{label}.{key} branch is missing required field: blocks")
        if not isinstance(item["blocks"], list) or not all(isinstance(block, dict) for block in item["blocks"]):
            raise ValueError(f"{label}.{key}.blocks must be a list of block objects")
    return value


__all__ = [
    "PIPELINE_SCHEMA_PROMPT",
    "deserialize_block",
    "deserialize_condition",
    "deserialize_pipeline",
    "is_valid_pipeline_definition",
]
