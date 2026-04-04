import asyncio
import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SessionMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

from app.agents.agent_tools import CUSTOM_BLOCK_TOOLS, DISCOVERY_TOOLS, FEED_TOOLS, UTILITY_TOOLS
from app.pipeline.schema import deserialize_pipeline

from .evaluation import validate_live_sources
from .logging import log, should_retry_agent_error
from .source_specs import normalize_submitted_source_spec, validate_source_spec

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AGENT_MAX_ATTEMPTS = 3
DEFAULT_AGENT_MAX_BUDGET_USD = 5.0
SOURCE_AGENT_NAMES = ("rss", "youtube", "reddit", "nitter", "tavily")

TOOL_BY_NAME = {
    tool.name: tool
    for tool in [*DISCOVERY_TOOLS, *FEED_TOOLS, *CUSTOM_BLOCK_TOOLS, *UTILITY_TOOLS]
}


async def run_agent_with_submission(
    prompt: str,
    *,
    system_prompt: str,
    tool_names: list[str],
    model: str,
    max_turns: int = 10,
    agent_name: str,
    verbose: bool,
    submission_tool: Any,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, DEFAULT_AGENT_MAX_ATTEMPTS + 1):
        log(
            verbose,
            f"{agent_name}.attempt_start",
            {
                "attempt": attempt,
                "max_attempts": DEFAULT_AGENT_MAX_ATTEMPTS,
                "model": model,
                "tool_names": tool_names,
                "max_turns": max_turns,
                "system_prompt": system_prompt,
                "prompt": prompt,
            },
        )
        submission_store: dict[str, Any] = {"value": None}
        wrapped_submission_tool = submission_tool(submission_store)
        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            mcp_servers={
                "feed_builder_tools": build_sdk_server(
                    "feed_builder_tools",
                    tool_names,
                    extra_tools=[wrapped_submission_tool],
                )
            },
            permission_mode="bypassPermissions",
            model=model,
            max_turns=max_turns,
            max_budget_usd=DEFAULT_AGENT_MAX_BUDGET_USD,
            cwd=PROJECT_ROOT,
        )
        try:
            async with ClaudeSDKClient(options) as client:
                await client.query(prompt)
                await collect_final_text(client, agent_name=agent_name, verbose=verbose)

            if submission_store["value"] is None:
                raise ValueError(f"{agent_name} agent did not call its submission tool")

            log(verbose, f"{agent_name}.submitted", submission_store["value"])
            return submission_store["value"]
        except Exception as exc:
            last_error = exc
            log(
                verbose,
                f"{agent_name}.attempt_error",
                {
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            if attempt >= DEFAULT_AGENT_MAX_ATTEMPTS or not should_retry_agent_error(exc):
                break
            backoff_seconds = float(2 ** attempt)
            log(
                verbose,
                f"{agent_name}.retry_backoff",
                {"attempt": attempt, "sleep_seconds": backoff_seconds},
            )
            await asyncio.sleep(backoff_seconds)

    if last_error is None:
        raise ValueError(f"{agent_name} agent failed without an explicit exception")
    raise last_error


async def collect_final_text(client: ClaudeSDKClient, *, agent_name: str, verbose: bool) -> str:
    last_text = ""
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text = block.text.strip()
                    if text:
                        log(verbose, f"{agent_name}.assistant_message", text)
                        last_text = text
                elif isinstance(block, ToolUseBlock):
                    log(
                        verbose,
                        f"{agent_name}.tool_use",
                        {"name": block.name, "input": block.input},
                    )
                elif isinstance(block, ToolResultBlock):
                    log(
                        verbose,
                        f"{agent_name}.tool_result",
                        {"is_error": bool(block.is_error), "content": block.content},
                    )
        elif isinstance(message, ResultMessage):
            log(
                verbose,
                f"{agent_name}.result",
                {
                    "num_turns": message.num_turns,
                    "stop_reason": message.stop_reason,
                    "total_cost_usd": message.total_cost_usd,
                    "is_error": message.is_error,
                },
            )
    return strip_json_fences(last_text)


def build_sdk_server(server_name: str, tool_names: list[str], *, extra_tools: list[Any]) -> dict[str, Any]:
    selected_tools = [require_tool(name) for name in tool_names] + list(extra_tools)
    return create_sdk_mcp_server(name=server_name, version="1.0.0", tools=selected_tools)


def require_tool(name: str) -> Any:
    if name not in TOOL_BY_NAME:
        raise ValueError(f"Unknown tool requested for agent: {name}")
    return TOOL_BY_NAME[name]


def normalize_dispatch_agents(values: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        agent_name = str(value).strip().lower()
        if agent_name not in SOURCE_AGENT_NAMES or agent_name in seen:
            continue
        seen.add(agent_name)
        normalized.append(agent_name)
    if normalized:
        return normalized
    return list(SOURCE_AGENT_NAMES)


def parse_json_text(text: str) -> Any:
    stripped = strip_json_fences(text)
    return json.loads(stripped)


def strip_json_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def build_source_submission_tool(agent_name: str, *, verbose: bool) -> Any:
    def factory(store: dict[str, Any]) -> Any:
        type_schema: dict[str, Any] = {"type": "string"}
        if agent_name == "youtube":
            type_schema = {"type": "string", "enum": ["channel", "channel_url", "search", "channels_by_topic", "videos_by_topic"]}
        elif agent_name == "reddit":
            type_schema = {"type": "string", "enum": ["subreddit", "search", "subreddits_by_topic"]}
        elif agent_name == "nitter":
            type_schema = {"type": "string", "enum": ["user", "search"]}
        elif agent_name == "tavily":
            type_schema = {"type": "string", "enum": ["search"]}
        elif agent_name == "rss":
            type_schema = {"type": "string", "enum": ["rss", "google_news_search"]}

        source_item_properties: dict[str, Any] = {
            "type": type_schema,
            "feed": {"type": "string"},
        }
        source_item_required = ["type", "feed"]

        @tool(
            "submit_source_candidates",
            f"Submit the final source candidates chosen by the {agent_name} agent.",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": source_item_properties,
                            "required": source_item_required,
                        },
                        "minItems": 1,
                    },
                    "notes": {"type": "string"},
                },
                "required": ["sources", "notes"],
            },
        )
        async def submit_source_candidates(args: dict[str, Any]) -> dict[str, Any]:
            sources = args.get("sources", [])
            notes = str(args.get("notes", "")).strip()
            if not isinstance(sources, list) or not all(isinstance(item, dict) for item in sources):
                return {"content": [{"type": "text", "text": "sources must be a list of objects"}], "is_error": True}
            log(
                verbose,
                f"{agent_name}.submit_source_candidates.start",
                {
                    "submitted_source_count": len(sources),
                    "sources": sources,
                },
            )
            try:
                normalized = [
                    normalize_submitted_source_spec(item, agent_name=agent_name, label=f"{agent_name} source")
                    for item in sources
                ]
            except Exception as exc:
                return {"content": [{"type": "text", "text": str(exc)}], "is_error": True}
            log(
                verbose,
                f"{agent_name}.submit_source_candidates.normalized",
                {
                    "normalized_source_count": len(normalized),
                    "sources": normalized,
                },
            )

            valid, failed = await validate_live_sources(normalized, label=f"{agent_name} source", verbose=verbose)
            log(
                verbose,
                f"{agent_name}.submit_source_candidates.validation_done",
                {
                    "valid_count": len(valid),
                    "failed_count": len(failed),
                    "valid_sources": valid,
                    "failed_sources": [
                        {"source": source, "reason": reason}
                        for source, reason in failed
                    ],
                },
            )

            if not valid:
                lines = [f"All {len(failed)} sources failed validation:"]
                for src, reason in failed:
                    lines.append(f"  - {src.get('type', '')}:{src.get('feed', '')}: {reason}")
                return {"content": [{"type": "text", "text": "\n".join(lines)}], "is_error": True}

            store["value"] = {"sources": valid, "notes": notes}

            if not failed:
                return {"content": [{"type": "text", "text": f"All {len(valid)} sources accepted."}]}

            lines = [f"{len(valid)} sources accepted, {len(failed)} failed validation:"]
            for src, reason in failed:
                lines.append(f"  - {src.get('type', '')}:{src.get('feed', '')}: {reason}")
            lines.append("The accepted sources have been recorded. You may submit replacements for the failed ones if needed.")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        return submit_source_candidates

    return factory


