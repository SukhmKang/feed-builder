"""
Multi-agent orchestration for building a feed config from a topic description.

Workflow:
1. Dispatch agent decides which specialist source agents to run.
2. Specialist source agents gather a broad set of candidate sources:
   - RSS agent
   - YouTube agent
   - Reddit agent
   - Nitter agent
3. The orchestrator deterministically merges and deduplicates their sources.
4. Pipeline builder agent drafts pipeline JSON.
5. Critic loop evaluates passed/filtered articles and requests refinements.
6. Final config is returned once the critic is satisfied or max iterations is hit.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Literal, TypedDict
from urllib.parse import parse_qs, unquote, urlparse

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

from agent_tools import CUSTOM_BLOCK_TOOLS, DISCOVERY_TOOLS, FEED_TOOLS, UTILITY_TOOLS
from critic import run_critic
from llm import generate_text
from pipeline import run_pipeline
from pipeline_schema import PIPELINE_SCHEMA_PROMPT, deserialize_pipeline
from runner import fetch_articles

PROJECT_ROOT = Path(__file__).resolve().parent

DEFAULT_AGENT_MODEL = "claude-sonnet-4-6"
DEFAULT_CRITIC_MODEL = "claude-sonnet-4-6"
DEFAULT_AGENT_MAX_ATTEMPTS = 3
DEFAULT_AGENT_MAX_BUDGET_USD = 5.0
DEFAULT_SOURCE_AGENT_DELAY_SECONDS = 1.0
SOURCE_AGENT_NAMES = ("rss", "youtube", "reddit", "nitter")
SOURCE_SPEC_TYPES = {
    "rss",
    "google_news_search",
    "nitter_user",
    "nitter_search",
    "reddit_subreddit",
    "reddit_search",
    "reddit_subreddits_by_topic",
    "youtube_search",
    "youtube_channel",
    "youtube_channel_url",
    "youtube_channels_by_topic",
    "youtube_videos_by_topic",
}

TOOL_BY_NAME = {
    tool.name: tool
    for tool in [*DISCOVERY_TOOLS, *FEED_TOOLS, *CUSTOM_BLOCK_TOOLS, *UTILITY_TOOLS]
}


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


async def build_feed_config(
    topic: str,
    *,
    max_iterations: int = 2,
    agent_model: str = DEFAULT_AGENT_MODEL,
    critic_model: str = DEFAULT_CRITIC_MODEL,
    verbose: bool = True,
) -> PipelineAgentResult:
    """Build a source list and pipeline config for a topic using a multi-agent loop."""

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
    """Run dispatch and source agents only, returning a reusable intermediate source bundle."""

    normalized_topic = topic.strip()
    if not normalized_topic:
        raise ValueError("topic must be non-empty")
    _log(verbose, "source_generation.start", {"topic": normalized_topic})

    dispatch = await _run_dispatch_agent(normalized_topic, model=agent_model, verbose=verbose)
    normalized_agents = _normalize_dispatch_agents(dispatch["agents"])
    _log(verbose, "dispatch.selected_agents", normalized_agents)

    for index, agent_name in enumerate(normalized_agents):
        _log(
            verbose,
            "source_agent.start",
            {"agent": agent_name, "position": index + 1, "total": len(normalized_agents)},
        )

    specialist_outputs = await asyncio.gather(
        *[
            _run_source_agent(agent_name, normalized_topic, model=agent_model, verbose=verbose)
            for agent_name in normalized_agents
        ]
    )

    merged_sources = _merge_source_agent_outputs(specialist_outputs)
    _log(verbose, "sources.merged", merged_sources)

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
    """Build and refine pipeline logic using an existing source-generation bundle."""

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

    _log(
        verbose,
        "start",
        {
            "topic": normalized_topic,
            "max_iterations": max_iterations,
            "reused_source_generation": True,
            "merged_source_count": len(merged_sources),
        },
    )

    current_blocks_json = await _run_pipeline_builder_agent(
        normalized_topic,
        merged_sources,
        model=agent_model,
        feedback=None,
        previous_blocks_json=None,
        verbose=verbose,
    )
    _log(verbose, "pipeline.initial_blocks", current_blocks_json)

    critic_history: list[dict[str, Any]] = []
    satisfied = False

    for iteration in range(1, max_iterations + 1):
        _log(verbose, "iteration.start", {"iteration": iteration})
        passed, filtered = await _evaluate_pipeline(
            merged_sources,
            current_blocks_json,
            verbose=verbose,
        )
        _log(
            verbose,
            "iteration.evaluation",
            {"iteration": iteration, "passed_count": len(passed), "filtered_count": len(filtered)},
        )
        critic_feedback = await run_critic(
            topic=normalized_topic,
            passed=passed,
            filtered=filtered,
            blocks_json=current_blocks_json,
            model=critic_model,
        )
        critic_history.append(critic_feedback)
        _log(verbose, "iteration.critic_feedback", critic_feedback)

        if bool(critic_feedback.get("satisfied")):
            satisfied = True
            _log(verbose, "iteration.satisfied", {"iteration": iteration})
            return _build_result(
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
            _log(verbose, "iteration.max_exceeded", {"iteration": iteration})
            break

        current_blocks_json = await _run_pipeline_builder_agent(
            normalized_topic,
            merged_sources,
            model=agent_model,
            feedback=critic_feedback,
            previous_blocks_json=current_blocks_json,
            verbose=verbose,
        )
        _log(verbose, "iteration.refined_blocks", {"iteration": iteration, "blocks": current_blocks_json})

    return _build_result(
        topic=normalized_topic,
        dispatch=dispatch,
        source_agent_outputs=specialist_outputs,
        merged_sources=merged_sources,
        blocks_json=current_blocks_json,
        critic_history=critic_history,
        satisfied=satisfied,
        iterations=max_iterations,
    )


async def _run_dispatch_agent(topic: str, *, model: str, verbose: bool) -> DispatchPlan:
    prompt = "\n".join(
        [
            "Decide which source-family specialist agents should run for this topic.",
            "Valid agent names: rss, youtube, reddit, nitter.",
            "Bias toward running multiple agents when the topic is broad or news-like.",
            "Return JSON only with this shape:",
            json.dumps(
                {
                    "agents": ["rss", "youtube", "reddit", "nitter"],
                    "reasons": {
                        "rss": "why",
                        "youtube": "why",
                    },
                },
                indent=2,
            ),
            f"Topic: {topic}",
        ]
    )
    _log(verbose, "dispatch.prompt", prompt)
    raw_text = await generate_text(
        prompt,
        provider="anthropic",
        model=model,
        system=(
            "You are a dispatch agent. "
            "Return valid JSON only. "
            "Choose which specialist source agents should run for the topic."
        ),
        json_output=True,
    )
    _log(verbose, "dispatch.raw_text", raw_text)
    parsed = _parse_json_text(raw_text)
    agents = parsed.get("agents", [])
    reasons = parsed.get("reasons", {})
    if not isinstance(agents, list) or not all(isinstance(item, str) for item in agents):
        raise ValueError("Dispatch agent returned invalid agents list")
    if not isinstance(reasons, dict):
        raise ValueError("Dispatch agent returned invalid reasons object")
    normalized_agents = _normalize_dispatch_agents(agents)
    return {
        "agents": normalized_agents,
        "reasons": {key: str(value).strip() for key, value in reasons.items() if isinstance(key, str)},
    }


async def _run_source_agent(agent_name: str, topic: str, *, model: str, verbose: bool) -> SourceAgentOutput:
    prompt = _build_source_agent_prompt(agent_name, topic)
    _log(verbose, f"{agent_name}.prompt", prompt)
    submitted = await _run_agent_with_submission(
        prompt,
        system_prompt=_source_agent_system_prompt(agent_name),
        tool_names=_source_agent_tool_names(agent_name),
        model=model,
        max_turns=14,
        agent_name=agent_name,
        verbose=verbose,
        submission_tool=_build_source_submission_tool(agent_name, verbose=verbose),
    )
    validated_sources = submitted["sources"]
    return {
        "agent": agent_name,
        "sources": validated_sources,
        "notes": submitted["notes"],
    }


async def _run_pipeline_builder_agent(
    topic: str,
    selected_sources: list[dict[str, str]],
    *,
    model: str,
    feedback: dict[str, Any] | None,
    previous_blocks_json: list[dict[str, Any]] | None,
    verbose: bool,
) -> list[dict[str, Any]]:
    prompt_parts = [
        "Build a pipeline for the given topic and source set.",
        "You are designing pipeline logic, not tuning to today's sample.",
        "Assume source content will vary over weeks and months.",
        "Optimize for rules that will hold up across the typical range of content these sources produce, not just the current snapshot.",
        "Use the available tools to preview sources, inspect existing custom blocks, and create custom blocks only when needed.",
        "Before creating a new custom block, search existing custom blocks to find reusable ones.",
        "If you decide a new custom block is needed and you are unsure of the exact interface, call get_custom_block_docs before writing it.",
        "When the pipeline needs source-type-specific behavior, prefer a switch block over deeply nested conditionals.",
        "Use validate_pipeline_json to sanity-check candidate pipeline JSON before final submission when helpful.",
        "Return only the pipeline JSON array.",
        PIPELINE_SCHEMA_PROMPT,
        f"Topic: {topic}",
        "Selected sources:",
        json.dumps(selected_sources, indent=2),
    ]
    if previous_blocks_json is not None:
        prompt_parts.extend(
            [
                "Previous pipeline JSON:",
                json.dumps(previous_blocks_json, indent=2),
            ]
        )
    if feedback is not None:
        prompt_parts.extend(
            [
                "Critic feedback from the previous iteration:",
                json.dumps(feedback, indent=2),
                "Revise the pipeline to address the feedback.",
            ]
        )

    _log(
        verbose,
        "pipeline_builder.input",
        {
            "topic": topic,
            "selected_sources": selected_sources,
            "previous_blocks_json": previous_blocks_json,
            "feedback": feedback,
        },
    )
    submitted = await _run_agent_with_submission(
        "\n".join(prompt_parts),
        system_prompt=(
            "You are a pipeline building agent. "
            "You are designing pipeline logic for the long run, not optimizing to today's sample. "
            "Assume the previewed articles are illustrative only, and build rules that will generalize across the future stream. "
            "Prefer reusable custom blocks over creating new ones. "
            "Prefer switch blocks for source-type-specific routing instead of deeply nested conditionals. "
            "Use tools as needed, and when you are satisfied call submit_pipeline with the final pipeline JSON."
        ),
        tool_names=[
            "preview_feed",
            "preview_sources",
            "search_custom_blocks",
            "list_custom_blocks",
            "read_custom_block",
            "get_custom_block_docs",
            "write_custom_block",
            "delete_custom_block",
            "test_custom_block",
            "validate_pipeline_json",
            "list_env_vars",
            "install_package",
        ],
        model=model,
        max_turns=20,
        agent_name="pipeline_builder",
        verbose=verbose,
        submission_tool=_build_pipeline_submission_tool(),
    )
    blocks_json = submitted["blocks_json"]
    deserialize_pipeline(blocks_json)
    return blocks_json


async def _evaluate_pipeline(
    selected_sources: list[dict[str, str]],
    blocks_json: list[dict[str, Any]],
    *,
    verbose: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blocks = deserialize_pipeline(blocks_json)
    _log(verbose, "evaluation.sources", selected_sources)
    articles = await _fetch_articles_for_evaluation(selected_sources, verbose=verbose)
    _log(verbose, "evaluation.article_preview", [_article_log_summary(article) for article in articles[:10]])

    results = await asyncio.gather(*[run_pipeline(article, blocks) for article in articles])
    passed: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    for article, result in zip(articles, results, strict=False):
        enriched_article = result["article"]
        if result["passed"]:
            passed.append(enriched_article)
        else:
            filtered.append(enriched_article)
    return passed, filtered


async def _fetch_articles_for_evaluation(
    selected_sources: list[dict[str, str]],
    *,
    verbose: bool,
) -> list[dict[str, Any]]:
    fetched_batches = await asyncio.gather(
        *[fetch_articles([source]) for source in selected_sources],
        return_exceptions=True,
    )

    articles: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for source, result in zip(selected_sources, fetched_batches, strict=False):
        if isinstance(result, Exception):
            error_payload = {
                "type": source["type"],
                "feed": source["feed"],
                "error_type": type(result).__name__,
                "error": str(result),
            }
            errors.append(error_payload)
            _log(verbose, "evaluation.source_fetch_error", error_payload)
            continue
        articles.extend(result)

    if errors:
        _log(
            verbose,
            "evaluation.source_fetch_summary",
            {
                "source_count": len(selected_sources),
                "success_count": len(selected_sources) - len(errors),
                "error_count": len(errors),
            },
        )

    if not articles:
        raise ValueError("Evaluation could not fetch articles from any selected source")

    return articles


async def _run_agent_with_submission(
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
        _log(
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
                "feed_builder_tools": _build_sdk_server(
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
                await _collect_final_text(client, agent_name=agent_name, verbose=verbose)

            if submission_store["value"] is None:
                raise ValueError(f"{agent_name} agent did not call its submission tool")

            _log(verbose, f"{agent_name}.submitted", submission_store["value"])
            return submission_store["value"]
        except Exception as exc:
            last_error = exc
            _log(
                verbose,
                f"{agent_name}.attempt_error",
                {
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            if attempt >= DEFAULT_AGENT_MAX_ATTEMPTS or not _should_retry_agent_error(exc):
                break
            backoff_seconds = float(2 ** attempt)
            _log(
                verbose,
                f"{agent_name}.retry_backoff",
                {"attempt": attempt, "sleep_seconds": backoff_seconds},
            )
            await asyncio.sleep(backoff_seconds)

    if last_error is None:
        raise ValueError(f"{agent_name} agent failed without an explicit exception")
    raise last_error


async def _collect_final_text(client: ClaudeSDKClient, *, agent_name: str, verbose: bool) -> str:
    last_text = ""
    async for message in client.receive_response():
        _log(verbose, f"{agent_name}.message_type", type(message).__name__)
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text = block.text.strip()
                    if text:
                        _log(verbose, f"{agent_name}.assistant_message", text)
                        last_text = text
                elif isinstance(block, ToolUseBlock):
                    _log(
                        verbose,
                        f"{agent_name}.tool_use",
                        {
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        },
                    )
                elif isinstance(block, ToolResultBlock):
                    _log(
                        verbose,
                        f"{agent_name}.tool_result",
                        {
                            "tool_use_id": block.tool_use_id,
                            "is_error": bool(block.is_error),
                            "content": block.content,
                        },
                    )
        elif isinstance(message, UserMessage):
            _log(
                verbose,
                f"{agent_name}.user_message",
                {
                    "parent_tool_use_id": message.parent_tool_use_id,
                    "tool_use_result": message.tool_use_result,
                    "content": message.content,
                },
            )
        elif isinstance(message, SessionMessage):
            _log(
                verbose,
                f"{agent_name}.session_message",
                {
                    "type": message.type,
                    "uuid": message.uuid,
                    "session_id": message.session_id,
                    "parent_tool_use_id": message.parent_tool_use_id,
                    "message": message.message,
                },
            )
        elif isinstance(message, TaskStartedMessage):
            _log(
                verbose,
                f"{agent_name}.task_started",
                {
                    "task_id": message.task_id,
                    "description": message.description,
                    "tool_use_id": message.tool_use_id,
                    "task_type": message.task_type,
                },
            )
        elif isinstance(message, TaskProgressMessage):
            _log(
                verbose,
                f"{agent_name}.task_progress",
                {
                    "task_id": message.task_id,
                    "description": message.description,
                    "tool_use_id": message.tool_use_id,
                    "last_tool_name": message.last_tool_name,
                    "usage": message.usage,
                },
            )
        elif isinstance(message, TaskNotificationMessage):
            _log(
                verbose,
                f"{agent_name}.task_notification",
                {
                    "task_id": message.task_id,
                    "status": message.status,
                    "tool_use_id": message.tool_use_id,
                    "summary": message.summary,
                    "output_file": message.output_file,
                },
            )
        elif isinstance(message, SystemMessage):
            _log(
                verbose,
                f"{agent_name}.system_message",
                {
                    "subtype": message.subtype,
                    "data": message.data,
                },
            )
        elif isinstance(message, ResultMessage):
            _log(
                verbose,
                f"{agent_name}.result",
                {
                    "subtype": message.subtype,
                    "duration_ms": message.duration_ms,
                    "duration_api_ms": message.duration_api_ms,
                    "is_error": message.is_error,
                    "num_turns": message.num_turns,
                    "stop_reason": message.stop_reason,
                    "total_cost_usd": message.total_cost_usd,
                    "result": message.result,
                },
            )
        else:
            _log(verbose, f"{agent_name}.unhandled_message", repr(message))
    return _strip_json_fences(last_text)


def _build_sdk_server(server_name: str, tool_names: list[str], *, extra_tools: list[Any]) -> dict[str, Any]:
    selected_tools = [_require_tool(name) for name in tool_names] + list(extra_tools)
    return create_sdk_mcp_server(name=server_name, version="1.0.0", tools=selected_tools)


def _require_tool(name: str) -> Any:
    if name not in TOOL_BY_NAME:
        raise ValueError(f"Unknown tool requested for agent: {name}")
    return TOOL_BY_NAME[name]


def _normalize_dispatch_agents(values: list[str]) -> list[str]:
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


def _validate_source_spec(source: dict[str, Any], *, label: str) -> dict[str, str]:
    source_type = str(source.get("type", "")).strip()
    feed = str(source.get("feed", "")).strip()
    if source_type not in SOURCE_SPEC_TYPES:
        raise ValueError(f"{label} has unsupported type: {source_type}")
    if not feed:
        raise ValueError(f"{label} is missing a non-empty feed value")
    return {"type": source_type, "feed": feed}


def _normalize_submitted_source_spec(
    source: dict[str, Any],
    *,
    agent_name: str,
    label: str,
) -> dict[str, str]:
    feed = str(source.get("feed", "")).strip()
    if not feed:
        raise ValueError(f"{label} is missing a non-empty feed value")

    if agent_name == "youtube":
        return _canonicalize_youtube_source_spec(feed)
    if agent_name == "reddit":
        return _canonicalize_reddit_source_spec(feed)
    return _validate_source_spec(source, label=label)


def _canonicalize_youtube_source_spec(feed: str) -> dict[str, str]:
    channel_id = _extract_youtube_channel_id_from_feed_url(feed)
    if channel_id:
        return {"type": "youtube_channel", "feed": channel_id}
    if _looks_like_youtube_url(feed) or feed.startswith("UC"):
        return {"type": "youtube_channel", "feed": feed}
    return {"type": "youtube_search", "feed": feed}


def _canonicalize_reddit_source_spec(feed: str) -> dict[str, str]:
    subreddit_name = _extract_reddit_subreddit_name(feed)
    if subreddit_name:
        return {"type": "reddit_subreddit", "feed": subreddit_name}

    if _looks_like_reddit_url(feed):
        query = _extract_reddit_search_query(feed)
        if query:
            return {"type": "reddit_search", "feed": query}

    normalized = feed.strip()
    if normalized.lower().startswith("r/"):
        normalized = normalized[2:]
    if _looks_like_simple_subreddit_name(normalized):
        return {"type": "reddit_subreddit", "feed": normalized}
    return {"type": "reddit_search", "feed": feed}


def _extract_youtube_channel_id_from_feed_url(value: str) -> str | None:
    parsed = urlparse(value)
    hostname = parsed.netloc.lower()
    if hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        return None
    if parsed.path != "/feeds/videos.xml":
        return None
    channel_ids = parse_qs(parsed.query).get("channel_id", [])
    for channel_id in channel_ids:
        normalized = str(channel_id).strip()
        if normalized:
            return normalized
    return None


def _looks_like_youtube_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and "youtube.com" in parsed.netloc.lower()


def _extract_reddit_subreddit_name(value: str) -> str | None:
    normalized = value.strip()
    if normalized.lower().startswith("r/"):
        candidate = normalized[2:].strip().strip("/")
        return candidate or None

    parsed = urlparse(normalized)
    if parsed.scheme in {"http", "https"} and "reddit.com" in parsed.netloc.lower():
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) >= 2 and path_parts[0].lower() == "r":
            candidate = unquote(path_parts[1]).strip()
            return candidate or None
    return None


def _extract_reddit_search_query(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or "reddit.com" not in parsed.netloc.lower():
        return None
    query_values = parse_qs(parsed.query).get("q", [])
    for query in query_values:
        normalized = unquote(str(query)).strip()
        if normalized:
            return normalized
    return None


def _looks_like_reddit_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and "reddit.com" in parsed.netloc.lower()


def _looks_like_simple_subreddit_name(value: str) -> bool:
    if not value or " " in value or "/" in value:
        return False
    return value.replace("_", "").isalnum()


def _parse_json_text(text: str) -> Any:
    stripped = _strip_json_fences(text)
    return json.loads(stripped)


def _strip_json_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def _build_source_submission_tool(agent_name: str, *, verbose: bool) -> Any:
    def factory(store: dict[str, Any]) -> Any:
        source_item_properties: dict[str, Any] = {"feed": {"type": "string"}}
        source_item_required = ["feed"]
        if agent_name not in {"youtube", "reddit"}:
            source_item_properties["type"] = {"type": "string"}
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
            try:
                validated_sources = [
                    _normalize_submitted_source_spec(item, agent_name=agent_name, label=f"{agent_name} source")
                    for item in sources
                ]
                await _validate_live_sources(validated_sources, label=f"{agent_name} source", verbose=verbose)
            except Exception as exc:
                return {"content": [{"type": "text", "text": str(exc)}], "is_error": True}
            store["value"] = {"sources": validated_sources, "notes": notes}
            return {"content": [{"type": "text", "text": "Source candidates accepted"}]}

        return submit_source_candidates

    return factory


def _build_pipeline_submission_tool() -> Any:
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


def _source_agent_tool_names(agent_name: str) -> list[str]:
    if agent_name == "rss":
        return [
            "discover_feeds",
            "search_web",
            "get_google_news_feed",
            "search_rsscatalog",
            "get_rsscatalog_category_feeds",
            "preview_feed",
        ]
    if agent_name == "youtube":
        return [
            "search_web",
            "search_youtube_videos",
            "search_youtube_channels",
            "get_channel_from_video",
            "get_channel_feed",
            "preview_sources",
        ]
    if agent_name == "reddit":
        return [
            "search_subreddits",
            "search_reddit_posts",
            "get_subreddit_from_post",
            "get_subreddit_feed",
            "preview_sources",
        ]
    if agent_name == "nitter":
        return [
            "search_web",
            "preview_nitter_user",
            "preview_nitter_search",
            "preview_sources",
        ]
    raise ValueError(f"Unsupported source agent: {agent_name}")


def _source_agent_system_prompt(agent_name: str) -> str:
    if agent_name == "youtube":
        source_shape = {"sources": [{"feed": "UCKKGlGrWD1ZxicRrqF6K98A"}], "notes": "short explanation"}
    elif agent_name == "reddit":
        source_shape = {"sources": [{"feed": "AceAttorney"}], "notes": "short explanation"}
    else:
        source_shape = {
            "sources": [{"type": "rss", "feed": "https://example.com/feed"}],
            "notes": "short explanation",
        }
    return "\n".join(
        [
            f"You are the {agent_name.upper()} source agent.",
            "You may use your own knowledge of good sources, and you may also use the provided tools to discover or verify them.",
            "If you already know a strong source URL or feed URL, use it directly instead of searching for it again.",
            "Discovery tools are helpers for when you do not already know the right URL or need extra candidates.",
            "Use discover_feeds only once you have a concrete publication/site name, domain, or homepage URL. If you only have a fuzzy description of a source, use search_web first to identify the actual site.",
            "Do not rely only on tools if you already know strong sources for the topic.",
            "Find strong candidate sources for the topic.",
            "Preview candidates before recommending them when possible.",
            "Prefer recurring, high-signal sources over broad noisy searches.",
            "When you are ready, call submit_source_candidates with this shape:",
            json.dumps(source_shape, indent=2),
        ]
    )


def _build_source_agent_prompt(agent_name: str, topic: str) -> str:
    source_hint: dict[str, str] = {
        "rss": (
            "Focus on official sites, publisher feeds, press blogs, and high-signal editorial feeds. "
            "Prefer native RSS feeds when available, and also use google_news_search when it adds useful coverage. "
            "Google News queries may include both site constraints and topic keywords, "
            'for example: site:bbc.com "Ace Attorney". '
            "If you have only a descriptive lead for a source, identify the actual site first before calling discover_feeds."
        ),
        "youtube": (
            "Focus on relevant channels or YouTube source types that are stable for ongoing coverage. "
            "Submit only the canonical channel identifier, channel URL, or search query as the feed value; "
            "the orchestrator will deterministically map it to the correct YouTube source type."
        ),
        "reddit": (
            "Focus on strong subreddit-based sources for ongoing topic coverage. "
            "Submit only the subreddit name, subreddit URL/RSS URL, or search query as the feed value; "
            "the orchestrator will deterministically map it to the correct Reddit source type."
        ),
        "nitter": (
            "Focus on official accounts, developers, publishers, and high-signal topic accounts. "
            "Use nitter_search only when specific accounts are not enough."
        ),
    }
    return "\n".join(
        [
            f"Topic: {topic}",
            f"Agent type: {agent_name}",
            source_hint[agent_name],
            "Recommend 5 to 12 sources when the topic supports it.",
            "Favor breadth over premature narrowing.",
        ]
    )


def _build_result(
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


def _log(enabled: bool, event: str, payload: Any) -> None:
    if not enabled:
        return
    print(f"[pipeline_agent] {event}")
    if isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=True, default=str))
    print()


def _article_log_summary(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(article.get("title", "")).strip(),
        "url": str(article.get("url", "")).strip(),
        "published_at": str(article.get("published_at", "")).strip(),
        "source_name": str(article.get("source_name", "")).strip(),
        "source_type": str(article.get("source_type", "")).strip(),
    }


def _should_retry_agent_error(exc: Exception) -> bool:
    text = str(exc).lower()
    retry_markers = (
        "529",
        "overloaded",
        "rate limit",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "server error",
        "connection reset",
    )
    return any(marker in text for marker in retry_markers)


async def _validate_live_sources(
    sources: list[dict[str, str]],
    *,
    label: str,
    verbose: bool,
) -> None:
    for source in sources:
        _log(verbose, "source_validation.start", {"label": label, "source": source})
        try:
            articles = await fetch_articles([source])
        except Exception as exc:
            raise ValueError(f"{label} failed validation for {source['type']}:{source['feed']}: {exc}") from exc
        if not articles:
            raise ValueError(f"{label} returned no articles during validation for {source['type']}:{source['feed']}")
        _log(
            verbose,
            "source_validation.success",
            {
                "label": label,
                "source": source,
                "article_count": len(articles),
                "preview": [_article_log_summary(article) for article in articles[:3]],
            },
        )


def _merge_source_agent_outputs(source_agent_outputs: list[SourceAgentOutput]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for output in source_agent_outputs:
        for source in output["sources"]:
            key = (source["type"], source["feed"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(source)
    return merged


__all__ = [
    "DispatchPlan",
    "PipelineAgentResult",
    "SourceAgentOutput",
    "SourceGenerationResult",
    "build_feed_config",
    "build_feed_config_from_sources",
    "build_sources_for_topic",
]
