import asyncio
import re
import os
from typing import Any

import anthropic
import openai

DEFAULT_ANTHROPIC_MAX_TOKENS = 1024


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
                "max_tokens": max_tokens if max_tokens is not None else DEFAULT_ANTHROPIC_MAX_TOKENS,
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
            if max_tokens is not None:
                kwargs["max_completion_tokens"] = max_tokens
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
        blocks = getattr(response, "content", []) or []
        parts: list[str] = []
        for block in blocks:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n\n".join(parts).strip()

    if provider == "openai":
        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""

        message = getattr(choices[0], "message", None)
        if message is None:
            return ""

        content = getattr(message, "content", None)
        if isinstance(content, str):
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
            return "\n\n".join(parts).strip()

        return ""

    raise LLMProviderError(f"Unsupported LLM provider: {provider}")


def _normalize_json_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return stripped

    fenced_match = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
    if fenced_match:
        return fenced_match.group(1).strip()

    return stripped


__all__ = ["generate_text", "LLMProviderError", "LLMRequestError"]
