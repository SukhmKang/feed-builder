"""Pydantic v2 schema models for pipeline blocks and conditions.

These are the single source of truth for the pipeline JSON schema.
They replace the hand-written deserializer in schema.py and drive TypeScript
type generation via scripts/generate_pipeline_types.py.

Runtime classes (KeywordFilter, LLMCondition, etc.) remain in filters.py and
conditions.py. Each schema model's .to_runtime() method bridges to them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

if TYPE_CHECKING:
    from app.pipeline.core import Block, Condition

MAX_LLM_PROMPT_CHARS = 2500

# ─── Condition schemas ────────────────────────────────────────────────────────


class AndConditionSchema(BaseModel):
    type: Literal["and"]
    conditions: list[ConditionSchema]

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import And

        return And([c.to_runtime() for c in self.conditions])


class OrConditionSchema(BaseModel):
    type: Literal["or"]
    conditions: list[ConditionSchema]

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import Or

        return Or([c.to_runtime() for c in self.conditions])


class NotConditionSchema(BaseModel):
    type: Literal["not"]
    condition: ConditionSchema

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import Not

        return Not(self.condition.to_runtime())


SourceType = Literal["rss", "reddit", "youtube", "nitter", "google_news", "tavily"]
ArticleField = Literal["title", "content", "full_text", "source_name", "source_type", "url", "published_at"]


class SourceTypeConditionSchema(BaseModel):
    type: Literal["source_type"]
    value: SourceType

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import SourceTypeCondition

        return SourceTypeCondition(type=self.value)


class SourceNameConditionSchema(BaseModel):
    type: Literal["source_name"]
    value: str

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import SourceNameCondition

        return SourceNameCondition(name=self.value)


class SourceUrlConditionSchema(BaseModel):
    type: Literal["source_url"]
    value: str

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import SourceUrlCondition

        return SourceUrlCondition(url=self.value)


class DomainConditionSchema(BaseModel):
    type: Literal["domain"]
    value: str

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import DomainCondition

        return DomainCondition(domain=self.value)


class SourceDomainConditionSchema(BaseModel):
    type: Literal["source_domain"]
    value: str

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import SourceDomainCondition

        return SourceDomainCondition(domain=self.value)


class FieldEqualsConditionSchema(BaseModel):
    type: Literal["field_equals"]
    field: ArticleField
    value: str

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import FieldEqualsCondition

        return FieldEqualsCondition(field=self.field, value=self.value)


class FieldContainsConditionSchema(BaseModel):
    type: Literal["field_contains"]
    field: ArticleField
    value: str

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import FieldContainsCondition

        return FieldContainsCondition(field=self.field, value=self.value)


class FieldExistsConditionSchema(BaseModel):
    type: Literal["field_exists"]
    field: ArticleField

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import FieldExistsCondition

        return FieldExistsCondition(field=self.field)


class FieldMatchesRegexConditionSchema(BaseModel):
    type: Literal["field_matches_regex"]
    field: ArticleField
    pattern: str

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import FieldMatchesRegexCondition

        return FieldMatchesRegexCondition(field=self.field, pattern=self.pattern)


class KeywordConditionSchema(BaseModel):
    type: Literal["keyword"]
    terms: list[str]
    operator: Literal["any", "all"]

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import KeywordCondition

        return KeywordCondition(terms=self.terms, operator=self.operator)


class LengthConditionSchema(BaseModel):
    type: Literal["length"]
    field: ArticleField
    min: int
    max: int

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import LengthCondition

        return LengthCondition(field=self.field, min=self.min, max=self.max)


class PublishedAfterConditionSchema(BaseModel):
    type: Literal["published_after"]
    days_ago: int

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import PublishedAfterCondition

        return PublishedAfterCondition(days_ago=self.days_ago)


class PublishedBeforeConditionSchema(BaseModel):
    type: Literal["published_before"]
    days_ago: int

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import PublishedBeforeCondition

        return PublishedBeforeCondition(days_ago=self.days_ago)


class LLMConditionSchema(BaseModel):
    type: Literal["llm"]
    prompt: str
    tier: Literal["mini", "medium", "high"] = "mini"

    @field_validator("prompt")
    @classmethod
    def validate_prompt_length(cls, v: str) -> str:
        if len(v) > MAX_LLM_PROMPT_CHARS:
            raise ValueError(f"prompt must be at most {MAX_LLM_PROMPT_CHARS} characters")
        return v

    def to_runtime(self) -> Condition:
        from app.pipeline.conditions import LLMCondition

        return LLMCondition(prompt=self.prompt, tier=self.tier)


ConditionSchema = Annotated[
    Union[
        AndConditionSchema,
        OrConditionSchema,
        NotConditionSchema,
        SourceTypeConditionSchema,
        SourceNameConditionSchema,
        SourceUrlConditionSchema,
        DomainConditionSchema,
        SourceDomainConditionSchema,
        FieldEqualsConditionSchema,
        FieldContainsConditionSchema,
        FieldExistsConditionSchema,
        FieldMatchesRegexConditionSchema,
        KeywordConditionSchema,
        LengthConditionSchema,
        PublishedAfterConditionSchema,
        PublishedBeforeConditionSchema,
        LLMConditionSchema,
    ],
    Field(discriminator="type"),
]

# ─── Block schemas ────────────────────────────────────────────────────────────


class KeywordFilterSchema(BaseModel):
    type: Literal["keyword_filter"]
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_at_least_one_nonempty(self) -> "KeywordFilterSchema":
        if not self.include and not self.exclude:
            raise ValueError("At least one of 'include' or 'exclude' must be non-empty")
        return self

    def to_runtime(self) -> Block:
        from app.pipeline.filters import KeywordFilter

        return KeywordFilter(include=self.include, exclude=self.exclude)


class SemanticSimilaritySchema(BaseModel):
    type: Literal["semantic_similarity"]
    query: str
    field: ArticleField
    threshold: float = 0.6
    embedding_model: str = "text-embedding-3-small"

    def to_runtime(self) -> Block:
        from app.pipeline.filters import SemanticSimilarity

        return SemanticSimilarity(
            query=self.query,
            field=self.field,
            threshold=self.threshold,
            embedding_model=self.embedding_model,
        )


class LLMFilterSchema(BaseModel):
    type: Literal["llm_filter"]
    prompt: str
    tier: Literal["mini", "medium", "high"] = "mini"
    batch_prompt: str | None = None
    batch_size: int = 10

    @field_validator("prompt")
    @classmethod
    def validate_prompt_length(cls, v: str) -> str:
        if len(v) > MAX_LLM_PROMPT_CHARS:
            raise ValueError(f"prompt must be at most {MAX_LLM_PROMPT_CHARS} characters")
        return v

    def to_runtime(self) -> Block:
        from app.pipeline.filters import LLMFilter

        return LLMFilter(
            prompt=self.prompt,
            tier=self.tier,
            batch_prompt=self.batch_prompt,
            batch_size=self.batch_size,
        )


class ConditionalSchema(BaseModel):
    type: Literal["conditional"]
    condition: ConditionSchema
    if_true: list[BlockSchema] = Field(default_factory=list)
    if_false: list[BlockSchema] = Field(default_factory=list)

    def to_runtime(self) -> Block:
        from app.pipeline.filters import Conditional

        return Conditional(
            condition=self.condition.to_runtime(),
            if_true=[b.to_runtime() for b in self.if_true],
            if_false=[b.to_runtime() for b in self.if_false],
        )


class SwitchBranchSchema(BaseModel):
    condition: ConditionSchema
    blocks: list[BlockSchema] = Field(default_factory=list)


class SwitchSchema(BaseModel):
    type: Literal["switch"]
    branches: list[SwitchBranchSchema]
    default: list[BlockSchema] = Field(default_factory=list)

    def to_runtime(self) -> Block:
        from app.pipeline.filters import Switch

        return Switch(
            branches=[
                (branch.condition.to_runtime(), [b.to_runtime() for b in branch.blocks])
                for branch in self.branches
            ],
            default=[b.to_runtime() for b in self.default],
        )


class RegexFilterSchema(BaseModel):
    type: Literal["regex_filter"]
    field: ArticleField
    pattern: str
    mode: Literal["include", "exclude"] = "include"

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, v: str) -> str:
        import re

        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"pattern is not a valid regex: {exc}") from exc
        return v

    def to_runtime(self) -> Block:
        from app.pipeline.filters import RegexFilter

        return RegexFilter(field=self.field, pattern=self.pattern, mode=self.mode)


class CustomBlockSchema(BaseModel):
    type: Literal["custom_block"]
    name: str

    def to_runtime(self) -> Block:
        from app.pipeline.filters import CustomBlock

        return CustomBlock(name=self.name)


BlockSchema = Annotated[
    Union[
        KeywordFilterSchema,
        SemanticSimilaritySchema,
        LLMFilterSchema,
        RegexFilterSchema,
        ConditionalSchema,
        SwitchSchema,
        CustomBlockSchema,
    ],
    Field(discriminator="type"),
]

# Rebuild models that have forward references to ConditionSchema / BlockSchema
AndConditionSchema.model_rebuild()
OrConditionSchema.model_rebuild()
NotConditionSchema.model_rebuild()
ConditionalSchema.model_rebuild()
SwitchBranchSchema.model_rebuild()
SwitchSchema.model_rebuild()


__all__ = [
    "AndConditionSchema",
    "ArticleField",
    "BlockSchema",
    "ConditionalSchema",
    "ConditionSchema",
    "CustomBlockSchema",
    "DomainConditionSchema",
    "FieldContainsConditionSchema",
    "FieldEqualsConditionSchema",
    "FieldExistsConditionSchema",
    "FieldMatchesRegexConditionSchema",
    "KeywordConditionSchema",
    "KeywordFilterSchema",
    "LLMConditionSchema",
    "LLMFilterSchema",
    "LengthConditionSchema",
    "NotConditionSchema",
    "OrConditionSchema",
    "PublishedAfterConditionSchema",
    "PublishedBeforeConditionSchema",
    "RegexFilterSchema",
    "SemanticSimilaritySchema",
    "SourceDomainConditionSchema",
    "SourceNameConditionSchema",
    "SourceType",
    "SourceTypeConditionSchema",
    "SourceUrlConditionSchema",
    "SwitchBranchSchema",
    "SwitchSchema",
]
