import os
from typing import Literal

LLMTier = Literal["mini", "medium", "high"]
LLMFamily = Literal["anthropic", "openai"]

TIER_MAP: dict[LLMFamily, dict[LLMTier, str]] = {
    "anthropic": {
        "mini": "claude-haiku-4-5-20251001",
        "medium": "claude-sonnet-4-6",
        "high": "claude-opus-4-6",
    },
    "openai": {
        "mini": os.getenv("PIPELINE_OPENAI_MINI_MODEL", "gpt-5-mini"),
        "medium": os.getenv("PIPELINE_OPENAI_MEDIUM_MODEL", "gpt-5"),
        "high": os.getenv("PIPELINE_OPENAI_HIGH_MODEL", "gpt-5"),
    },
}


def get_pipeline_llm_family() -> LLMFamily:
    family = str(os.getenv("PIPELINE_LLM_FAMILY", "anthropic")).strip().lower()
    if family not in TIER_MAP:
        raise ValueError(f"Unsupported PIPELINE_LLM_FAMILY: {family}")
    return family  # type: ignore[return-value]


def resolve_tier_model(tier: LLMTier) -> tuple[LLMFamily, str]:
    family = get_pipeline_llm_family()
    return family, TIER_MAP[family][tier]


__all__ = ["LLMFamily", "LLMTier", "TIER_MAP", "get_pipeline_llm_family", "resolve_tier_model"]
