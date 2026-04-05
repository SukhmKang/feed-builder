"""
Multi-agent orchestration for building a feed config from a topic description.

Workflow:
1. Dispatch agent decides which specialist source agents to run.
2. Specialist source agents gather a broad set of candidate sources.
3. The orchestrator deterministically merges and deduplicates their sources.
4. Pipeline builder agent drafts pipeline JSON.
5. Critic loop evaluates passed/filtered articles and requests refinements.
6. Final config is returned once the critic is satisfied or max iterations is hit.
"""

import asyncio
import os
from typing import Any

from app.agents.cache import run_cache_context
from app.ai.critic import run_critic

from .evaluation import evaluate_pipeline, merge_source_agent_outputs
from .logging import log, log_timed
from .runtime import (
    DEFAULT_AGENT_MODEL,
    DEFAULT_CRITIC_MODEL,
    normalize_dispatch_agents,
    run_dispatch_agent,
    run_pipeline_builder_agent,
    run_source_agent,
)
from .types import DispatchPlan, PipelineAgentResult, SourceAgentOutput, SourceGenerationResult

DEFAULT_SOURCE_AGENT_CONCURRENCY = 1


def get_source_agent_concurrency() -> int:
    raw_value = str(
        os.getenv("PIPELINE_SOURCE_AGENT_CONCURRENCY", str(DEFAULT_SOURCE_AGENT_CONCURRENCY))
    ).strip()
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_SOURCE_AGENT_CONCURRENCY
    return max(1, value)


