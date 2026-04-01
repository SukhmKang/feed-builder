from typing import Any, TypedDict


class DispatchPlan(TypedDict):
    agents: list[str]
    reasons: dict[str, str]


class SourceAgentOutput(TypedDict):
    agent: str
    sources: list[dict[str, str]]
    notes: str


class SourceGenerationResult(TypedDict):
    topic: str
    dispatch: DispatchPlan
    source_agent_outputs: list[SourceAgentOutput]
    merged_sources: list[dict[str, str]]


class PipelineAgentResult(TypedDict):
    topic: str
    dispatch: DispatchPlan
    source_agent_outputs: list[SourceAgentOutput]
    merged_sources: list[dict[str, str]]
    blocks_json: list[dict[str, Any]]
    critic_history: list[dict[str, Any]]
    satisfied: bool
    iterations: int
    final_config: dict[str, Any]