def build_pipeline_submission_tool() -> Any:
    def factory(store: dict[str, Any]) -> Any:
        @tool(
            "submit_pipeline",
            "Submit the final pipeline JSON once you are satisfied with it.",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "blocks_json": {
                        "type": "array",
                        "items": {"type": "object"},
                    }
                },
                "required": ["blocks_json"],
            },
        )
        async def submit_pipeline(args: dict[str, Any]) -> dict[str, Any]:
            blocks_json = args.get("blocks_json", [])
            if not isinstance(blocks_json, list) or not all(isinstance(item, dict) for item in blocks_json):
                return {"content": [{"type": "text", "text": "blocks_json must be a list of block objects"}], "is_error": True}
            try:
                deserialize_pipeline(blocks_json)
            except Exception as exc:
                return {"content": [{"type": "text", "text": f"Invalid pipeline: {exc}"}], "is_error": True}
            store["value"] = {"blocks_json": blocks_json}
            return {"content": [{"type": "text", "text": "Pipeline accepted"}]}

        return submit_pipeline

    return factory


def build_block_edit_submission_tool() -> Any:
    def factory(store: dict[str, Any]) -> Any:
        @tool(
            "submit_block_edit",
            "Submit the replacement block sequence for the selected block.",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "replacement_blocks": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                },
                "required": ["replacement_blocks"],
            },
        )
        async def submit_block_edit(args: dict[str, Any]) -> dict[str, Any]:
            replacement_blocks = args.get("replacement_blocks", [])
            if not isinstance(replacement_blocks, list) or not all(isinstance(item, dict) for item in replacement_blocks):
                return {"content": [{"type": "text", "text": "replacement_blocks must be a list of block objects"}], "is_error": True}
            try:
                deserialize_pipeline(replacement_blocks)
            except Exception as exc:
                return {"content": [{"type": "text", "text": f"Invalid pipeline: {exc}"}], "is_error": True}
            store["value"] = {"replacement_blocks": replacement_blocks}
            return {"content": [{"type": "text", "text": "Block edit accepted"}]}

        return submit_block_edit

    return factory


