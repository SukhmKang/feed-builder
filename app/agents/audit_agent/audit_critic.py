"""Four-step LLM critique for the audit agent.

Steps:
  1. Assessment  — blind to pipeline + sources; evaluates article sample quality
  2. Pipeline    — suggests block-level edits (pipeline-only, no source changes)
  3. Sources     — suggests remove/modify/add_needed source changes
  4. Discovery   — (optional) runs source discovery agents for each add_needed gap
"""

import json
import logging
from typing import Any

from app.ai.llm import generate_text
from app.ai.critic_utils import (
    load_json_object as _load_json_object,
    require_keys as _require_keys,
    run_structured_step,
)
from app.agents.pipeline_agent.evaluation import validate_live_sources
from app.agents.pipeline_agent.runtime import DEFAULT_AGENT_MODEL, run_source_agent

from .summarizer import format_stats_table
from .types import (
    AuditAssessment,
    AuditSummaryPayload,
    ManualOverrideAssessment,
    PipelineRecommendation,
    SourceRecommendation,
)

logger = logging.getLogger(__name__)

AUDIT_CRITIC_PROVIDER = "anthropic"
AUDIT_CRITIC_MAX_ATTEMPTS = 2
AUDIT_CRITIC_MAX_TOKENS_STEP_1 = 1400
AUDIT_CRITIC_MAX_TOKENS_MANUAL_OVERRIDES = 1000
AUDIT_CRITIC_MAX_TOKENS_STEP_2 = 3000
AUDIT_CRITIC_MAX_TOKENS_STEP_3 = 3000
AUDIT_MAX_DISCOVERY_GAPS = 3   # limit concurrent discovery runs
AUDIT_MAX_SOURCE_CHANGES = 20


STEP_1_SCHEMA = {
    "passed_quality": "Assessment of whether the passed articles are actually relevant to the topic.",
    "filtered_quality": "Assessment of whether relevant articles are being incorrectly filtered.",
    "source_quality": "Assessment of which sources are contributing well vs. generating noise.",
    "coverage_gaps": "What relevant content categories seem to be missing entirely.",
    "noise_patterns": "What irrelevant patterns are getting through the pipeline.",
    "volume_health": "Is overall article volume and pass rate healthy? Flag anomalies or declining trends.",
}

STEP_2_SCHEMA = {
    "satisfied": True,
    "feedback": "Overall pipeline assessment.",
    "issues": {
        "coverage": "What relevant things are getting filtered out.",
        "noise": "What irrelevant things are getting through.",
    },
    "suggested_changes": [
        {
            "block": "new",
            "change": "Description of the change.",
            "reason": "Why this change is needed based on the audit data.",
        }
    ],
}

MANUAL_OVERRIDE_SCHEMA = {
    "summary": "Overall takeaway from the user-corrected articles.",
    "false_positives": "What kinds of articles the pipeline incorrectly passed and the user filtered out.",
    "false_negatives": "What kinds of articles the pipeline incorrectly filtered and the user passed.",
    "patterns": "Common recurring signals in the overrides.",
    "suggested_focus": "How later audit steps should weight these corrections.",
}

STEP_3_SCHEMA = {
    "satisfied": True,
    "feedback": "Overall source set assessment.",
    "suggested_changes": [
        {
            "action": "remove|modify|add_needed",
            "source_type": "rss",
            "source_feeds": ["https://example.com/feed", "https://example.com/other-feed"],
            "reason": "Why this change is warranted by the data.",
            "coverage_gap_description": "Only for add_needed: describe what kind of source is missing.",
        }
    ],
}