async def gather_source_agent_outputs(
    agent_names: list[str],
    topic: str,
    *,
    agent_model: str,
    verbose: bool,
) -> list[SourceAgentOutput]:
    concurrency = min(get_source_agent_concurrency(), max(1, len(agent_names)))
    log(
        verbose,
        "source_agents.execution_plan",
        {"agent_count": len(agent_names), "concurrency": concurrency, "agents": agent_names},
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(agent_name: str) -> SourceAgentOutput:
        async with semaphore:
            return await run_source_agent(agent_name, topic, model=agent_model, verbose=verbose)

    return await asyncio.gather(*[run_one(agent_name) for agent_name in agent_names])


async def build_feed_config(
    topic: str,
    *,
    max_iterations: int = 2,
    agent_model: str = DEFAULT_AGENT_MODEL,
    critic_model: str = DEFAULT_CRITIC_MODEL,
    verbose: bool = True,
) -> PipelineAgentResult:
    async with run_cache_context():
        source_generation = await build_sources_for_topic(
            topic,
            agent_model=agent_model,
            verbose=verbose,
        )
        return await build_feed_config_from_sources(
            topic,
            source_generation=source_generation,
            max_iterations=max_iterations,
            agent_model=agent_model,
            critic_model=critic_model,
            verbose=verbose,
        )


async def build_sources_for_topic(
    topic: str,
    *,
    agent_model: str = DEFAULT_AGENT_MODEL,
    verbose: bool = True,
) -> SourceGenerationResult:
    normalized_topic = topic.strip()
    if not normalized_topic:
        raise ValueError("topic must be non-empty")
    log(verbose, "source_generation.start", {"topic": normalized_topic})

    async with log_timed(verbose, "dispatch"):
        dispatch = await run_dispatch_agent(normalized_topic, model=agent_model, verbose=verbose)
    normalized_agents = normalize_dispatch_agents(dispatch["agents"])
    log(verbose, "dispatch.selected_agents", normalized_agents)

    for index, agent_name in enumerate(normalized_agents):
        log(
            verbose,
            "source_agent.start",
            {"agent": agent_name, "position": index + 1, "total": len(normalized_agents)},
        )

    async with log_timed(verbose, "source_agents"):
        specialist_outputs = await gather_source_agent_outputs(
            normalized_agents,
            normalized_topic,
            agent_model=agent_model,
            verbose=verbose,
        )

    merged_sources = merge_source_agent_outputs(specialist_outputs)
    log(verbose, "sources.merged", merged_sources)

    return {
        "topic": normalized_topic,
        "dispatch": dispatch,
        "source_agent_outputs": specialist_outputs,
        "merged_sources": merged_sources,
    }


async def build_feed_config_from_sources(
    topic: str,
    *,
    source_generation: SourceGenerationResult,
    max_iterations: int = 2,
    agent_model: str = DEFAULT_AGENT_MODEL,
    critic_model: str = DEFAULT_CRITIC_MODEL,
    verbose: bool = True,
) -> PipelineAgentResult:
    async with run_cache_context():
        return await _build_feed_config_from_sources(
            topic,
            source_generation=source_generation,
            max_iterations=max_iterations,
            agent_model=agent_model,
            critic_model=critic_model,
            verbose=verbose,
        )


async def _build_feed_config_from_sources(
    topic: str,
    *,
    source_generation: SourceGenerationResult,
    max_iterations: int = 2,
    agent_model: str = DEFAULT_AGENT_MODEL,
    critic_model: str = DEFAULT_CRITIC_MODEL,
    verbose: bool = True,
) -> PipelineAgentResult:
    normalized_topic = topic.strip()
    if not normalized_topic:
        raise ValueError("topic must be non-empty")
    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1")

    bundle_topic = str(source_generation.get("topic", "")).strip()
    if bundle_topic and bundle_topic != normalized_topic:
        raise ValueError(
            f"source_generation topic mismatch: expected {normalized_topic!r}, got {bundle_topic!r}"
        )

    dispatch = source_generation["dispatch"]
    specialist_outputs = source_generation["source_agent_outputs"]
    merged_sources = source_generation["merged_sources"]

    log(
        verbose,
        "start",
        {
            "topic": normalized_topic,
            "max_iterations": max_iterations,
            "reused_source_generation": True,
            "merged_source_count": len(merged_sources),
        },
    )

    async with log_timed(verbose, "pipeline_builder.initial"):
        current_blocks_json = await run_pipeline_builder_agent(
            normalized_topic,
            merged_sources,
            model=agent_model,
            feedback=None,
            previous_blocks_json=None,
            verbose=verbose,
        )
    log(verbose, "pipeline.initial_blocks", current_blocks_json)

    critic_history: list[dict[str, Any]] = []
    satisfied = False

    for iteration in range(1, max_iterations + 1):
        log(verbose, "iteration.start", {"iteration": iteration})
        async with log_timed(verbose, f"iteration.{iteration}.evaluate"):
            passed, filtered = await evaluate_pipeline(
                merged_sources,
                current_blocks_json,
                verbose=verbose,
            )
        log(
            verbose,
            "iteration.evaluation",
            {"iteration": iteration, "passed_count": len(passed), "filtered_count": len(filtered)},
        )
        async with log_timed(verbose, f"iteration.{iteration}.critic"):
            critic_feedback = await run_critic(
                topic=normalized_topic,
                passed=passed,
                filtered=filtered,
                blocks_json=current_blocks_json,
                model=critic_model,
            )
        critic_history.append(critic_feedback)
        log(verbose, "iteration.critic_feedback", critic_feedback)

        if bool(critic_feedback.get("satisfied")):
            satisfied = True
            log(verbose, "iteration.satisfied", {"iteration": iteration})
            return build_result(
                topic=normalized_topic,
                dispatch=dispatch,
                source_agent_outputs=specialist_outputs,
                merged_sources=merged_sources,
                blocks_json=current_blocks_json,
                critic_history=critic_history,
                satisfied=satisfied,
                iterations=iteration,
            )

        if iteration == max_iterations:
            log(verbose, "iteration.max_exceeded", {"iteration": iteration})
            break

        async with log_timed(verbose, f"pipeline_builder.iteration.{iteration}"):
            current_blocks_json = await run_pipeline_builder_agent(
                normalized_topic,
                merged_sources,
                model=agent_model,
                feedback=critic_feedback,
                previous_blocks_json=current_blocks_json,
                verbose=verbose,
            )
        log(verbose, "iteration.refined_blocks", {"iteration": iteration, "blocks": current_blocks_json})

    return build_result(
        topic=normalized_topic,
        dispatch=dispatch,
        source_agent_outputs=specialist_outputs,
        merged_sources=merged_sources,
        blocks_json=current_blocks_json,
        critic_history=critic_history,
        satisfied=satisfied,
        iterations=max_iterations,
    )


def build_result(
    *,
    topic: str,
    dispatch: DispatchPlan,
    source_agent_outputs: list[SourceAgentOutput],
    merged_sources: list[dict[str, str]],
    blocks_json: list[dict[str, Any]],
    critic_history: list[dict[str, Any]],
    satisfied: bool,
    iterations: int,
) -> PipelineAgentResult:
    final_config = {
        "topic": topic,
        "sources": merged_sources,
        "blocks": blocks_json,
    }
    return {
        "topic": topic,
        "dispatch": dispatch,
        "source_agent_outputs": source_agent_outputs,
        "merged_sources": merged_sources,
        "blocks_json": blocks_json,
        "critic_history": critic_history,
        "satisfied": satisfied,
        "iterations": iterations,
        "final_config": final_config,
    }


__all__ = [
    "DEFAULT_AGENT_MODEL",
    "DEFAULT_CRITIC_MODEL",
    "DispatchPlan",
    "PipelineAgentResult",
    "SourceAgentOutput",
    "SourceGenerationResult",
    "build_feed_config",
    "build_feed_config_from_sources",
    "build_sources_for_topic",
]
