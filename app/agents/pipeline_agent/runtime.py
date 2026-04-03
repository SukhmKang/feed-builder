from typing import Any

from app.ai.llm import generate_text
from app.pipeline.llm_batching import compile_llm_filter_batches
from app.pipeline.schema import deserialize_pipeline

from .logging import log
from .prompts import (
    PIPELINE_BUILDER_TOOL_NAMES,
    build_dispatch_prompt,
    build_pipeline_builder_prompt,
    build_source_agent_prompt,
    dispatch_system_prompt,
    pipeline_builder_system_prompt,
    source_agent_system_prompt,
    source_agent_tool_names,
)
from .sdk import (
    build_pipeline_submission_tool,
    build_source_submission_tool,
    normalize_dispatch_agents,
    parse_json_text,
    run_agent_with_submission,
)
from .types import DispatchPlan, SourceAgentOutput
import asyncio

DEFAULT_AGENT_MODEL = "claude-sonnet-4-6"
DEFAULT_CRITIC_MODEL = "claude-sonnet-4-6"
SOURCE_AGENT_TIMEOUT_SECONDS = 600.0


async def run_dispatch_agent(topic: str, *, model: str, verbose: bool) -> DispatchPlan:
    prompt = build_dispatch_prompt(topic)
    log(verbose, "dispatch.prompt", prompt)
    raw_text = await generate_text(
        prompt,
        provider="anthropic",
        model=model,
        system=dispatch_system_prompt(),
        json_output=True,
    )
    log(verbose, "dispatch.raw_text", raw_text)
    parsed = parse_json_text(raw_text)
    agents = parsed.get("agents", [])
    reasons = parsed.get("reasons", {})
    if not isinstance(agents, list) or not all(isinstance(item, str) for item in agents):
        raise ValueError("Dispatch agent returned invalid agents list")
    if not isinstance(reasons, dict):
        raise ValueError("Dispatch agent returned invalid reasons object")
    normalized_agents = normalize_dispatch_agents(agents)
    return {
        "agents": normalized_agents,
        "reasons": {key: str(value).strip() for key, value in reasons.items() if isinstance(key, str)},
    }


async def run_source_agent(agent_name: str, topic: str, *, model: str, verbose: bool) -> SourceAgentOutput:
    prompt = build_source_agent_prompt(agent_name, topic)
    log(verbose, f"{agent_name}.prompt", prompt)
    try:
        submitted = await asyncio.wait_for(
            run_agent_with_submission(
                prompt,
                system_prompt=source_agent_system_prompt(agent_name),
                tool_names=source_agent_tool_names(agent_name),
                model=model,
                max_turns=14,
                agent_name=agent_name,
                verbose=verbose,
                submission_tool=build_source_submission_tool(agent_name, verbose=verbose),
            ),
            timeout=SOURCE_AGENT_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        log(
            verbose,
            f"{agent_name}.timeout",
            {
                "timeout_seconds": SOURCE_AGENT_TIMEOUT_SECONDS,
            },
        )
        raise TimeoutError(f"{agent_name} source agent timed out after {SOURCE_AGENT_TIMEOUT_SECONDS:.0f}s") from exc
    validated_sources = submitted["sources"]
    log(
        verbose,
        f"{agent_name}.done",
        {
            "agent": agent_name,
            "source_count": len(validated_sources),
            "notes": submitted["notes"],
        },
    )
    return {
        "agent": agent_name,
        "sources": validated_sources,
        "notes": submitted["notes"],
    }


async def run_pipeline_builder_agent(
    topic: str,
    selected_sources: list[dict[str, str]],
    *,
    model: str,
    feedback: dict[str, Any] | None,
    previous_blocks_json: list[dict[str, Any]] | None,
    verbose: bool,
) -> list[dict[str, Any]]:
    prompt = build_pipeline_builder_prompt(
        topic,
        selected_sources,
        feedback=feedback,
        previous_blocks_json=previous_blocks_json,
    )

    log(
        verbose,
        "pipeline_builder.input",
        {
            "topic": topic,
            "selected_sources": selected_sources,
            "previous_blocks_json": previous_blocks_json,
            "feedback": feedback,
        },
    )
    submitted = await run_agent_with_submission(
        prompt,
        system_prompt=pipeline_builder_system_prompt(),
        tool_names=PIPELINE_BUILDER_TOOL_NAMES,
        model=model,
        max_turns=20,
        agent_name="pipeline_builder",
        verbose=verbose,
        submission_tool=build_pipeline_submission_tool(),
    )
    log(
        verbose,
        "pipeline_builder.submission_received",
        {
            "block_count": len(submitted["blocks_json"]),
        },
    )
    blocks_json = submitted["blocks_json"]
    deserialize_pipeline(blocks_json)
    log(verbose, "pipeline_builder.schema_validated", {"block_count": len(blocks_json)})
    compiled_blocks_json = await compile_llm_filter_batches(blocks_json)
    log(verbose, "pipeline_builder.batch_prompts_compiled", {"block_count": len(compiled_blocks_json)})
    deserialize_pipeline(compiled_blocks_json)
    log(verbose, "pipeline_builder.compiled_schema_validated", {"block_count": len(compiled_blocks_json)})
    return compiled_blocks_json
