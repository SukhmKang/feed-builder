from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.pipeline.core import Block, Condition
from app.pipeline.schema_models import BlockSchema, ConditionSchema

_block_adapter: TypeAdapter[BlockSchema] = TypeAdapter(BlockSchema)
_condition_adapter: TypeAdapter[ConditionSchema] = TypeAdapter(ConditionSchema)

PIPELINE_SCHEMA_PROMPT = """
You must return a JSON array of pipeline blocks. Return valid JSON only — no markdown, no prose.

Valid article fields: title | content | full_text | source_name | source_type | url | published_at
Valid source types: rss | reddit | youtube | nitter | google_news | tavily

Blocks:
{"type": "keyword_filter", "include": ["term1", "term2"], "exclude": ["term3"]}
{"type": "semantic_similarity", "query": "plain english description", "field": "content", "threshold": 0.6}
{"type": "llm_filter", "prompt": "...", "tier": "mini"}  -- article title, source, and content are appended automatically
{"type": "regex_filter", "field": "title", "pattern": "(?i)review|analysis", "mode": "include"}
{"type": "conditional", "condition": {...}, "if_true": [...], "if_false": [...]}
{"type": "switch", "branches": [{"condition": {...}, "blocks": [...]}], "default": [...]}
{"type": "custom_block", "name": "pass_through"}

Notes:
- llm_filter: prompt ≤2500 chars; tier is mini|medium|high (default: mini).
- regex_filter: mode="include" passes if pattern matches; mode="exclude" passes if it does not.
- keyword_filter: include and exclude are both optional arrays; at least one must be non-empty.

Conditions:
{"type": "and", "conditions": [...]}
{"type": "or", "conditions": [...]}
{"type": "not", "condition": {...}}
{"type": "source_type", "value": "rss"}  -- also: reddit, youtube, nitter, google_news, tavily
{"type": "source_name", "value": "Kotaku"}
{"type": "source_url", "value": "https://example.com/feed"}
{"type": "domain", "value": "youtube.com"}
{"type": "source_domain", "value": "nintendolife.com"}
{"type": "field_equals", "field": "source_name", "value": "Kotaku"}
{"type": "field_contains", "field": "title", "value": "steam deck"}
{"type": "field_exists", "field": "published_at"}
{"type": "field_matches_regex", "field": "title", "pattern": "(?i)review|preview"}
{"type": "keyword", "terms": ["steam", "deck"], "operator": "all"}  -- operator: any|all
{"type": "length", "field": "content", "min": 200, "max": 10000}
{"type": "published_after", "days_ago": 7}
{"type": "published_before", "days_ago": 30}
{"type": "llm", "prompt": "Return true only if...", "tier": "mini"}  -- article title, source, and content are appended automatically
""".strip()


def deserialize_pipeline(blocks_json: list[dict[str, Any]]) -> list[Block]:
    _validate_pipeline_definition(blocks_json)
    return [deserialize_block(block_json) for block_json in blocks_json]


def deserialize_block(block_json: dict[str, Any]) -> Block:
    try:
        return _block_adapter.validate_python(block_json).to_runtime()
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


def deserialize_condition(condition_json: dict[str, Any]) -> Condition:
    try:
        return _condition_adapter.validate_python(condition_json).to_runtime()
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


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


__all__ = [
    "PIPELINE_SCHEMA_PROMPT",
    "deserialize_block",
    "deserialize_condition",
    "deserialize_pipeline",
    "is_valid_pipeline_definition",
]
