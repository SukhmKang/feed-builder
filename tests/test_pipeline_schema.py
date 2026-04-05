"""Tests for pipeline schema deserialization and prompt/model drift detection."""

import json
import re
import asyncio

import pytest

from app.pipeline.schema import PIPELINE_SCHEMA_PROMPT, deserialize_block, deserialize_condition, deserialize_pipeline, is_valid_pipeline_definition
from app.pipeline.filters import KeywordFilter, LLMFilter, RegexFilter, SemanticSimilarity, Conditional, Switch, CustomBlock
from app.pipeline.conditions import (
    And,
    SourceTypeCondition,
    LLMCondition,
)


# ─── Prompt / model drift detection ──────────────────────────────────────────

def _extract_json_objects_from_prompt(prompt: str) -> list[dict]:
    """Pull out every top-level {...} JSON object from the prompt text."""
    objects = []
    for match in re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}', prompt, re.DOTALL):
        text = match.group()
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and "type" in obj:
                objects.append(obj)
        except json.JSONDecodeError:
            pass
    return objects


def test_prompt_block_examples_are_valid():
    """Every block JSON example in PIPELINE_SCHEMA_PROMPT must deserialize without error."""
    examples = _extract_json_objects_from_prompt(PIPELINE_SCHEMA_PROMPT)
    block_examples = [e for e in examples if e.get("type") not in {
        "and", "or", "not", "source_type", "source_name", "source_url", "domain",
        "source_domain", "field_equals", "field_contains", "field_exists",
        "field_matches_regex", "keyword", "length", "published_after", "published_before", "llm",
    }]
    assert block_examples, "No block examples found in PIPELINE_SCHEMA_PROMPT"
    for example in block_examples:
        # Should not raise
        deserialize_block(example)


def test_prompt_condition_examples_are_valid():
    """Every condition JSON example in PIPELINE_SCHEMA_PROMPT must deserialize without error."""
    condition_types = {
        "and", "or", "not", "source_type", "source_name", "source_url", "domain",
        "source_domain", "field_equals", "field_contains", "field_exists",
        "field_matches_regex", "keyword", "length", "published_after", "published_before", "llm",
    }
    examples = _extract_json_objects_from_prompt(PIPELINE_SCHEMA_PROMPT)
    condition_examples = [e for e in examples if e.get("type") in condition_types]
    assert condition_examples, "No condition examples found in PIPELINE_SCHEMA_PROMPT"
    for example in condition_examples:
        deserialize_condition(example)


def test_prompt_documents_all_block_types():
    """PIPELINE_SCHEMA_PROMPT must mention every block type known to the schema."""
    from pydantic import TypeAdapter
    from app.pipeline.schema_models import BlockSchema
    import typing

    args = typing.get_args(typing.get_args(BlockSchema)[0])
    known_types = {typing.get_args(m.model_fields["type"].annotation)[0] for m in args}

    for block_type in known_types:
        assert block_type in PIPELINE_SCHEMA_PROMPT, (
            f"Block type '{block_type}' is missing from PIPELINE_SCHEMA_PROMPT"
        )


# ─── Block deserialization ────────────────────────────────────────────────────

def test_deserialize_keyword_filter():
    block = deserialize_block({"type": "keyword_filter", "include": ["steam deck"]})
    assert isinstance(block, KeywordFilter)
    assert block.include == ["steam deck"]
    assert block.exclude == []


def test_deserialize_keyword_filter_missing_include():
    with pytest.raises(ValueError):
        deserialize_block({"type": "keyword_filter"})


def test_deserialize_keyword_filter_empty_include():
    with pytest.raises(ValueError):
        deserialize_block({"type": "keyword_filter", "include": []})


def test_deserialize_llm_filter():
    block = deserialize_block({"type": "llm_filter", "prompt": "Pass if relevant", "tier": "medium"})
    assert isinstance(block, LLMFilter)
    assert block.tier == "medium"


def test_deserialize_llm_filter_invalid_tier():
    with pytest.raises(ValueError):
        deserialize_block({"type": "llm_filter", "prompt": "test", "tier": "ultra"})


def test_deserialize_regex_filter_include():
    block = deserialize_block({"type": "regex_filter", "field": "title", "pattern": "(?i)review"})
    assert isinstance(block, RegexFilter)
    assert block.mode == "include"


def test_deserialize_regex_filter_exclude():
    block = deserialize_block({"type": "regex_filter", "field": "title", "pattern": "sponsored", "mode": "exclude"})
    assert isinstance(block, RegexFilter)
    assert block.mode == "exclude"


def test_deserialize_regex_filter_invalid_pattern():
    with pytest.raises(ValueError):
        deserialize_block({"type": "regex_filter", "field": "title", "pattern": "(?i[broken"})


def test_deserialize_regex_filter_invalid_mode():
    with pytest.raises(ValueError):
        deserialize_block({"type": "regex_filter", "field": "title", "pattern": "test", "mode": "fuzzy"})


def test_deserialize_unknown_block_type():
    with pytest.raises(ValueError):
        deserialize_block({"type": "nonexistent_block"})


def test_deserialize_nested_conditional():
    block = deserialize_block({
        "type": "conditional",
        "condition": {"type": "source_type", "value": "reddit"},
        "if_true": [{"type": "keyword_filter", "include": ["python"]}],
        "if_false": [],
    })
    assert isinstance(block, Conditional)
    assert isinstance(block.condition, SourceTypeCondition)
    assert isinstance(block.if_true[0], KeywordFilter)


def test_deserialize_switch():
    block = deserialize_block({
        "type": "switch",
        "branches": [
            {"condition": {"type": "source_type", "value": "rss"}, "blocks": []},
        ],
        "default": [],
    })
    assert isinstance(block, Switch)
    assert len(block.branches) == 1


def test_is_valid_pipeline_definition():
    assert is_valid_pipeline_definition([{"type": "keyword_filter", "include": ["ai"]}])
    assert not is_valid_pipeline_definition([{"type": "bad_type"}])
    assert not is_valid_pipeline_definition("not a list")
    assert not is_valid_pipeline_definition(None)


# ─── RegexFilter runtime ──────────────────────────────────────────────────────

def run(coro):
    return asyncio.run(coro)


def test_regex_filter_include_match():
    f = RegexFilter(field="title", pattern="(?i)review", mode="include")
    result = run(f.run({"title": "Steam Deck Review: Worth it?"}))
    assert result["passed"] is True


def test_regex_filter_include_no_match():
    f = RegexFilter(field="title", pattern="(?i)review", mode="include")
    result = run(f.run({"title": "Steam Deck shipping update"}))
    assert result["passed"] is False


def test_regex_filter_exclude_match():
    f = RegexFilter(field="title", pattern="(?i)sponsored", mode="exclude")
    result = run(f.run({"title": "Sponsored: Best headsets"}))
    assert result["passed"] is False


def test_regex_filter_exclude_no_match():
    f = RegexFilter(field="title", pattern="(?i)sponsored", mode="exclude")
    result = run(f.run({"title": "Top 10 headsets"}))
    assert result["passed"] is True


def test_regex_filter_missing_field():
    f = RegexFilter(field="title", pattern="(?i)review", mode="include")
    result = run(f.run({"content": "no title here"}))
    assert result["passed"] is False


def test_regex_filter_case_insensitive():
    f = RegexFilter(field="title", pattern="REVIEW", mode="include")
    result = run(f.run({"title": "my review of the year"}))
    assert result["passed"] is True
