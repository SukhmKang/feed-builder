"""Shared utilities for structured LLM critique steps with retry logic."""

import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

CRITIC_RESPONSE_LOG_SNIPPET_CHARS = 500


async def run_structured_step(
    *,
    task_prompt: str,
    system_prompt: str,
    model: str,
    provider: str,
    max_tokens: int,
    max_attempts: int,
    validator: Callable[[str], dict[str, Any]],
    label: str,
    generate_text_fn: Any,
) -> dict[str, Any]:
    """Run a single structured LLM step with validation and retry.

    Parameters
    ----------
    generate_text_fn:
        The ``generate_text`` callable from ``app.ai.llm``. Passed explicitly
        to avoid a circular import when this module is loaded by critic.py.
    """
    validation_error = ""
    raw_response = ""
    parsed: dict[str, Any] | None = None

    logger.info(
        "%s start model=%s max_tokens=%s task_prompt_chars=%s system_prompt_chars=%s",
        label,
        model,
        max_tokens,
        len(task_prompt),
        len(system_prompt),
    )

    for attempt in range(1, max_attempts + 1):
        prompt = build_retryable_task_prompt(task_prompt, validation_error, raw_response)
        logger.info(
            "%s attempt=%s prompt_chars=%s retrying=%s",
            label,
            attempt,
            len(prompt),
            bool(validation_error),
        )
        raw_response = await generate_text_fn(
            prompt,
            provider=provider,
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            json_output=True,
        )
        logger.info(
            "%s attempt=%s response_chars=%s response_head=%r response_tail=%r",
            label,
            attempt,
            len(raw_response),
            raw_response[:CRITIC_RESPONSE_LOG_SNIPPET_CHARS],
            raw_response[-CRITIC_RESPONSE_LOG_SNIPPET_CHARS:],
        )
        try:
            parsed = validator(raw_response)
            logger.info("%s attempt=%s validation=success", label, attempt)
            break
        except ValueError as exc:
            validation_error = str(exc)
            logger.warning(
                "%s attempt=%s validation=failed error=%s response_tail=%r",
                label,
                attempt,
                validation_error,
                raw_response[-CRITIC_RESPONSE_LOG_SNIPPET_CHARS:],
            )

    if parsed is None:
        logger.error(
            "%s failed after %s attempts final_error=%s final_response_tail=%r",
            label,
            max_attempts,
            validation_error,
            raw_response[-CRITIC_RESPONSE_LOG_SNIPPET_CHARS:],
        )
        raise ValueError(f"{label} returned malformed JSON after retry: {validation_error}")
    return parsed


def build_retryable_task_prompt(task_prompt: str, validation_error: str, raw_response: str) -> str:
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


def load_json_object(raw_response: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Response was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("Response must be a JSON object")
    return parsed


def require_keys(mapping: dict[str, Any], keys: set[str], label: str) -> None:
    missing_keys = keys.difference(mapping.keys())
    if missing_keys:
        raise ValueError(f"{label} is missing keys: {', '.join(sorted(missing_keys))}")


__all__ = [
    "run_structured_step",
    "build_retryable_task_prompt",
    "load_json_object",
    "require_keys",
]
