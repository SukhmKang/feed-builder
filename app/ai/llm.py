import asyncio
import logging
import os
import re
from typing import Any

import anthropic
import openai

DEFAULT_MAX_TOKENS = 1024
logger = logging.getLogger(__name__)


class LLMProviderError(RuntimeError):
    """Raised when an unsupported LLM provider is requested."""


class LLMRequestError(RuntimeError):
    """Raised when an LLM request cannot be completed."""


_clients: dict[str, Any] = {}


async def generate_text(
    prompt: str,
    *,
    provider: str,
    model: str,
    max_tokens: int | None = None,
    max_completion_tokens: int | None = None,
    system: str | None = None,
    json_output: bool = False,
    model_params: dict[str, Any] | None = None,
) -> str:
    """Run a text generation request through the selected provider and return plain text.

    `model_params` is provider-specific and is passed through after the shared
    parameters are assembled.

    When `json_output=True` and `provider="openai"`, the request defaults to
    `response_format={"type": "json_object"}` unless `model_params` already
    provides its own `response_format`.
    """
    response = await asyncio.to_thread(
        _create_message,
        prompt,
        provider=provider,
        model=model,
        max_tokens=max_tokens,
        max_completion_tokens=max_completion_tokens,
        system=system,
        json_output=json_output,
        model_params=model_params or {},
    )
    text = _extract_text(response, provider=provider)
    if json_output:
        return _normalize_json_text(text)
    return text


def _create_message(
    prompt: str,
    *,
    provider: str,
    model: str,
    max_tokens: int | None,
    max_completion_tokens: int | None,
    system: str | None,
    json_output: bool,
    model_params: dict[str, Any],
) -> Any:
    client = _get_client(provider)

    try:
        if provider == "anthropic":
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": (
                    max_tokens
                    if max_tokens is not None
                    else max_completion_tokens
                    if max_completion_tokens is not None
                    else DEFAULT_MAX_TOKENS
                ),
            }
            if system:
                kwargs["system"] = system
            kwargs.update(model_params)
            return client.messages.create(**kwargs)

        if provider == "openai":
            kwargs = {
                "model": model,
                "messages": _build_openai_messages(prompt, system=system),
            }
            kwargs["max_completion_tokens"] = (
                max_completion_tokens
                if max_completion_tokens is not None
                else max_tokens
                if max_tokens is not None
                else DEFAULT_MAX_TOKENS
            )
            if json_output and "response_format" not in model_params:
                kwargs["response_format"] = {"type": "json_object"}
            kwargs.update(model_params)
            return client.chat.completions.create(**kwargs)
    except Exception as exc:
        raise LLMRequestError(f"{provider} request failed: {exc}") from exc

    raise LLMProviderError(f"Unsupported LLM provider: {provider}")


def _get_client(provider: str) -> Any:
    if provider in _clients:
        return _clients[provider]

    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise LLMRequestError("ANTHROPIC_API_KEY is not configured")
        client = anthropic.Anthropic(api_key=api_key)
        _clients[provider] = client
        return client

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise LLMRequestError("OPENAI_API_KEY is not configured")

        client = openai.OpenAI(api_key=api_key)
        _clients[provider] = client
        return client

    raise LLMProviderError(f"Unsupported LLM provider: {provider}")


def _build_openai_messages(prompt: str, *, system: str | None) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return messages


def _extract_text(response: Any, *, provider: str) -> str:
    if provider == "anthropic":
        stop_reason = getattr(response, "stop_reason", None)
        usage = getattr(response, "usage", None)
        blocks = getattr(response, "content", []) or []
        parts: list[str] = []
        for block in blocks:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        text = "\n\n".join(parts).strip()
        logger.info(
            "Anthropic response metadata stop_reason=%r usage=%r text_chars=%s",
            stop_reason,
            usage,
            len(text),
        )
        if not text:
            logger.warning(
                "Anthropic response text was empty stop_reason=%r usage=%r blocks=%r",
                stop_reason,
                usage,
                blocks,
            )
        return text

    if provider == "openai":
        choices = getattr(response, "choices", None) or []
        if not choices:
            logger.warning("OpenAI response had no choices")
            return ""

        message = getattr(choices[0], "message", None)
        finish_reason = getattr(choices[0], "finish_reason", None)
        usage = getattr(response, "usage", None)
        logger.info(
            "OpenAI response metadata finish_reason=%r usage=%r",
            finish_reason,
            usage,
        )
        if message is None:
            logger.warning("OpenAI response had no message on first choice")
            return ""

        content = getattr(message, "content", None)
        if isinstance(content, str):
            if not content.strip():
                logger.warning(
                    "OpenAI response content was empty string finish_reason=%r usage=%r",
                    finish_reason,
                    usage,
                )
            return content.strip()

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                text = getattr(item, "text", None)
                if text:
                    parts.append(text)
                    continue
                if isinstance(item, dict) and item.get("text"):
                    parts.append(str(item["text"]))
            text = "\n\n".join(parts).strip()
            if not text:
                logger.warning(
                    "OpenAI response content list produced empty text finish_reason=%r usage=%r raw_content=%r",
                    finish_reason,
                    usage,
                    content,
                )
            return text

        if content is None:
            logger.warning(
                "OpenAI response message content was None finish_reason=%r usage=%r",
                finish_reason,
                usage,
            )
        return ""

    raise LLMProviderError(f"Unsupported LLM provider: {provider}")


def _normalize_json_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return stripped

    fenced_match = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
    if fenced_match:
        return fenced_match.group(1).strip()

    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()

    return stripped


__all__ = ["generate_text", "LLMProviderError", "LLMRequestError"]
