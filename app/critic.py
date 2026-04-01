import json
from typing import Any

from app.llm import generate_text

CRITIC_PROVIDER = "anthropic"
CRITIC_MAX_ATTEMPTS = 2
CRITIC_SAMPLE_LIMIT = 20
CRITIC_CONTENT_LIMIT = 300
CRITIC_MAX_TOKENS_STEP_1 = 1200
CRITIC_MAX_TOKENS_STEP_2 = 1400

STEP_1_SCHEMA = {
    "passed_quality": "Assessment of whether the passed articles are actually relevant.",
    "filtered_quality": "Assessment of whether relevant articles are being filtered out.",
    "coverage_gaps": "What relevant content categories seem to be missing entirely.",
    "noise_patterns": "What irrelevant patterns are getting through.",
}

FINAL_SCHEMA = {
    "satisfied": True,
    "feedback": "Overall summary of whether the pipeline is good enough or needs another iteration.",
    "issues": {
        "coverage": "What relevant things are getting filtered out.",
        "noise": "What irrelevant things are getting through.",
        "sources": "What source coverage seems missing.",
    },
    "suggested_changes": [
        {
            "block": "new",
            "change": "Add a keyword_filter for handheld PC terms.",
            "reason": "Relevant articles are missing because the current pipeline is too broad in one place and too narrow in another.",
        }
    ],
}


async def run_critic(
    *,
    topic: str,
    passed: list[dict[str, Any]],
    filtered: list[dict[str, Any]],
    blocks_json: list[dict[str, Any]],
    model: str,
) -> dict[str, Any]:
    """Evaluate a feed run and suggest concrete pipeline improvements.

    Step 1 sees only the topic plus samples of passed/filtered articles.
    Step 2 sees the Step 1 assessment plus the current pipeline JSON.
    Both steps return structured JSON that is validated locally.
    """
    step_1_input = _format_article_samples(topic=topic, passed=passed, filtered=filtered)
    step_1_result = await _run_structured_step(
        task_prompt=step_1_input,
        system_prompt=_build_step_1_system_prompt(),
        model=model,
        max_tokens=CRITIC_MAX_TOKENS_STEP_1,
        validator=_validate_step_1_output,
        label="critic step 1",
    )

    step_2_input = _build_step_2_task_prompt(
        topic=topic,
        assessment=step_1_result,
        blocks_json=blocks_json,
    )
    return await _run_structured_step(
        task_prompt=step_2_input,
        system_prompt=_build_step_2_system_prompt(),
        model=model,
        max_tokens=CRITIC_MAX_TOKENS_STEP_2,
        validator=_validate_final_output,
        label="critic step 2",
    )


async def _run_structured_step(
    *,
    task_prompt: str,
    system_prompt: str,
    model: str,
    max_tokens: int,
    validator,
    label: str,
) -> dict[str, Any]:
    validation_error = ""
    raw_response = ""
    parsed: dict[str, Any] | None = None

    for _ in range(CRITIC_MAX_ATTEMPTS):
        prompt = _build_retryable_task_prompt(task_prompt, validation_error, raw_response)
        raw_response = await generate_text(
            prompt,
            provider=CRITIC_PROVIDER,
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            json_output=True,
        )
        try:
            parsed = validator(raw_response)
            break
        except ValueError as exc:
            validation_error = str(exc)

    if parsed is None:
        raise ValueError(f"{label} returned malformed JSON after retry: {validation_error}")
    return parsed


def _build_step_1_system_prompt() -> str:
    return "\n\n".join(
        [
            "You are evaluating pipeline quality without seeing the pipeline configuration.",
            "You are evaluating pipeline LOGIC, not today's results.",
            "The article sample is illustrative only. Do not optimize for the current snapshot.",
            "Ask: will this pipeline work well across the full range of content this source typically produces over weeks and months?",
            "Return JSON only.",
            "Your response must be a single JSON object that exactly matches this schema:",
            json.dumps(STEP_1_SCHEMA, indent=2),
            "Validation rules:",
            "- Every field must be a non-empty string.",
            "- Focus only on the topic and the sampled passed/filtered articles.",
            "- Do not suggest specific block edits in this step.",
            "Do not include markdown fences, prose, or any text outside the JSON object.",
        ]
    )


def _build_step_2_system_prompt() -> str:
    return "\n\n".join(
        [
            "You are converting a pipeline quality assessment into actionable pipeline edits.",
            "You are evaluating pipeline LOGIC, not today's results.",
            "The article sample is illustrative only. Do not optimize for the current snapshot.",
            "Ask: will this pipeline work well across the full range of content this source typically produces over weeks and months?",
            "Return JSON only.",
            "Your response must be a single JSON object that exactly matches this schema:",
            json.dumps(FINAL_SCHEMA, indent=2),
            "Validation rules:",
            "- 'satisfied' must be a boolean.",
            "- 'feedback' must be a non-empty string.",
            "- 'issues' must be an object with non-empty string fields: coverage, noise, sources.",
            "- 'suggested_changes' must be a list of objects with non-empty string fields: block, change, reason.",
            "- Use 'new' in the block field when suggesting adding a new block.",
            "Do not include markdown fences, prose, or any text outside the JSON object.",
        ]
    )