async def run_audit_critic(
    *,
    topic: str,
    payload: AuditSummaryPayload,
    blocks_json: list[dict[str, Any]],
    current_sources: list[dict[str, Any]],
    model: str = DEFAULT_AGENT_MODEL,
    enable_discovery: bool = True,
    user_context: str | None = None,
) -> tuple[AuditAssessment, ManualOverrideAssessment, PipelineRecommendation, SourceRecommendation, list[dict[str, Any]]]:
    """Run the four-step audit critique.

    Returns
    -------
    (assessment, manual_override_assessment, pipeline_recommendations, source_recommendations, proposed_new_sources)
    """
    stats_table = format_stats_table(payload["stats"])

    # Step 1 — Assessment
    step_1_task = _build_step_1_task(topic, payload, stats_table, user_context=user_context)
    assessment: AuditAssessment = await _run_step(
        task_prompt=step_1_task,
        system_prompt=_step_1_system_prompt(),
        model=model,
        max_tokens=AUDIT_CRITIC_MAX_TOKENS_STEP_1,
        validator=_validate_step_1,
        label="audit critic step 1 (assessment)",
    )
    logger.info("audit_critic.step_1.done")

    manual_override_assessment: ManualOverrideAssessment = {
        "summary": "No manual overrides were available during the audit period.",
        "false_positives": "No user-filtered false positives were available.",
        "false_negatives": "No user-passed false negatives were available.",
        "patterns": "No manual override patterns were available.",
        "suggested_focus": "Rely on the general passed/filtered samples because no manual corrections were present.",
    }
    if payload["manual_override_sample"]:
        manual_override_task = _build_manual_override_task(topic, payload["manual_override_sample"])
        manual_override_assessment = await _run_step(
            task_prompt=manual_override_task,
            system_prompt=_manual_override_system_prompt(),
            model=model,
            max_tokens=AUDIT_CRITIC_MAX_TOKENS_MANUAL_OVERRIDES,
            validator=_validate_manual_override_step,
            label="audit critic manual overrides",
        )
        logger.info("audit_critic.manual_overrides.done")

    # Step 2 — Pipeline recommendations
    step_2_task = _build_step_2_task(topic, assessment, manual_override_assessment, blocks_json, user_context=user_context)
    pipeline_recs: PipelineRecommendation = await _run_step(
        task_prompt=step_2_task,
        system_prompt=_step_2_system_prompt(),
        model=model,
        max_tokens=AUDIT_CRITIC_MAX_TOKENS_STEP_2,
        validator=_validate_step_2,
        label="audit critic step 2 (pipeline)",
    )
    logger.info("audit_critic.step_2.done satisfied=%s", pipeline_recs.get("satisfied"))

    # Step 3 — Source recommendations
    step_3_task = _build_step_3_task(topic, assessment, manual_override_assessment, current_sources, payload["stats"], user_context=user_context)
    source_recs: SourceRecommendation = await _run_step(
        task_prompt=step_3_task,
        system_prompt=_step_3_system_prompt(),
        model=model,
        max_tokens=AUDIT_CRITIC_MAX_TOKENS_STEP_3,
        validator=_validate_step_3,
        label="audit critic step 3 (sources)",
    )
    logger.info("audit_critic.step_3.done satisfied=%s", source_recs.get("satisfied"))

    # Step 4 — Discovery for add_needed gaps
    proposed_new_sources: list[dict[str, Any]] = []
    if enable_discovery:
        add_needed = [
            c for c in source_recs.get("suggested_changes", [])
            if c.get("action") == "add_needed"
        ]
        if add_needed:
            proposed_new_sources = await _run_discovery(
                topic=topic,
                add_needed_changes=add_needed[:AUDIT_MAX_DISCOVERY_GAPS],
                model=model,
            )
            logger.info("audit_critic.step_4.done proposed_sources=%s", len(proposed_new_sources))

    return assessment, manual_override_assessment, pipeline_recs, source_recs, proposed_new_sources


async def _run_step(
    *,
    task_prompt: str,
    system_prompt: str,
    model: str,
    max_tokens: int,
    validator,
    label: str,
) -> dict[str, Any]:
    return await run_structured_step(
        task_prompt=task_prompt,
        system_prompt=system_prompt,
        model=model,
        provider=AUDIT_CRITIC_PROVIDER,
        max_tokens=max_tokens,
        max_attempts=AUDIT_CRITIC_MAX_ATTEMPTS,
        validator=validator,
        label=label,
        generate_text_fn=generate_text,
    )


# ---- Prompt builders ----

def _build_step_1_task(topic: str, payload: AuditSummaryPayload, stats_table: str, *, user_context: str | None = None) -> str:
    parts = [
        f"User topic: {topic.strip()}",
    ]
    if user_context and user_context.strip():
        parts += [
            "User guidance for this audit:",
            user_context.strip(),
            "---",
        ]
    parts += [
        stats_table,
        "Important: if an article sample has manually_overridden=true and a manual_verdict value, that is a user correction and should be treated as the effective ground truth for audit purposes.",
        f"Passed article sample ({len(payload['passed_sample'])} of {payload['stats']['passed_count']}):",
        json.dumps(payload["passed_sample"], indent=2),
        f"Filtered article sample ({len(payload['filtered_sample'])} of {payload['stats']['filtered_count']}):",
        json.dumps(payload["filtered_sample"], indent=2),
    ]
    parts.append(
        "Assess the overall health of this feed over the audit period based on the stats and samples above."
    )
    return "\n\n".join(parts)