def build_audit_remediation_submission_tool(*, verbose: bool) -> Any:
    def factory(store: dict[str, Any]) -> Any:
        @tool(
            "submit_audit_remediation",
            "Submit the full revised source list and full revised pipeline after applying the audit report.",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "type": {"type": "string"},
                                "feed": {"type": "string"},
                            },
                            "required": ["type", "feed"],
                        },
                    },
                    "blocks_json": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "summary": {"type": "string"},
                },
                "required": ["sources", "blocks_json", "summary"],
            },
        )
        async def submit_audit_remediation(args: dict[str, Any]) -> dict[str, Any]:
            sources = args.get("sources", [])
            blocks_json = args.get("blocks_json", [])
            summary = str(args.get("summary", "")).strip()
            if not isinstance(sources, list) or not all(isinstance(item, dict) for item in sources):
                return {"content": [{"type": "text", "text": "sources must be a list of source objects"}], "is_error": True}
            if not isinstance(blocks_json, list) or not all(isinstance(item, dict) for item in blocks_json):
                return {"content": [{"type": "text", "text": "blocks_json must be a list of block objects"}], "is_error": True}
            if not summary:
                return {"content": [{"type": "text", "text": "summary must be non-empty"}], "is_error": True}

            try:
                validated_sources = [
                    validate_source_spec(item, label=f"audit remediation source[{index}]")
                    for index, item in enumerate(sources)
                ]
            except Exception as exc:
                return {"content": [{"type": "text", "text": str(exc)}], "is_error": True}

            try:
                deserialize_pipeline(blocks_json)
            except Exception as exc:
                return {"content": [{"type": "text", "text": f"Invalid pipeline: {exc}"}], "is_error": True}

            valid_sources, failed_sources = await validate_live_sources(
                validated_sources,
                label="audit remediation source",
                verbose=verbose,
            )
            if failed_sources:
                lines = [f"{len(valid_sources)} sources accepted, {len(failed_sources)} failed validation:"]
                for src, reason in failed_sources:
                    lines.append(f"  - {src.get('type', '')}:{src.get('feed', '')}: {reason}")
                if not valid_sources:
                    return {"content": [{"type": "text", "text": "\n".join(lines)}], "is_error": True}
                lines.append("Resubmit with a fully valid source set.")
                return {"content": [{"type": "text", "text": "\n".join(lines)}], "is_error": True}

            store["value"] = {
                "sources": valid_sources,
                "blocks_json": blocks_json,
                "summary": summary,
            }
            return {"content": [{"type": "text", "text": "Audit remediation accepted"}]}

        return submit_audit_remediation

    return factory