def _build_retryable_task_prompt(task_prompt: str, validation_error: str, raw_response: str) -> str:
    prompt_parts = [task_prompt.strip()]
    if validation_error:
        prompt_parts.extend(
            [
                "Your previous response could not be parsed or did not conform to the required format.",
                f"Validation error: {validation_error}",
                "Rewrite the answer so it strictly matches the required JSON schema.",
                f"Previous response: {raw_response}",
            ]
        )
    return "\n\n".join(prompt_parts)


def _format_article_samples(*, topic: str, passed: list[dict[str, Any]], filtered: list[dict[str, Any]]) -> str:
    passed_sample = [_summarize_article(article) for article in passed[:CRITIC_SAMPLE_LIMIT]]
    filtered_sample = [_summarize_article(article) for article in filtered[:CRITIC_SAMPLE_LIMIT]]

    return "\n\n".join(
        [
            f"User topic:\n{topic.strip()}",
            "Passed article sample:",
            json.dumps(passed_sample, indent=2),
            "Filtered article sample:",
            json.dumps(filtered_sample, indent=2),
            "Assess the relevance quality of the passed sample, whether relevant articles appear in the filtered sample, what coverage gaps are visible, and what noise patterns are visible.",
        ]
    )


def _build_step_2_task_prompt(*, topic: str, assessment: dict[str, Any], blocks_json: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        [
            f"User topic:\n{topic.strip()}",
            "Quality assessment from step 1:",
            json.dumps(assessment, indent=2),
            "Current pipeline blocks JSON:",
            json.dumps(blocks_json, indent=2),
            "Given this assessment and the current pipeline, decide whether the pipeline is good enough and suggest specific changes that would improve coverage, reduce noise, and improve source coverage.",
        ]
    )


def _summarize_article(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(article.get("title", "")).strip(),
        "url": str(article.get("url", "")).strip(),
        "source_name": str(article.get("source_name", "")).strip(),
        "source_type": str(article.get("source_type", "")).strip(),
        "tags": list(article.get("tags", [])) if isinstance(article.get("tags"), list) else [],
        "similarity_score": article.get("similarity_score"),
        "content": _truncate_text(str(article.get("content", "")).strip(), CRITIC_CONTENT_LIMIT),
    }


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _validate_step_1_output(raw_response: str) -> dict[str, Any]:
    parsed = _load_json_object(raw_response)
    required_keys = {"passed_quality", "filtered_quality", "coverage_gaps", "noise_patterns"}
    _require_keys(parsed, required_keys, "critic step 1")
    for key in required_keys:
        if not isinstance(parsed[key], str) or not parsed[key].strip():
            raise ValueError(f"critic step 1.{key} must be a non-empty string")
    return parsed


def _validate_final_output(raw_response: str) -> dict[str, Any]:
    parsed = _load_json_object(raw_response)
    _require_keys(parsed, {"satisfied", "feedback", "issues", "suggested_changes"}, "critic output")

    if not isinstance(parsed["satisfied"], bool):
        raise ValueError("critic output.satisfied must be a boolean")
    if not isinstance(parsed["feedback"], str) or not parsed["feedback"].strip():
        raise ValueError("critic output.feedback must be a non-empty string")

    issues = parsed["issues"]
    if not isinstance(issues, dict):
        raise ValueError("critic output.issues must be an object")
    _require_keys(issues, {"coverage", "noise", "sources"}, "critic output.issues")
    for key in ("coverage", "noise", "sources"):
        if not isinstance(issues[key], str) or not issues[key].strip():
            raise ValueError(f"critic output.issues.{key} must be a non-empty string")

    suggested_changes = parsed["suggested_changes"]
    if not isinstance(suggested_changes, list):
        raise ValueError("critic output.suggested_changes must be a list")
    for index, change in enumerate(suggested_changes):
        if not isinstance(change, dict):
            raise ValueError(f"critic output.suggested_changes[{index}] must be an object")
        _require_keys(change, {"block", "change", "reason"}, f"critic output.suggested_changes[{index}]")
        for key in ("block", "change", "reason"):
            if not isinstance(change[key], str) or not change[key].strip():
                raise ValueError(f"critic output.suggested_changes[{index}].{key} must be a non-empty string")

    return parsed


def _load_json_object(raw_response: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Response was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("Response must be a JSON object")
    return parsed


def _require_keys(mapping: dict[str, Any], keys: set[str], label: str) -> None:
    missing_keys = keys.difference(mapping.keys())
    if missing_keys:
        raise ValueError(f"{label} is missing keys: {', '.join(sorted(missing_keys))}")


__all__ = ["run_critic"]