def _build_manual_override_task(topic: str, manual_override_sample: list[dict[str, Any]]) -> str:
    return "\n\n".join([
        f"User topic: {topic.strip()}",
        "These article samples were manually corrected by the user and should be treated as high-signal feedback on what the feed got wrong.",
        f"Manual override sample ({len(manual_override_sample)} articles):",
        json.dumps(manual_override_sample, indent=2),
        "Summarize what the user's corrections teach us about false positives, false negatives, and recurring patterns.",
    ])


def _build_step_2_task(
    topic: str,
    assessment: AuditAssessment,
    manual_override_assessment: ManualOverrideAssessment,
    blocks_json: list[dict[str, Any]],
    *,
    user_context: str | None = None,
) -> str:
    parts = [f"User topic: {topic.strip()}"]
    if user_context and user_context.strip():
        parts += ["User guidance for this audit:", user_context.strip(), "---"]
    parts += [
        "Quality assessment from step 1:",
        json.dumps(assessment, indent=2),
        "Manual override learnings:",
        json.dumps(manual_override_assessment, indent=2),
        "Current pipeline blocks JSON:",
        json.dumps(_strip_internal_fields(blocks_json), indent=2),
        "Given this assessment and the pipeline, decide whether pipeline logic needs changes and suggest specific block-level edits.",
        "Do not suggest adding, removing, or changing sources — that is handled separately.",
    ]
    return "\n\n".join(parts)


def _build_step_3_task(
    topic: str,
    assessment: AuditAssessment,
    manual_override_assessment: ManualOverrideAssessment,
    current_sources: list[dict[str, Any]],
    stats: Any,
    *,
    user_context: str | None = None,
) -> str:
    per_source_lines: list[str] = []
    for src in stats.get("per_source", []):
        per_source_lines.append(
            f"  {src['source_type']}:{src['source_name']} — "
            f"{src['total_articles']} total, {src['pass_rate'] * 100:.1f}% pass rate"
        )
    per_source_text = "\n".join(per_source_lines) if per_source_lines else "  (no per-source data)"

    parts = [f"User topic: {topic.strip()}"]
    if user_context and user_context.strip():
        parts += ["User guidance for this audit:", user_context.strip(), "---"]
    parts += [
        "Quality assessment from step 1:",
        json.dumps(assessment, indent=2),
        "Manual override learnings:",
        json.dumps(manual_override_assessment, indent=2),
        "Current sources:",
        json.dumps(current_sources, indent=2),
        "Per-source statistics:\n" + per_source_text,
        "Based on the assessment and per-source data, decide whether the source set needs changes.",
        f"Return at most {AUDIT_MAX_SOURCE_CHANGES} suggested_changes total. Prioritize the highest-impact recommendations.",
        "You may group multiple concrete sources into a single recommendation by listing them together in source_feeds when they need the same action for the same reason.",
        "For sources you recommend removing: cite the pass rate and noise evidence.",
        "For modifications: be specific about what changes (e.g., narrower subreddit, different query). Group similar modifications when practical.",
        "For add_needed: describe the coverage gap. Do NOT invent specific feed URLs.",
        "If the source set is healthy, set satisfied=true with an empty suggested_changes list.",
    ]
    return "\n\n".join(parts)


def _step_1_system_prompt() -> str:
    return "\n\n".join([
        "You are auditing a content feed over a multi-week period.",
        "You are evaluating the pipeline AND source configuration holistically.",
        "Manual verdict overrides in the samples represent explicit user corrections and should be treated as high-signal evidence.",
        "The article sample and replay data are illustrative. Do not optimize for the current snapshot.",
        "Ask: will this feed stay relevant and healthy over the coming months?",
        "Return JSON only. Your response must be a single JSON object matching this schema exactly:",
        json.dumps(STEP_1_SCHEMA, indent=2),
        "Validation rules: every field must be a non-empty string.",
        "Do not include markdown fences, prose, or any text outside the JSON object.",
    ])


def _manual_override_system_prompt() -> str:
    return "\n\n".join([
        "You are reviewing only the articles that a human user manually corrected.",
        "Treat these corrections as high-signal evidence of what the feed got wrong.",
        "Do not suggest specific block or source edits here; just summarize the learnings.",
        "Return JSON only. Your response must be a single JSON object matching this schema exactly:",
        json.dumps(MANUAL_OVERRIDE_SCHEMA, indent=2),
        "Validation rules: every field must be a non-empty string.",
        "Do not include markdown fences, prose, or any text outside the JSON object.",
    ])


