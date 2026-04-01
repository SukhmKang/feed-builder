import argparse
import asyncio
import json
import os
import sys
from typing import Any

from dotenv import load_dotenv

from llm import generate_text, LLMRequestError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke test the OpenAI path in llm.py")
    parser.add_argument(
        "--model",
        default=os.getenv("TEST_OPENAI_MODEL", "gpt-5-mini"),
        help="OpenAI model to call. Defaults to TEST_OPENAI_MODEL or gpt-5-mini.",
    )
    parser.add_argument(
        "--prompt",
        default="Reply with exactly: openai path ok",
        help="Prompt to send in text mode.",
    )
    parser.add_argument(
        "--system",
        default="You are a terse test assistant.",
        help="Optional system prompt.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Run in JSON mode instead of plain text mode.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Optional max completion tokens. Defaults to llm.py provider fallback.",
    )
    return parser


async def main() -> int:
    load_dotenv()

    parser = build_parser()
    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 2

    prompt = args.prompt
    if args.json:
        prompt = (
            'Return valid JSON only with this exact shape: '
            '{"status": "ok", "echo": string}. '
            'Set status to "ok" and echo to "openai path ok".'
        )

    try:
        text = await generate_text(
            prompt,
            provider="openai",
            model=args.model,
            max_tokens=args.max_tokens,
            system=args.system,
            json_output=args.json,
        )
    except LLMRequestError as exc:
        print(f"OpenAI request failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("model:", args.model)
    print("json_mode:", args.json)
    print("response:")
    print(text)

    if args.json:
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            print(f"Response was not valid JSON: {exc}", file=sys.stderr)
            return 1
        if not isinstance(parsed, dict) or parsed.get("status") != "ok":
            print(f"Unexpected JSON payload: {parsed}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