def _step_2_system_prompt() -> str:
    return "\n\n".join([
        "You are converting a pipeline quality assessment into actionable pipeline edits.",
        "You are evaluating pipeline LOGIC, not today's results.",
        "The source list is fixed and out of scope. Do not suggest adding, removing, or replacing sources.",
        "All suggested changes must be pipeline-only: block edits, additions, removals, or prompt/threshold refinements.",
        "Return JSON only. Your response must be a single JSON object matching this schema exactly:",
        json.dumps(STEP_2_SCHEMA, indent=2),
        "Validation rules:",
        "- 'satisfied' must be a boolean.",
        "- 'feedback' must be a non-empty string.",
        "- 'issues' must have non-empty string fields: coverage and noise.",
        "- 'suggested_changes' must be a list of objects with non-empty string fields: block, change, reason.",
        "Do not include markdown fences, prose, or any text outside the JSON object.",
    ])


def _step_3_system_prompt() -> str:
    return "\n\n".join([
        "You are auditing the source configuration of a content feed.",
        "Evaluate which sources contribute high-signal content vs. which generate noise or miss coverage.",
        "Base recommendations on the per-source statistics and article samples.",
        "For 'add_needed': describe the gap clearly in coverage_gap_description. Do NOT invent specific URLs.",
        "Do not suggest pipeline changes — that is handled separately.",
        "Return JSON only. Your response must be a single JSON object matching this schema exactly:",
        json.dumps(STEP_3_SCHEMA, indent=2),
        "Validation rules:",
        "- 'satisfied' must be a boolean.",
        "- 'feedback' must be a non-empty string.",
        "- 'suggested_changes' must be a list of objects with non-empty string fields: action, source_type, reason.",
        f"- 'suggested_changes' must contain at most {AUDIT_MAX_SOURCE_CHANGES} items.",
        "- 'action' must be one of: remove, modify, add_needed.",
        "- 'coverage_gap_description' is required when action is add_needed, empty string otherwise.",
        "- 'source_feeds' must be a non-empty list for remove/modify and an empty list for add_needed.",
        "Do not include markdown fences, prose, or any text outside the JSON object.",
    ])


# ---- Step 4: Discovery ----

async def _run_discovery(
    *,
    topic: str,
    add_needed_changes: list[dict[str, Any]],
    model: str,
) -> list[dict[str, Any]]:
    """Run source discovery for each coverage gap and validate results."""
    import asyncio

    async def _discover_one(change: dict[str, Any]) -> list[dict[str, Any]]:
        gap_description = str(change.get("coverage_gap_description", "")).strip()
        src_type = str(change.get("source_type", "rss")).strip().lower()
        if not gap_description:
            return []

        sub_topic = f"{gap_description} (for feed topic: {topic})"
        logger.info("audit_critic.discovery.start agent=%s sub_topic=%s", src_type, sub_topic)

        # Determine which source agent to run
        valid_agents = {"rss", "youtube", "reddit", "nitter", "tavily"}
        agent_name = src_type if src_type in valid_agents else "rss"

        try:
            output = await run_source_agent(agent_name, sub_topic, model=model, verbose=False)
            candidates = output.get("sources", [])
            if not candidates:
                return []
            valid, _failed = await validate_live_sources(
                candidates,
                label=f"audit_discovery_{agent_name}",
                verbose=False,
            )
            logger.info(
                "audit_critic.discovery.done agent=%s candidates=%s valid=%s",
                agent_name, len(candidates), len(valid),
            )
            return valid
        except Exception as exc:
            logger.warning("audit_critic.discovery.error agent=%s error=%s", agent_name, exc)
            return []

    results = await asyncio.gather(*[_discover_one(c) for c in add_needed_changes])
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source_list in results:
        for src in source_list:
            key = (str(src.get("type", "")), str(src.get("feed", "")))
            if key in seen:
                continue
            seen.add(key)
            merged.append(src)
    return merged


# ---- Validators ----

def _validate_step_1(raw: str) -> AuditAssessment:
    parsed = _load_json_object(raw)
    required = {"passed_quality", "filtered_quality", "source_quality",
                "coverage_gaps", "noise_patterns", "volume_health"}
    _require_keys(parsed, required, "audit step 1")
    for key in required:
        if not isinstance(parsed[key], str) or not parsed[key].strip():
            raise ValueError(f"audit step 1.{key} must be a non-empty string")
    return parsed


def _validate_step_2(raw: str) -> PipelineRecommendation:
    parsed = _load_json_object(raw)
    _require_keys(parsed, {"satisfied", "feedback", "issues", "suggested_changes"}, "audit step 2")
    if not isinstance(parsed["satisfied"], bool):
        raise ValueError("audit step 2.satisfied must be a boolean")
    if not isinstance(parsed["feedback"], str) or not parsed["feedback"].strip():
        raise ValueError("audit step 2.feedback must be a non-empty string")
    issues = parsed["issues"]
    if not isinstance(issues, dict):
        raise ValueError("audit step 2.issues must be an object")
    _require_keys(issues, {"coverage", "noise"}, "audit step 2.issues")
    for key in ("coverage", "noise"):
        if not isinstance(issues[key], str) or not issues[key].strip():
            raise ValueError(f"audit step 2.issues.{key} must be a non-empty string")
    changes = parsed["suggested_changes"]
    if not isinstance(changes, list):
        raise ValueError("audit step 2.suggested_changes must be a list")
    for i, change in enumerate(changes):
        if not isinstance(change, dict):
            raise ValueError(f"audit step 2.suggested_changes[{i}] must be an object")
        _require_keys(change, {"block", "change", "reason"}, f"audit step 2.suggested_changes[{i}]")
        for key in ("block", "change", "reason"):
            if not isinstance(change[key], str) or not change[key].strip():
                raise ValueError(f"audit step 2.suggested_changes[{i}].{key} must be a non-empty string")
    return parsed


def _validate_manual_override_step(raw: str) -> ManualOverrideAssessment:
    parsed = _load_json_object(raw)
    required = {"summary", "false_positives", "false_negatives", "patterns", "suggested_focus"}
    _require_keys(parsed, required, "audit manual override step")
    for key in required:
        if not isinstance(parsed[key], str) or not parsed[key].strip():
            raise ValueError(f"audit manual override step.{key} must be a non-empty string")
    return parsed


def _validate_step_3(raw: str) -> SourceRecommendation:
    parsed = _load_json_object(raw)
    _require_keys(parsed, {"satisfied", "feedback", "suggested_changes"}, "audit step 3")
    if not isinstance(parsed["satisfied"], bool):
        raise ValueError("audit step 3.satisfied must be a boolean")
    if not isinstance(parsed["feedback"], str) or not parsed["feedback"].strip():
        raise ValueError("audit step 3.feedback must be a non-empty string")
    changes = parsed["suggested_changes"]
    if not isinstance(changes, list):
        raise ValueError("audit step 3.suggested_changes must be a list")
    if len(changes) > AUDIT_MAX_SOURCE_CHANGES:
        raise ValueError(f"audit step 3.suggested_changes must contain at most {AUDIT_MAX_SOURCE_CHANGES} items")
    valid_actions = {"remove", "modify", "add_needed"}
    for i, change in enumerate(changes):
        if not isinstance(change, dict):
            raise ValueError(f"audit step 3.suggested_changes[{i}] must be an object")
        _require_keys(change, {"action", "source_type", "reason"}, f"audit step 3.suggested_changes[{i}]")
        action = str(change.get("action", "")).strip()
        if action not in valid_actions:
            raise ValueError(
                f"audit step 3.suggested_changes[{i}].action must be one of: {', '.join(sorted(valid_actions))}"
            )
        for key in ("source_type", "reason"):
            if not isinstance(change[key], str) or not change[key].strip():
                raise ValueError(f"audit step 3.suggested_changes[{i}].{key} must be a non-empty string")
        source_feeds = change.get("source_feeds", [])
        if source_feeds is None:
            source_feeds = []
        if not isinstance(source_feeds, list) or not all(isinstance(item, str) for item in source_feeds):
            raise ValueError(f"audit step 3.suggested_changes[{i}].source_feeds must be a list of strings")
        normalized_source_feeds = [item.strip() for item in source_feeds if item.strip()]
        action = str(change.get("action", "")).strip()
        if action in {"remove", "modify"} and not normalized_source_feeds:
            raise ValueError(f"audit step 3.suggested_changes[{i}].source_feeds must be non-empty for {action}")
        if action == "add_needed" and normalized_source_feeds:
            raise ValueError(f"audit step 3.suggested_changes[{i}].source_feeds must be empty for add_needed")
        change["source_feeds"] = normalized_source_feeds
        change.setdefault("coverage_gap_description", "")
    return parsed


def _strip_internal_fields(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_internal_fields(item) for item in value]
    if isinstance(value, dict):
        return {
            k: _strip_internal_fields(v)
            for k, v in value.items()
            if k not in {"batch_prompt", "batch_prompt_source_hash", "batch_size", "embedding_model"}
        }
    return value


__all__ = ["run_audit_critic"]
